"""
Unit tests for migration logic.

Tests the migrate module's blue-green deployment creation, monitoring,
and rollback functionality.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rds_migrate.migrate import (
    DeploymentResult,
    DeploymentStatus,
    _create_target_parameter_group,
    _is_aurora_cluster,
    _poll_deployment_status,
    create_blue_green_deployment,
    rollback_deployment,
)
from rds_migrate.utils import validate_upgrade_path


class TestDeploymentResult:
    """Tests for the DeploymentResult dataclass."""

    def test_str_representation(self) -> None:
        """Should produce a readable string representation."""
        result = DeploymentResult(
            deployment_id="bgd-12345",
            status=DeploymentStatus.AVAILABLE,
            source_instance="test-db",
            target_version="16",
            message="Deployment ready for switchover.",
            green_instance_id="test-db-green",
        )
        output = str(result)
        assert "bgd-12345" in output
        assert "AVAILABLE" in output
        assert "test-db" in output
        assert "test-db-green" in output

    def test_str_without_optional_fields(self) -> None:
        """Should handle missing optional fields gracefully."""
        result = DeploymentResult(
            deployment_id="bgd-12345",
            status=DeploymentStatus.PROVISIONING,
            source_instance="test-db",
            target_version="16",
            message="Creating...",
        )
        output = str(result)
        assert "Green ID" not in output
        assert "Switchover" not in output


class TestDeploymentStatus:
    """Tests for the DeploymentStatus enum."""

    def test_all_statuses_exist(self) -> None:
        """All expected deployment statuses should be defined."""
        expected = {
            "PROVISIONING",
            "AVAILABLE",
            "SWITCHOVER_IN_PROGRESS",
            "SWITCHOVER_COMPLETED",
            "DELETE_IN_PROGRESS",
            "DELETED",
            "FAILED",
        }
        actual = {s.value for s in DeploymentStatus}
        assert actual == expected


class TestIsAuroraCluster:
    """Tests for Aurora cluster detection."""

    def test_returns_bool(self, mock_rds_client: MagicMock) -> None:
        """Should return a boolean."""
        result = _is_aurora_cluster(mock_rds_client, "test-instance")
        assert isinstance(result, bool)

    def test_aurora_cluster_detected(self, mock_rds_client: MagicMock) -> None:
        """Should return True when describe_db_clusters returns results."""
        result = _is_aurora_cluster(mock_rds_client, "test-aurora-cluster")
        assert result is True

    def test_standalone_rds_detected(self, mock_rds_client: MagicMock) -> None:
        """Should return False when describe_db_clusters finds nothing."""
        mock_rds_client.describe_db_clusters.side_effect = Exception("DBClusterNotFound")
        result = _is_aurora_cluster(mock_rds_client, "test-instance")
        assert result is False


class TestCreateTargetParameterGroup:
    """Tests for the parameter group creation helper."""

    def test_filters_incompatible_params(self, mock_rds_client: MagicMock) -> None:
        """Should filter max_logical_replication_workers for PG 13 -> 16."""
        mock_rds_client.describe_db_parameters.return_value = {
            "Parameters": [
                {"ParameterName": "max_connections", "ParameterValue": "200", "ApplyMethod": "pending-reboot"},
                {"ParameterName": "max_logical_replication_workers", "ParameterValue": "4", "ApplyMethod": "pending-reboot"},
            ]
        }
        name = _create_target_parameter_group(
            mock_rds_client, "source-pg13", 13, 16, "bgd-test", is_cluster=False,
        )
        assert name == "bgd-test-pg16"
        # Verify only max_connections was copied (incompatible param filtered)
        call_args = mock_rds_client.modify_db_parameter_group.call_args
        copied_params = call_args.kwargs["Parameters"]
        param_names = [p["ParameterName"] for p in copied_params]
        assert "max_connections" in param_names
        assert "max_logical_replication_workers" not in param_names


class TestCreateBlueGreenDeployment:
    """Tests for the create_blue_green_deployment function."""

    def test_detects_aurora_and_delegates(self, mock_rds_client: MagicMock) -> None:
        """Aurora cluster should use cluster-level deployment."""
        # Setup: mock the blue-green response
        mock_rds_client.create_blue_green_deployment.return_value = {
            "BlueGreenDeployment": {
                "BlueGreenDeploymentIdentifier": "bgd-aurora-123",
                "Status": "PROVISIONING",
            }
        }
        mock_rds_client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [{"EngineVersion": "16.4"}]
        }
        mock_rds_client.describe_db_cluster_parameters.return_value = {"Parameters": []}

        result = create_blue_green_deployment(
            mock_rds_client, "test-aurora-cluster", "16", wait=False, switchover=False,
        )
        assert result.deployment_id == "bgd-aurora-123"
        assert result.source_instance == "test-aurora-cluster"

    def test_rds_deployment_created(self, mock_rds_client: MagicMock) -> None:
        """Standalone RDS should use instance-level deployment."""
        mock_rds_client.describe_db_clusters.side_effect = Exception("DBClusterNotFound")
        mock_rds_client.create_blue_green_deployment.return_value = {
            "BlueGreenDeployment": {
                "BlueGreenDeploymentIdentifier": "bgd-rds-456",
                "Status": "PROVISIONING",
            }
        }
        mock_rds_client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [{"EngineVersion": "16.4"}]
        }

        result = create_blue_green_deployment(
            mock_rds_client, "test-instance", "16", wait=False, switchover=False,
        )
        assert result.deployment_id == "bgd-rds-456"
        assert result.source_instance == "test-instance"


class TestPollDeploymentStatus:
    """Tests for the deployment polling function."""

    @patch("rds_migrate.migrate.time.sleep", return_value=None)
    def test_poll_reaches_available(self, mock_sleep: MagicMock, mock_rds_client: MagicMock) -> None:
        """Should return AVAILABLE when deployment becomes ready."""
        mock_rds_client.describe_blue_green_deployments.side_effect = [
            {"BlueGreenDeployments": [{"Status": "PROVISIONING"}]},
            {"BlueGreenDeployments": [{"Status": "PROVISIONING"}]},
            {"BlueGreenDeployments": [{"Status": "AVAILABLE"}]},
        ]
        status = _poll_deployment_status(mock_rds_client, "bgd-123", timeout=300, poll_interval=1)
        assert status == DeploymentStatus.AVAILABLE

    @patch("rds_migrate.migrate.time.sleep", return_value=None)
    @patch("rds_migrate.migrate.time.time")
    def test_poll_timeout(self, mock_time: MagicMock, mock_sleep: MagicMock, mock_rds_client: MagicMock) -> None:
        """Should raise TimeoutError when timeout is exceeded."""
        mock_time.side_effect = [0, 0, 50, 100, 200, 400]
        mock_rds_client.describe_blue_green_deployments.return_value = {
            "BlueGreenDeployments": [{"Status": "PROVISIONING"}]
        }
        with pytest.raises(TimeoutError, match="did not complete"):
            _poll_deployment_status(mock_rds_client, "bgd-123", timeout=100, poll_interval=1)

    @patch("rds_migrate.migrate.time.sleep", return_value=None)
    def test_poll_detects_failure(self, mock_sleep: MagicMock, mock_rds_client: MagicMock) -> None:
        """Should return FAILED status without error."""
        mock_rds_client.describe_blue_green_deployments.return_value = {
            "BlueGreenDeployments": [{"Status": "FAILED"}]
        }
        status = _poll_deployment_status(mock_rds_client, "bgd-123", timeout=300, poll_interval=1)
        assert status == DeploymentStatus.FAILED


class TestRollbackDeployment:
    """Tests for the rollback_deployment function."""

    def test_returns_deployment_result(self, mock_rds_client: MagicMock) -> None:
        """Should return a DeploymentResult object."""
        result = rollback_deployment(
            rds_client=mock_rds_client,
            deployment_id="bgd-12345",
            delete_green=True,
        )
        assert isinstance(result, DeploymentResult)
        assert result.deployment_id == "bgd-12345"

    def test_green_environment_deleted(self, mock_rds_client: MagicMock) -> None:
        """Should call delete with DeleteTarget=True and return DELETED."""
        result = rollback_deployment(mock_rds_client, "bgd-12345", delete_green=True)
        mock_rds_client.delete_blue_green_deployment.assert_called_once_with(
            BlueGreenDeploymentIdentifier="bgd-12345",
            DeleteTarget=True,
        )
        assert result.status == DeploymentStatus.DELETED

    def test_rollback_failure_handled(self, mock_rds_client: MagicMock) -> None:
        """Should handle deletion errors gracefully."""
        mock_rds_client.delete_blue_green_deployment.side_effect = Exception("AccessDenied")
        result = rollback_deployment(mock_rds_client, "bgd-12345")
        assert result.status == DeploymentStatus.FAILED
        assert "Rollback failed" in result.message


class TestValidateUpgradePath:
    """Tests for the upgrade path validation utility."""

    def test_pg13_to_pg16_supported(self) -> None:
        assert validate_upgrade_path(13, 16) is True

    def test_pg13_to_pg14_supported(self) -> None:
        assert validate_upgrade_path(13, 14) is True

    def test_pg15_to_pg16_supported(self) -> None:
        assert validate_upgrade_path(15, 16) is True

    def test_downgrade_not_supported(self) -> None:
        assert validate_upgrade_path(16, 13) is False

    def test_same_version_not_supported(self) -> None:
        assert validate_upgrade_path(13, 13) is False

    def test_unsupported_source_version(self) -> None:
        assert validate_upgrade_path(11, 16) is False
