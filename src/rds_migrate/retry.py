"""
Aurora retry automation for failed blue-green deployments.

Blue-green deployments on Aurora can fail due to transient issues:
  - Replication lag spikes
  - Underlying infrastructure issues
  - Concurrent maintenance operations
  - Parameter group compatibility (max_logical_replication_workers on PG 16)

This module provides:
  1. EventBridge rule creation to monitor blue-green deployment failures
  2. Lambda function for automatic retry with exponential backoff
  3. Retry management (list, cancel, status)
  4. SAM template generation for CloudFormation-based deployment

The EventBridge + Lambda approach is preferred over polling because:
  - No long-running processes needed
  - Automatic retry on failure with backoff
  - CloudWatch logs for audit trail
  - Cost-effective (pay per invocation)
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Lambda function code for retry automation.
# Deployed inline via the AWS SDK without requiring a separate ZIP artifact.
RETRY_LAMBDA_CODE = '''
import boto3
import json
import os
import time

rds = boto3.client("rds")
ssm = boto3.client("ssm")
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY_BASE = int(os.environ.get("RETRY_DELAY_BASE", "300"))
SSM_PREFIX = "/rds-migrate/retry"


def handler(event, context):
    """Handle EventBridge event for failed blue-green deployment.

    Checks retry count, applies exponential backoff, deletes the failed
    deployment, and creates a new one. Retry state is tracked in SSM
    Parameter Store.
    """
    detail = event.get("detail", {})
    deployment_id = detail.get("SourceIdentifier", "")

    if not deployment_id.startswith("bgd-"):
        print(f"Ignoring non-deployment event: {deployment_id}")
        return {"status": "ignored"}

    retry_count = get_retry_count(deployment_id)
    if retry_count >= MAX_RETRIES:
        print(f"Max retries ({MAX_RETRIES}) reached for {deployment_id}")
        return {"status": "max_retries_reached", "deployment_id": deployment_id}

    delay = min(RETRY_DELAY_BASE * (2 ** retry_count), 900)
    print(f"Retrying {deployment_id} (attempt {retry_count + 1}/{MAX_RETRIES}) after {delay}s")
    time.sleep(delay)

    try:
        # Delete the failed deployment first
        rds.delete_blue_green_deployment(
            BlueGreenDeploymentIdentifier=deployment_id,
            DeleteTarget=True,
        )
        print(f"Deleted failed deployment {deployment_id}")

        # Retrieve original deployment config from SSM
        config = get_deployment_config(deployment_id)
        if not config:
            print(f"No stored config for {deployment_id}, cannot retry")
            return {"status": "no_config", "deployment_id": deployment_id}

        # Recreate the deployment
        resp = rds.create_blue_green_deployment(
            BlueGreenDeploymentName=config["name"],
            Source=config["source_arn"],
            TargetEngineVersion=config["target_version"],
        )
        new_id = resp["BlueGreenDeployment"]["BlueGreenDeploymentIdentifier"]
        print(f"Created new deployment {new_id} (retry {retry_count + 1})")
        increment_retry_count(deployment_id)
        return {"status": "retrying", "attempt": retry_count + 1, "new_deployment_id": new_id}
    except Exception as e:
        print(f"Retry failed for {deployment_id}: {e}")
        return {"status": "error", "error": str(e)}


def get_retry_count(deployment_id):
    try:
        param = ssm.get_parameter(Name=f"{SSM_PREFIX}/{deployment_id}/count")
        return int(param["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return 0


def increment_retry_count(deployment_id):
    current = get_retry_count(deployment_id)
    ssm.put_parameter(
        Name=f"{SSM_PREFIX}/{deployment_id}/count",
        Value=str(current + 1),
        Type="String",
        Overwrite=True,
    )


def get_deployment_config(deployment_id):
    try:
        param = ssm.get_parameter(Name=f"{SSM_PREFIX}/{deployment_id}/config")
        return json.loads(param["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return None
'''

# IAM trust policy allowing Lambda to assume the role.
_LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

# IAM policy for the retry Lambda function.
_RETRY_LAMBDA_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "rds:DescribeBlueGreenDeployments",
                "rds:CreateBlueGreenDeployment",
                "rds:DeleteBlueGreenDeployment",
                "rds:DescribeDBInstances",
                "rds:DescribeDBClusters",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "ssm:GetParameter",
                "ssm:PutParameter",
            ],
            "Resource": "arn:aws:ssm:*:*:parameter/rds-migrate/retry/*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": "arn:aws:logs:*:*:*",
        },
    ],
}


@dataclass
class RetryConfig:
    """Configuration for retry automation."""

    max_retries: int = 3
    retry_delay_base_seconds: int = 300
    lambda_timeout_seconds: int = 900
    lambda_memory_mb: int = 128
    rule_name_prefix: str = "rds-migrate-retry"


@dataclass
class RetryRuleResult:
    """Result of creating an EventBridge retry rule."""

    rule_name: str
    rule_arn: str | None
    lambda_function_name: str
    lambda_function_arn: str | None
    status: str
    message: str

    def __str__(self) -> str:
        return (
            f"Retry Rule: {self.rule_name}\n"
            f"Lambda:     {self.lambda_function_name}\n"
            f"Status:     {self.status}\n"
            f"Message:    {self.message}"
        )


def _build_lambda_zip() -> bytes:
    """Package RETRY_LAMBDA_CODE into a ZIP file for Lambda deployment."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.py", RETRY_LAMBDA_CODE)
    return buf.getvalue()


def create_retry_automation(
    events_client: Any,
    lambda_client: Any,
    iam_client: Any,
    config: RetryConfig | None = None,
    region: str = "us-east-1",
) -> RetryRuleResult:
    """Create EventBridge rule and Lambda function for automatic retry.

    Sets up:
      1. IAM role for the Lambda function
      2. Lambda function with retry logic
      3. EventBridge rule to capture blue-green deployment failures
      4. EventBridge target linking the rule to the Lambda function

    Args:
        events_client: boto3 EventBridge client.
        lambda_client: boto3 Lambda client.
        iam_client: boto3 IAM client.
        config: Retry configuration. Uses defaults if None.
        region: AWS region.

    Returns:
        RetryRuleResult with created resource details.
    """
    if config is None:
        config = RetryConfig()

    rule_name = f"{config.rule_name_prefix}-{region}"
    lambda_name = f"{config.rule_name_prefix}-handler"
    role_name = f"{config.rule_name_prefix}-lambda-role"

    logger.info("Creating retry automation: rule=%s, lambda=%s", rule_name, lambda_name)

    try:
        # 1. Create IAM role
        role_resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(_LAMBDA_TRUST_POLICY),
            Description="IAM role for rds-migrate retry Lambda",
        )
        role_arn = role_resp["Role"]["Arn"]

        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=f"{role_name}-policy",
            PolicyDocument=json.dumps(_RETRY_LAMBDA_POLICY),
        )
        logger.info("Created IAM role %s", role_arn)

        # 2. Create Lambda function
        lambda_resp = lambda_client.create_function(
            FunctionName=lambda_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="index.handler",
            Code={"ZipFile": _build_lambda_zip()},
            Timeout=config.lambda_timeout_seconds,
            MemorySize=config.lambda_memory_mb,
            Environment={
                "Variables": {
                    "MAX_RETRIES": str(config.max_retries),
                    "RETRY_DELAY_BASE": str(config.retry_delay_base_seconds),
                }
            },
            Description="Automatic retry for failed RDS blue-green deployments",
        )
        lambda_arn = lambda_resp["FunctionArn"]
        logger.info("Created Lambda function %s", lambda_arn)

        # 3. Create EventBridge rule
        pattern = get_eventbridge_rule_pattern()
        rule_resp = events_client.put_rule(
            Name=rule_name,
            EventPattern=json.dumps(pattern),
            State="ENABLED",
            Description="Trigger retry on blue-green deployment failure",
        )
        rule_arn = rule_resp["RuleArn"]
        logger.info("Created EventBridge rule %s", rule_arn)

        # 4. Add Lambda as target
        events_client.put_targets(
            Rule=rule_name,
            Targets=[{"Id": "retry-lambda", "Arn": lambda_arn}],
        )

        # 5. Add permission for EventBridge to invoke Lambda
        lambda_client.add_permission(
            FunctionName=lambda_name,
            StatementId="eventbridge-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )

        return RetryRuleResult(
            rule_name=rule_name,
            rule_arn=rule_arn,
            lambda_function_name=lambda_name,
            lambda_function_arn=lambda_arn,
            status="created",
            message="Retry automation created successfully.",
        )
    except Exception as exc:
        logger.error("Failed to create retry automation: %s", exc)
        return RetryRuleResult(
            rule_name=rule_name,
            rule_arn=None,
            lambda_function_name=lambda_name,
            lambda_function_arn=None,
            status="failed",
            message=f"Creation failed: {exc}",
        )


def get_eventbridge_rule_pattern() -> dict[str, Any]:
    """Return the EventBridge event pattern for blue-green deployment failures.

    This pattern matches RDS events where a blue-green deployment has failed,
    allowing the retry Lambda to be triggered automatically.

    Returns:
        EventBridge event pattern as a dictionary.
    """
    return {
        "source": ["aws.rds"],
        "detail-type": ["RDS DB Instance Event"],
        "detail": {
            "EventCategories": ["blue-green deployment"],
            "Message": [{"prefix": "Blue/green deployment failed"}],
        },
    }


def generate_sam_template(config: RetryConfig | None = None) -> str:
    """Generate a SAM/CloudFormation template for the retry automation stack.

    Args:
        config: Retry configuration. Uses defaults if None.

    Returns:
        YAML string of the SAM template.
    """
    if config is None:
        config = RetryConfig()

    template: dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Transform": "AWS::Serverless-2016-10-31",
        "Description": "RDS blue-green deployment retry automation",
        "Parameters": {
            "MaxRetries": {
                "Type": "Number",
                "Default": config.max_retries,
                "Description": "Maximum number of retry attempts",
            },
            "RetryDelayBase": {
                "Type": "Number",
                "Default": config.retry_delay_base_seconds,
                "Description": "Base delay in seconds for exponential backoff",
            },
        },
        "Globals": {
            "Function": {
                "Timeout": config.lambda_timeout_seconds,
                "MemorySize": config.lambda_memory_mb,
                "Runtime": "python3.12",
            },
        },
        "Resources": {
            "RetryFunction": {
                "Type": "AWS::Serverless::Function",
                "Properties": {
                    "Handler": "index.handler",
                    "Description": "Automatic retry for failed RDS blue-green deployments",
                    "Environment": {
                        "Variables": {
                            "MAX_RETRIES": {"Ref": "MaxRetries"},
                            "RETRY_DELAY_BASE": {"Ref": "RetryDelayBase"},
                        },
                    },
                    "Policies": [
                        {
                            "Statement": _RETRY_LAMBDA_POLICY["Statement"],
                        },
                    ],
                    "Events": {
                        "BlueGreenFailure": {
                            "Type": "EventBridgeRule",
                            "Properties": {
                                "Pattern": get_eventbridge_rule_pattern(),
                            },
                        },
                    },
                },
            },
        },
        "Outputs": {
            "RetryFunctionArn": {
                "Description": "ARN of the retry Lambda function",
                "Value": {"Fn::GetAtt": ["RetryFunction", "Arn"]},
            },
        },
    }

    return yaml.dump(template, default_flow_style=False, sort_keys=False)


def list_retry_rules(
    events_client: Any,
    rule_name_prefix: str = "rds-migrate-retry",
) -> list[dict[str, Any]]:
    """List all active retry rules.

    Args:
        events_client: boto3 EventBridge client.
        rule_name_prefix: Prefix to filter rules.

    Returns:
        List of rule descriptions.
    """
    logger.info("Listing retry rules with prefix: %s", rule_name_prefix)
    try:
        resp = events_client.list_rules(NamePrefix=rule_name_prefix)
        rules = []
        for rule in resp.get("Rules", []):
            rules.append({
                "rule_name": rule.get("Name", ""),
                "rule_arn": rule.get("Arn", ""),
                "state": rule.get("State", ""),
                "description": rule.get("Description", ""),
                "event_pattern": rule.get("EventPattern", ""),
            })
        return rules
    except Exception as exc:
        logger.error("Failed to list retry rules: %s", exc)
        return []


def delete_retry_automation(
    events_client: Any,
    lambda_client: Any,
    rule_name: str,
    lambda_name: str,
) -> bool:
    """Delete retry automation resources.

    Removes in dependency order: targets -> rule -> Lambda function.

    Args:
        events_client: boto3 EventBridge client.
        lambda_client: boto3 Lambda client.
        rule_name: EventBridge rule name.
        lambda_name: Lambda function name.

    Returns:
        True if all resources were deleted successfully.
    """
    logger.info("Deleting retry automation: rule=%s, lambda=%s", rule_name, lambda_name)

    try:
        # 1. Remove targets from the rule
        targets_resp = events_client.list_targets_by_rule(Rule=rule_name)
        target_ids = [t["Id"] for t in targets_resp.get("Targets", [])]
        if target_ids:
            events_client.remove_targets(Rule=rule_name, Ids=target_ids)
            logger.info("Removed %d targets from rule %s", len(target_ids), rule_name)

        # 2. Delete the EventBridge rule
        events_client.delete_rule(Name=rule_name)
        logger.info("Deleted rule %s", rule_name)

        # 3. Delete the Lambda function
        lambda_client.delete_function(FunctionName=lambda_name)
        logger.info("Deleted Lambda function %s", lambda_name)

        return True
    except Exception as exc:
        logger.error("Failed to delete retry automation: %s", exc)
        return False
