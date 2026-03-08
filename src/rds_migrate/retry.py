"""
Aurora retry automation for failed blue-green deployments.

Blue-green deployments on Aurora can fail due to transient issues:
  - Replication lag spikes
  - Underlying infrastructure issues
  - Concurrent maintenance operations

This module provides:
  1. EventBridge rule creation to monitor blue-green deployment failures
  2. Lambda function template for automatic retry with exponential backoff
  3. Retry management (list, cancel, status)

The EventBridge + Lambda approach is preferred over polling because:
  - No long-running processes needed
  - Automatic retry on failure
  - CloudWatch logs for audit trail
  - Cost-effective (pay per invocation)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Default Lambda function code for retry automation.
# This is embedded as a string so it can be deployed via the AWS SDK
# without requiring a separate ZIP artifact.
RETRY_LAMBDA_CODE = '''
import boto3
import json
import os
import time

rds = boto3.client("rds")
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY_BASE = int(os.environ.get("RETRY_DELAY_BASE", "300"))


def handler(event, context):
    """Handle EventBridge event for failed blue-green deployment.

    Event structure:
    {
        "source": "aws.rds",
        "detail-type": "RDS DB Instance Event",
        "detail": {
            "EventCategories": ["blue-green deployment"],
            "SourceIdentifier": "bgd-xxxx",
            "Message": "Blue/green deployment failed..."
        }
    }
    """
    detail = event.get("detail", {})
    deployment_id = detail.get("SourceIdentifier", "")

    if not deployment_id.startswith("bgd-"):
        print(f"Ignoring non-deployment event: {deployment_id}")
        return {"status": "ignored"}

    # Check retry count from DynamoDB or tags
    retry_count = get_retry_count(deployment_id)
    if retry_count >= MAX_RETRIES:
        print(f"Max retries ({MAX_RETRIES}) reached for {deployment_id}")
        return {"status": "max_retries_reached"}

    # Exponential backoff
    delay = RETRY_DELAY_BASE * (2 ** retry_count)
    print(f"Retrying {deployment_id} (attempt {retry_count + 1}/{MAX_RETRIES}) after {delay}s")
    time.sleep(min(delay, 900))  # Lambda max timeout is 15 min

    # Recreate the deployment
    # TODO: Implement retry logic
    increment_retry_count(deployment_id)
    return {"status": "retrying", "attempt": retry_count + 1}


def get_retry_count(deployment_id):
    """Get current retry count from SSM Parameter Store."""
    ssm = boto3.client("ssm")
    try:
        param = ssm.get_parameter(Name=f"/rds-migrate/retry/{deployment_id}")
        return int(param["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return 0


def increment_retry_count(deployment_id):
    """Increment retry count in SSM Parameter Store."""
    ssm = boto3.client("ssm")
    current = get_retry_count(deployment_id)
    ssm.put_parameter(
        Name=f"/rds-migrate/retry/{deployment_id}",
        Value=str(current + 1),
        Type="String",
        Overwrite=True,
    )
'''


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

    # TODO: Implement:
    # 1. Create IAM role with RDS, SSM, and CloudWatch Logs permissions
    # 2. Create Lambda function with RETRY_LAMBDA_CODE
    # 3. Create EventBridge rule with pattern:
    #    {
    #      "source": ["aws.rds"],
    #      "detail-type": ["RDS DB Instance Event"],
    #      "detail": {
    #        "EventCategories": ["blue-green deployment"],
    #        "Message": [{"prefix": "Blue/green deployment failed"}]
    #      }
    #    }
    # 4. Add Lambda as target for the rule
    # 5. Add Lambda permission for EventBridge to invoke

    logger.info("Creating retry automation: rule=%s, lambda=%s", rule_name, lambda_name)
    return RetryRuleResult(
        rule_name=rule_name,
        rule_arn=None,
        lambda_function_name=lambda_name,
        lambda_function_arn=None,
        status="not_implemented",
        message="Retry automation creation not yet implemented.",
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

    This can be used to deploy the retry automation via CloudFormation
    instead of programmatically via boto3.

    Args:
        config: Retry configuration. Uses defaults if None.

    Returns:
        YAML string of the SAM template.
    """
    if config is None:
        config = RetryConfig()

    # TODO: Generate full SAM template
    # See templates/eventbridge-lambda/template.yaml for the static version
    logger.info("Generating SAM template with config: max_retries=%d", config.max_retries)
    return "# SAM template generation not yet implemented. See templates/eventbridge-lambda/template.yaml"


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
    # TODO: Implement:
    # Call events_client.list_rules(NamePrefix=rule_name_prefix)
    logger.info("Listing retry rules with prefix: %s", rule_name_prefix)
    return []


def delete_retry_automation(
    events_client: Any,
    lambda_client: Any,
    rule_name: str,
    lambda_name: str,
) -> bool:
    """Delete retry automation resources.

    Removes the EventBridge rule, Lambda function, and associated IAM role.

    Args:
        events_client: boto3 EventBridge client.
        lambda_client: boto3 Lambda client.
        rule_name: EventBridge rule name.
        lambda_name: Lambda function name.

    Returns:
        True if all resources were deleted successfully.
    """
    # TODO: Implement:
    # 1. Remove targets from the rule
    # 2. Delete the rule
    # 3. Delete the Lambda function
    # 4. Delete the IAM role
    logger.info("Deleting retry automation: rule=%s, lambda=%s", rule_name, lambda_name)
    return False
