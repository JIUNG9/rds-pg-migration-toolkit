"""
Unit tests for shared utility functions.

Tests ARN resolution, engine version resolution, config loading,
and upgrade path validation edge cases.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rds_migrate.utils import (
    get_instance_arn,
    load_config,
    resolve_engine_version,
    validate_upgrade_path,
    DEFAULTS,
)


class TestGetInstanceArn:
    """Tests for the get_instance_arn function."""

    def test_resolves_rds_instance_arn(self, mock_rds_client: MagicMock) -> None:
        """Should return DBInstanceArn for a standalone RDS instance."""
        arn = get_instance_arn(mock_rds_client, "test-instance")
        assert arn == "arn:aws:rds:us-east-1:123456789012:db:test-instance"

    def test_falls_back_to_aurora_cluster(self, mock_rds_client: MagicMock) -> None:
        """Should return DBClusterArn when instance lookup fails."""
        mock_rds_client.describe_db_instances.side_effect = Exception("DBInstanceNotFound")
        arn = get_instance_arn(mock_rds_client, "test-aurora-cluster")
        assert arn == "arn:aws:rds:us-east-1:123456789012:cluster:test-aurora-cluster"

    def test_raises_for_nonexistent(self, mock_rds_client: MagicMock) -> None:
        """Should raise ValueError when neither instance nor cluster found."""
        mock_rds_client.describe_db_instances.side_effect = Exception("DBInstanceNotFound")
        mock_rds_client.describe_db_clusters.return_value = {"DBClusters": []}
        with pytest.raises(ValueError, match="not found"):
            get_instance_arn(mock_rds_client, "ghost-db")


class TestResolveEngineVersion:
    """Tests for the resolve_engine_version function."""

    def test_resolves_latest_minor_version(self) -> None:
        """Should return the latest minor version for a major version."""
        client = MagicMock()
        client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [
                {"EngineVersion": "16.1"},
                {"EngineVersion": "16.3"},
                {"EngineVersion": "16.2"},
                {"EngineVersion": "16.12"},
                {"EngineVersion": "15.8"},
            ]
        }
        result = resolve_engine_version(client, "16")
        assert result == "16.12"

    def test_raises_when_no_versions_found(self) -> None:
        """Should raise ValueError when no matching versions exist."""
        client = MagicMock()
        client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [
                {"EngineVersion": "15.8"},
                {"EngineVersion": "14.12"},
            ]
        }
        with pytest.raises(ValueError, match="No engine versions found"):
            resolve_engine_version(client, "17")

    def test_handles_pagination(self) -> None:
        """Should follow pagination markers."""
        client = MagicMock()
        client.describe_db_engine_versions.side_effect = [
            {
                "DBEngineVersions": [{"EngineVersion": "16.1"}],
                "Marker": "page2",
            },
            {
                "DBEngineVersions": [{"EngineVersion": "16.4"}],
            },
        ]
        result = resolve_engine_version(client, "16")
        assert result == "16.4"
        assert client.describe_db_engine_versions.call_count == 2


class TestLoadConfig:
    """Tests for the load_config function."""

    def test_loads_from_explicit_path(self, tmp_config_file: str) -> None:
        """Should load config from the specified path."""
        config = load_config(tmp_config_file)
        assert config["poll_interval_seconds"] == 10
        assert config["deployment_timeout_seconds"] == 1800

    def test_returns_defaults_when_no_file(self) -> None:
        """Should return defaults when no config file is found."""
        config = load_config()
        assert config == DEFAULTS

    def test_raises_for_missing_file(self) -> None:
        """Should raise FileNotFoundError for nonexistent explicit path."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yml")

    def test_defaults_merged_with_user_config(self, tmp_config_file: str) -> None:
        """User config should override defaults, not replace them entirely."""
        config = load_config(tmp_config_file)
        # User-set values
        assert config["storage_warning_threshold_pct"] == 75
        # Default values still present
        assert config["bloat_warning_threshold_pct"] == 20
        assert config["reindex_size_threshold_mb"] == 1024


class TestValidateUpgradePathEdgeCases:
    """Additional edge case tests for validate_upgrade_path."""

    def test_pg12_to_pg16_supported(self) -> None:
        """PG 12 -> 16 should be supported (skip-version upgrade)."""
        assert validate_upgrade_path(12, 16) is True

    def test_pg12_to_pg13_supported(self) -> None:
        """PG 12 -> 13 should be supported (single step)."""
        assert validate_upgrade_path(12, 13) is True

    def test_pg16_has_no_upgrade_targets(self) -> None:
        """PG 16 (latest) should have no further upgrade targets."""
        assert validate_upgrade_path(16, 17) is False

    def test_pg10_unsupported(self) -> None:
        """PG 10 is EOL and not in the supported path list."""
        assert validate_upgrade_path(10, 16) is False
