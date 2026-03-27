"""
Unit tests for post-migration verification.

Tests the verify module's ability to check PG version, connection status,
replication health, and basic query responsiveness after switchover.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rds_migrate.verify import (
    VerificationReport,
    VerifyCheck,
    VerifyStatus,
    run_verification,
    verify_basic_queries,
    verify_connections,
    verify_pg_version,
    verify_replication,
)


class TestVerificationReport:
    """Tests for the VerificationReport dataclass."""

    def test_passes_when_all_ok(self) -> None:
        """Report should pass when all checks are OK."""
        report = VerificationReport(
            instance_id="test-db",
            expected_version="16",
            checks=[
                VerifyCheck("Check 1", VerifyStatus.OK, "Good"),
                VerifyCheck("Check 2", VerifyStatus.OK, "Good"),
            ],
        )
        assert report.passed is True

    def test_fails_when_any_failed(self) -> None:
        """Report should fail if any check has FAILED status."""
        report = VerificationReport(
            instance_id="test-db",
            expected_version="16",
            checks=[
                VerifyCheck("Check 1", VerifyStatus.OK, "Good"),
                VerifyCheck("Check 2", VerifyStatus.FAILED, "Bad"),
            ],
        )
        assert report.passed is False

    def test_passes_with_degraded(self) -> None:
        """DEGRADED checks should not cause failure."""
        report = VerificationReport(
            instance_id="test-db",
            expected_version="16",
            checks=[
                VerifyCheck("Check 1", VerifyStatus.OK, "Good"),
                VerifyCheck("Check 2", VerifyStatus.DEGRADED, "Slow"),
            ],
        )
        assert report.passed is True

    def test_str_representation(self) -> None:
        """Should produce readable string output."""
        report = VerificationReport(
            instance_id="prod-db",
            expected_version="16",
            checks=[VerifyCheck("PG Version", VerifyStatus.OK, "16.4", latency_ms=12.5)],
        )
        output = str(report)
        assert "prod-db" in output
        assert "PASS" in output
        assert "16" in output


class TestVerifyPgVersion:
    """Tests for the verify_pg_version function."""

    def test_version_matches(self, mock_rds_client: MagicMock) -> None:
        """Should return OK when version matches expected."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"EngineVersion": "16.4"}]
        }
        result = verify_pg_version(mock_rds_client, "test-db", "16")
        assert result.status == VerifyStatus.OK
        assert "16.4" in result.message

    def test_version_mismatch(self, mock_rds_client: MagicMock) -> None:
        """Should return FAILED when version does not match."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"EngineVersion": "13.12"}]
        }
        result = verify_pg_version(mock_rds_client, "test-db", "16")
        assert result.status == VerifyStatus.FAILED
        assert "mismatch" in result.message.lower()

    def test_aurora_fallback(self, mock_rds_client: MagicMock) -> None:
        """Should fall back to describe_db_clusters for Aurora."""
        mock_rds_client.describe_db_instances.side_effect = Exception("DBInstanceNotFound")
        mock_rds_client.describe_db_clusters.return_value = {
            "DBClusters": [{"EngineVersion": "16.4"}]
        }
        result = verify_pg_version(mock_rds_client, "aurora-cluster", "16")
        assert result.status == VerifyStatus.OK

    def test_instance_not_found(self, mock_rds_client: MagicMock) -> None:
        """Should return FAILED when instance cannot be found."""
        mock_rds_client.describe_db_instances.side_effect = Exception("DBInstanceNotFound")
        mock_rds_client.describe_db_clusters.return_value = {"DBClusters": []}
        result = verify_pg_version(mock_rds_client, "ghost-db", "16")
        assert result.status == VerifyStatus.FAILED


class TestVerifyConnections:
    """Tests for the verify_connections function."""

    @patch("rds_migrate.verify.get_cloudwatch_client")
    def test_connections_healthy(self, mock_cw_factory: MagicMock, mock_rds_client: MagicMock) -> None:
        """Should return OK when instance is available with connections."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "available"}]
        }
        mock_cw = MagicMock()
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 42.0}]
        }
        mock_cw_factory.return_value = mock_cw
        result = verify_connections(mock_rds_client, "test-db")
        assert result.status == VerifyStatus.OK
        assert "42" in result.message

    def test_unavailable_instance(self, mock_rds_client: MagicMock) -> None:
        """Should return FAILED when instance is not available."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "modifying"}]
        }
        result = verify_connections(mock_rds_client, "test-db")
        assert result.status == VerifyStatus.FAILED
        assert "modifying" in result.message

    @patch("rds_migrate.verify.get_cloudwatch_client")
    def test_no_connections_degraded(self, mock_cw_factory: MagicMock, mock_rds_client: MagicMock) -> None:
        """Should return DEGRADED when no active connections."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceStatus": "available"}]
        }
        mock_cw = MagicMock()
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 0}]
        }
        mock_cw_factory.return_value = mock_cw
        result = verify_connections(mock_rds_client, "test-db")
        assert result.status == VerifyStatus.DEGRADED


class TestVerifyReplication:
    """Tests for the verify_replication function."""

    @patch("rds_migrate.verify.get_cloudwatch_client")
    def test_aurora_healthy(self, mock_cw_factory: MagicMock, mock_rds_client: MagicMock) -> None:
        """Should return OK for healthy Aurora cluster."""
        mock_rds_client.describe_db_clusters.return_value = {
            "DBClusters": [{
                "DBClusterMembers": [
                    {"DBInstanceIdentifier": "writer", "IsClusterWriter": True},
                    {"DBInstanceIdentifier": "reader-1", "IsClusterWriter": False},
                ],
            }]
        }
        mock_cw = MagicMock()
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Maximum": 5.0}]
        }
        mock_cw_factory.return_value = mock_cw
        result = verify_replication(mock_rds_client, "aurora-cluster")
        assert result.status == VerifyStatus.OK
        assert "1 writer" in result.message

    def test_standalone_no_replicas(self, mock_rds_client: MagicMock) -> None:
        """Should return OK for standalone RDS with no replicas."""
        mock_rds_client.describe_db_clusters.side_effect = Exception("DBClusterNotFound")
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"ReadReplicaDBInstanceIdentifiers": []}]
        }
        result = verify_replication(mock_rds_client, "standalone")
        assert result.status == VerifyStatus.OK
        assert "No read replicas" in result.message


class TestVerifyBasicQueries:
    """Tests for the verify_basic_queries function."""

    def test_ok_response(self, mock_rds_client: MagicMock) -> None:
        """Should return OK when API responds quickly."""
        result = verify_basic_queries(mock_rds_client, "test-db")
        assert result.status == VerifyStatus.OK
        assert result.latency_ms is not None


class TestRunVerification:
    """Tests for the run_verification orchestrator."""

    @patch("rds_migrate.verify.get_cloudwatch_client")
    def test_report_passes_all(
        self, mock_cw_factory: MagicMock, mock_rds_client: MagicMock
    ) -> None:
        """Full verification with healthy instance should have report.passed == True."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{
                "DBInstanceIdentifier": "test-db",
                "EngineVersion": "16.4",
                "DBInstanceStatus": "available",
            }]
        }
        mock_cw = MagicMock()
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 10.0}]
        }
        mock_cw_factory.return_value = mock_cw
        report = run_verification(mock_rds_client, "test-db", "16", check_connections=True)
        assert report.passed is True

    def test_includes_version_and_basic_query(self, mock_rds_client: MagicMock) -> None:
        """Should always include PG Version and Basic Query checks."""
        mock_rds_client.describe_db_instances.return_value = {
            "DBInstances": [{"EngineVersion": "16.4", "DBInstanceStatus": "available"}]
        }
        report = run_verification(
            mock_rds_client, "test-db", "16",
            check_connections=False, check_replication=False,
        )
        check_names = [c.name for c in report.checks]
        assert "PG Version" in check_names
        assert "Basic Query" in check_names
