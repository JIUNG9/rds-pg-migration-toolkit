"""
Unit tests for pre-flight checks.

Tests the preflight module's ability to detect incompatible parameters,
validate upgrade paths, and generate accurate reports.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rds_migrate.preflight import (
    CheckResult,
    CheckStatus,
    PreflightReport,
    REMOVED_PARAMETERS,
    check_blue_green_support,
    check_current_version,
    check_extension_compatibility,
    check_parameter_compatibility,
    check_storage_space,
    run_preflight_checks,
)


class TestPreflightReport:
    """Tests for the PreflightReport dataclass."""

    def test_report_passes_when_all_checks_pass(self) -> None:
        """Report should pass when no checks have FAIL status."""
        report = PreflightReport(
            instance_id="test-db",
            target_version="16",
            checks=[
                CheckResult("Check 1", CheckStatus.PASS, "OK"),
                CheckResult("Check 2", CheckStatus.PASS, "OK"),
                CheckResult("Check 3", CheckStatus.WARN, "Minor issue"),
            ],
        )
        assert report.passed is True
        assert report.has_warnings is True

    def test_report_fails_when_any_check_fails(self) -> None:
        """Report should fail if any check has FAIL status."""
        report = PreflightReport(
            instance_id="test-db",
            target_version="16",
            checks=[
                CheckResult("Check 1", CheckStatus.PASS, "OK"),
                CheckResult("Check 2", CheckStatus.FAIL, "Incompatible parameter"),
            ],
        )
        assert report.passed is False

    def test_report_passes_with_skip_status(self) -> None:
        """Skipped checks should not cause failure."""
        report = PreflightReport(
            instance_id="test-db",
            target_version="16",
            checks=[
                CheckResult("Check 1", CheckStatus.PASS, "OK"),
                CheckResult("Check 2", CheckStatus.SKIP, "Not applicable"),
            ],
        )
        assert report.passed is True

    def test_empty_report_passes(self) -> None:
        """Report with no checks should pass (vacuous truth)."""
        report = PreflightReport(instance_id="test-db", target_version="16")
        assert report.passed is True
        assert report.has_warnings is False


class TestRemovedParameters:
    """Tests for the REMOVED_PARAMETERS constant."""

    def test_pg13_to_pg16_has_removed_params(self) -> None:
        """PG 13 -> 16 should have max_logical_replication_workers."""
        removed = REMOVED_PARAMETERS.get((13, 16), [])
        assert "max_logical_replication_workers" in removed

    def test_pg14_to_pg16_has_removed_params(self) -> None:
        """PG 14 -> 16 should also flag max_logical_replication_workers."""
        removed = REMOVED_PARAMETERS.get((14, 16), [])
        assert "max_logical_replication_workers" in removed


class TestCheckCurrentVersion:
    """Tests for the check_current_version function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_current_version(mock_rds_client, "test-instance", "16")
        assert isinstance(result, CheckResult)
        assert result.name == "PG Version Check"

    def test_valid_upgrade_path_passes(self, mock_rds_client: MagicMock) -> None:
        """PG 13 -> 16 should pass (mock returns 13.12)."""
        result = check_current_version(mock_rds_client, "test-instance", "16")
        assert result.status == CheckStatus.PASS
        assert "13 -> 16" in result.message

    def test_invalid_upgrade_path_fails(self, mock_rds_client: MagicMock) -> None:
        """Downgrade PG 13 -> 11 should fail."""
        result = check_current_version(mock_rds_client, "test-instance", "11")
        assert result.status == CheckStatus.FAIL
        assert "not supported" in result.message

    def test_same_version_passes_already_on_target(self, mock_rds_client: MagicMock) -> None:
        """Same version should pass with 'already on target' message."""
        result = check_current_version(mock_rds_client, "test-instance", "13")
        assert result.status == CheckStatus.PASS
        assert "Already on target" in result.message

    def test_aurora_cluster_version_check(self, mock_rds_client: MagicMock) -> None:
        """Should fall back to describe_db_clusters for Aurora."""
        # Make describe_db_instances fail
        mock_rds_client.describe_db_instances.side_effect = Exception("DBInstanceNotFound")
        result = check_current_version(mock_rds_client, "test-aurora-cluster", "16")
        assert result.status == CheckStatus.PASS
        assert "13 -> 16" in result.message


class TestCheckParameterCompatibility:
    """Tests for the check_parameter_compatibility function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_parameter_compatibility(mock_rds_client, "test-instance", "16")
        assert isinstance(result, CheckResult)
        assert result.name == "Parameter Compatibility"

    def test_compatible_params_pass(self, mock_rds_client: MagicMock) -> None:
        """Parameters without removed entries should pass."""
        result = check_parameter_compatibility(mock_rds_client, "test-instance", "16")
        assert result.status == CheckStatus.PASS
        assert "compatible" in result.message.lower()

    def test_incompatible_params_detected(
        self, mock_rds_client_with_incompatible_params: MagicMock
    ) -> None:
        """max_logical_replication_workers should trigger FAIL for PG 16."""
        result = check_parameter_compatibility(
            mock_rds_client_with_incompatible_params, "test-instance", "16"
        )
        assert result.status == CheckStatus.FAIL
        assert "max_logical_replication_workers" in result.message
        assert "incompatible_params" in result.details

    def test_no_removals_for_unsupported_version_pair(self, mock_rds_client: MagicMock) -> None:
        """Version pair with no known removals should pass."""
        result = check_parameter_compatibility(mock_rds_client, "test-instance", "14")
        assert result.status == CheckStatus.PASS


class TestCheckStorageSpace:
    """Tests for the check_storage_space function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_storage_space(mock_rds_client, "test-instance")
        assert isinstance(result, CheckResult)
        assert result.name == "Storage Space"

    def test_aurora_cluster_always_passes(self, mock_rds_client: MagicMock) -> None:
        """Aurora clusters use auto-scaling storage — always pass."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{
                "DBInstanceIdentifier": "test-aurora",
                "Engine": "aurora-postgresql",
                "EngineVersion": "13.12",
                "AllocatedStorage": 0,
                "DBInstanceStatus": "available",
                "DBParameterGroups": [{"DBParameterGroupName": "default", "ParameterApplyStatus": "in-sync"}],
            }]
        }
        result = check_storage_space(mock_rds_client, "test-aurora")
        assert result.status == CheckStatus.PASS
        assert "auto-scaling" in result.message


class TestCheckBlueGreenSupport:
    """Tests for the check_blue_green_support function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_blue_green_support(mock_rds_client, "test-instance")
        assert isinstance(result, CheckResult)
        assert result.name == "Blue-Green Support"

    def test_supported_instance_passes(self, mock_rds_client: MagicMock) -> None:
        """PG 13, available, no cross-region replicas should pass."""
        result = check_blue_green_support(mock_rds_client, "test-instance")
        assert result.status == CheckStatus.PASS
        assert "eligible" in result.message

    def test_unsupported_engine_fails(self, mock_rds_client: MagicMock) -> None:
        """MySQL engine should fail."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{
                "DBInstanceIdentifier": "mysql-db",
                "Engine": "mysql",
                "EngineVersion": "8.0.35",
                "DBInstanceStatus": "available",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:mysql-db",
                "AllocatedStorage": 100,
                "DBParameterGroups": [{"DBParameterGroupName": "default", "ParameterApplyStatus": "in-sync"}],
            }]
        }
        result = check_blue_green_support(mock_rds_client, "mysql-db")
        assert result.status == CheckStatus.FAIL
        assert "mysql" in result.message.lower()

    def test_old_version_fails(self, mock_rds_client: MagicMock) -> None:
        """PG 11 should fail (blue-green requires >= 12)."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{
                "DBInstanceIdentifier": "old-pg",
                "Engine": "postgres",
                "EngineVersion": "11.22",
                "DBInstanceStatus": "available",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:old-pg",
                "AllocatedStorage": 100,
                "DBParameterGroups": [{"DBParameterGroupName": "default", "ParameterApplyStatus": "in-sync"}],
            }]
        }
        result = check_blue_green_support(mock_rds_client, "old-pg")
        assert result.status == CheckStatus.FAIL
        assert ">= 12" in result.message

    def test_unavailable_instance_fails(self, mock_rds_client: MagicMock) -> None:
        """Instance not in 'available' status should fail."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{
                "DBInstanceIdentifier": "modifying-db",
                "Engine": "postgres",
                "EngineVersion": "13.12",
                "DBInstanceStatus": "modifying",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:modifying-db",
                "AllocatedStorage": 100,
                "DBParameterGroups": [{"DBParameterGroupName": "default", "ParameterApplyStatus": "in-sync"}],
            }]
        }
        result = check_blue_green_support(mock_rds_client, "modifying-db")
        assert result.status == CheckStatus.FAIL
        assert "modifying" in result.message


class TestRunPreflightChecks:
    """Tests for the run_preflight_checks orchestrator."""

    def test_returns_report_with_all_checks(self, mock_rds_client: MagicMock) -> None:
        """Should return a report containing all five checks."""
        report = run_preflight_checks(mock_rds_client, "test-instance", "16")
        assert isinstance(report, PreflightReport)
        assert len(report.checks) == 5
        assert report.instance_id == "test-instance"
        assert report.target_version == "16"

    def test_report_check_names(self, mock_rds_client: MagicMock) -> None:
        """All expected check names should be present."""
        report = run_preflight_checks(mock_rds_client, "test-instance", "16")
        check_names = {c.name for c in report.checks}
        expected_names = {
            "PG Version Check",
            "Parameter Compatibility",
            "Extension Compatibility",
            "Storage Space",
            "Blue-Green Support",
        }
        assert check_names == expected_names
