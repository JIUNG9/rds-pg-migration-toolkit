"""
Unit tests for retry automation module.

Tests EventBridge rule creation, Lambda deployment, SAM template generation,
rule listing, and cleanup.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import yaml

from rds_migrate.retry import (
    RetryConfig,
    RetryRuleResult,
    create_retry_automation,
    delete_retry_automation,
    generate_sam_template,
    get_eventbridge_rule_pattern,
    list_retry_rules,
)


class TestEventBridgePattern:
    """Tests for the EventBridge event pattern."""

    def test_pattern_structure(self) -> None:
        """Pattern should match RDS blue-green deployment failures."""
        pattern = get_eventbridge_rule_pattern()
        assert pattern["source"] == ["aws.rds"]
        assert "detail-type" in pattern
        assert "detail" in pattern
        assert "EventCategories" in pattern["detail"]
        assert pattern["detail"]["Message"] == [{"prefix": "Blue/green deployment failed"}]

    def test_pattern_is_valid_json(self) -> None:
        """Pattern should be JSON-serializable."""
        pattern = get_eventbridge_rule_pattern()
        json_str = json.dumps(pattern)
        parsed = json.loads(json_str)
        assert parsed == pattern


class TestRetryConfig:
    """Tests for the RetryConfig dataclass."""

    def test_defaults(self) -> None:
        """Should have sensible default values."""
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.retry_delay_base_seconds == 300
        assert config.lambda_timeout_seconds == 900
        assert config.lambda_memory_mb == 128
        assert config.rule_name_prefix == "rds-migrate-retry"

    def test_custom_values(self) -> None:
        """Should accept custom configuration."""
        config = RetryConfig(max_retries=5, lambda_memory_mb=256)
        assert config.max_retries == 5
        assert config.lambda_memory_mb == 256


class TestCreateAutomation:
    """Tests for the create_retry_automation function."""

    def test_creates_all_resources(self) -> None:
        """Should create IAM role, Lambda, EventBridge rule, and target."""
        events = MagicMock()
        lam = MagicMock()
        iam = MagicMock()

        iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123:role/test-role"}}
        lam.create_function.return_value = {"FunctionArn": "arn:aws:lambda:us-east-1:123:function:test"}
        events.put_rule.return_value = {"RuleArn": "arn:aws:events:us-east-1:123:rule/test"}

        result = create_retry_automation(events, lam, iam)
        assert result.status == "created"
        assert result.rule_arn is not None
        assert result.lambda_function_arn is not None

        iam.create_role.assert_called_once()
        iam.put_role_policy.assert_called_once()
        lam.create_function.assert_called_once()
        events.put_rule.assert_called_once()
        events.put_targets.assert_called_once()
        lam.add_permission.assert_called_once()

    def test_handles_creation_error(self) -> None:
        """Should return failed status on error."""
        events = MagicMock()
        lam = MagicMock()
        iam = MagicMock()
        iam.create_role.side_effect = Exception("AccessDenied")

        result = create_retry_automation(events, lam, iam)
        assert result.status == "failed"
        assert "AccessDenied" in result.message


class TestRetryRuleResult:
    """Tests for the RetryRuleResult dataclass and its operator-facing __str__."""

    def test_str_reports_every_field_an_operator_needs(self) -> None:
        """__str__ is what gets printed after a create — it must not hide the status."""
        result = RetryRuleResult(
            rule_name="rds-migrate-retry-abc",
            rule_arn="arn:aws:events:us-east-1:123:rule/rds-migrate-retry-abc",
            lambda_function_name="rds-migrate-retry-abc-fn",
            lambda_function_arn="arn:aws:lambda:us-east-1:123:function:rds-migrate-retry-abc-fn",
            status="created",
            message="Retry automation created successfully.",
        )
        rendered = str(result)
        assert "rds-migrate-retry-abc" in rendered
        assert "rds-migrate-retry-abc-fn" in rendered
        assert "created" in rendered
        assert "Retry automation created successfully." in rendered

    def test_str_on_a_failed_result_surfaces_the_reason(self) -> None:
        """A failure with no ARNs must still print why, or the operator is blind."""
        result = RetryRuleResult(
            rule_name="rds-migrate-retry-xyz",
            rule_arn=None,
            lambda_function_name="rds-migrate-retry-xyz-fn",
            lambda_function_arn=None,
            status="failed",
            message="Creation failed: AccessDenied",
        )
        rendered = str(result)
        assert "failed" in rendered
        assert "AccessDenied" in rendered
        assert "None" not in rendered


class TestListRetryRules:
    """Tests for the list_retry_rules function."""

    def test_empty_list(self) -> None:
        """Should return empty list when no rules exist."""
        events = MagicMock()
        events.list_rules.return_value = {"Rules": []}
        result = list_retry_rules(events)
        assert result == []

    def test_returns_rules(self) -> None:
        """Should parse rule responses correctly."""
        events = MagicMock()
        events.list_rules.return_value = {
            "Rules": [
                {"Name": "rds-migrate-retry-us-east-1", "Arn": "arn:1", "State": "ENABLED", "Description": "test"},
            ]
        }
        result = list_retry_rules(events)
        assert len(result) == 1
        assert result[0]["rule_name"] == "rds-migrate-retry-us-east-1"
        assert result[0]["state"] == "ENABLED"

    def test_handles_error(self) -> None:
        """Should return empty list on error."""
        events = MagicMock()
        events.list_rules.side_effect = Exception("throttled")
        result = list_retry_rules(events)
        assert result == []


class TestDeleteAutomation:
    """Tests for the delete_retry_automation function."""

    def test_deletes_in_order(self) -> None:
        """Should delete targets, then rule, then Lambda."""
        events = MagicMock()
        lam = MagicMock()
        events.list_targets_by_rule.return_value = {"Targets": [{"Id": "retry-lambda"}]}

        result = delete_retry_automation(events, lam, "test-rule", "test-lambda")
        assert result is True

        events.remove_targets.assert_called_once()
        events.delete_rule.assert_called_once_with(Name="test-rule")
        lam.delete_function.assert_called_once_with(FunctionName="test-lambda")

    def test_handles_no_targets(self) -> None:
        """Should handle rules with no targets."""
        events = MagicMock()
        lam = MagicMock()
        events.list_targets_by_rule.return_value = {"Targets": []}

        result = delete_retry_automation(events, lam, "test-rule", "test-lambda")
        assert result is True
        events.remove_targets.assert_not_called()

    def test_handles_error(self) -> None:
        """Should return False on error."""
        events = MagicMock()
        lam = MagicMock()
        events.list_targets_by_rule.side_effect = Exception("not found")

        result = delete_retry_automation(events, lam, "test-rule", "test-lambda")
        assert result is False


class TestSamTemplate:
    """Tests for SAM template generation."""

    def test_valid_yaml(self) -> None:
        """Generated template should be valid YAML."""
        template_str = generate_sam_template()
        template = yaml.safe_load(template_str)
        assert template["AWSTemplateFormatVersion"] == "2010-09-09"
        assert template["Transform"] == "AWS::Serverless-2016-10-31"
        assert "RetryFunction" in template["Resources"]
        assert "Outputs" in template

    def test_custom_config_reflected(self) -> None:
        """Custom config should appear in the template."""
        config = RetryConfig(max_retries=5, lambda_memory_mb=512)
        template_str = generate_sam_template(config)
        template = yaml.safe_load(template_str)
        assert template["Parameters"]["MaxRetries"]["Default"] == 5
        assert template["Globals"]["Function"]["MemorySize"] == 512

    def test_contains_event_pattern(self) -> None:
        """Template should include the EventBridge event pattern."""
        template_str = generate_sam_template()
        template = yaml.safe_load(template_str)
        events = template["Resources"]["RetryFunction"]["Properties"]["Events"]
        pattern = events["BlueGreenFailure"]["Properties"]["Pattern"]
        assert pattern["source"] == ["aws.rds"]
