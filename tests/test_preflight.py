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

    # TODO: Add tests once implementation is complete:
    # - test_valid_upgrade_path_passes
    # - test_invalid_upgrade_path_fails (e.g., PG 16 -> PG 13)
    # - test_same_version_fails
    # - test_aurora_cluster_version_check


class TestCheckParameterCompatibility:
    """Tests for the check_parameter_compatibility function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_parameter_compatibility(mock_rds_client, "test-instance", "16")
        assert isinstance(result, CheckResult)
        assert result.name == "Parameter Compatibility"

    # TODO: Add tests once implementation is complete:
    # - test_incompatible_params_detected (use mock_rds_client_with_incompatible_params)
    # - test_compatible_params_pass
    # - test_default_params_not_flagged (only user-modified params should be checked)


class TestCheckStorageSpace:
    """Tests for the check_storage_space function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_storage_space(mock_rds_client, "test-instance")
        assert isinstance(result, CheckResult)
        assert result.name == "Storage Space"

    # TODO: Add tests once implementation is complete:
    # - test_low_storage_warns
    # - test_critical_storage_fails
    # - test_ample_storage_passes


class TestCheckBlueGreenSupport:
    """Tests for the check_blue_green_support function."""

    def test_returns_check_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a CheckResult object."""
        result = check_blue_green_support(mock_rds_client, "test-instance")
        assert isinstance(result, CheckResult)
        assert result.name == "Blue-Green Support"

    # TODO: Add tests once implementation is complete:
    # - test_supported_instance_passes
    # - test_unsupported_engine_fails
    # - test_cross_region_replica_fails


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
