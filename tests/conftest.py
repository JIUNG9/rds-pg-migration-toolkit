"""
Pytest fixtures for rds-pg-migration-toolkit tests.

Provides:
  - Mock RDS client with common responses
  - Sample instance configurations
  - Temporary configuration files
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_rds_client() -> MagicMock:
    """Create a mock boto3 RDS client with common responses.

    Returns:
        MagicMock configured as an RDS client.
    """
    client = MagicMock()

    # Default describe_db_instances response
    client.describe_db_instances.return_value = {
        "DBInstances": [
            {
                "DBInstanceIdentifier": "test-instance",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:test-instance",
                "Engine": "postgres",
                "EngineVersion": "13.12",
                "DBInstanceClass": "db.r6g.large",
                "DBInstanceStatus": "available",
                "AllocatedStorage": 100,
                "DBParameterGroups": [
                    {
                        "DBParameterGroupName": "custom-pg13",
                        "ParameterApplyStatus": "in-sync",
                    }
                ],
                "MultiAZ": True,
                "StorageType": "gp3",
            }
        ]
    }

    # Default describe_db_clusters response (for Aurora)
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DBClusterIdentifier": "test-aurora-cluster",
                "DBClusterArn": "arn:aws:rds:us-east-1:123456789012:cluster:test-aurora-cluster",
                "Engine": "aurora-postgresql",
                "EngineVersion": "13.12",
                "Status": "available",
                "DBClusterMembers": [
                    {
                        "DBInstanceIdentifier": "test-aurora-writer",
                        "IsClusterWriter": True,
                    },
                    {
                        "DBInstanceIdentifier": "test-aurora-reader-1",
                        "IsClusterWriter": False,
                    },
                ],
                "DBClusterParameterGroup": "custom-aurora-pg13",
            }
        ]
    }

    # Default describe_db_parameters response
    client.describe_db_parameters.return_value = {
        "Parameters": [
            {
                "ParameterName": "max_connections",
                "ParameterValue": "200",
                "ApplyMethod": "pending-reboot",
                "Source": "user",
            },
            {
                "ParameterName": "shared_buffers",
                "ParameterValue": "{DBInstanceClassMemory/4}",
                "ApplyMethod": "pending-reboot",
                "Source": "user",
            },
        ]
    }

    return client


@pytest.fixture
def mock_rds_client_with_incompatible_params(mock_rds_client: MagicMock) -> MagicMock:
    """RDS client with parameter group containing PG 16-incompatible parameters.

    Returns:
        MagicMock with max_logical_replication_workers in the parameter group.
    """
    mock_rds_client.describe_db_parameters.return_value = {
        "Parameters": [
            {
                "ParameterName": "max_connections",
                "ParameterValue": "200",
                "ApplyMethod": "pending-reboot",
                "Source": "user",
            },
            {
                "ParameterName": "max_logical_replication_workers",
                "ParameterValue": "4",
                "ApplyMethod": "pending-reboot",
                "Source": "user",
            },
        ]
    }
    return mock_rds_client


@pytest.fixture
def sample_aurora_cluster_config() -> dict[str, Any]:
    """Sample Aurora cluster configuration for testing.

    Returns:
        Dictionary mimicking a describe_db_clusters response item.
    """
    return {
        "DBClusterIdentifier": "prod-aurora-pg13",
        "DBClusterArn": "arn:aws:rds:us-east-1:123456789012:cluster:prod-aurora-pg13",
        "Engine": "aurora-postgresql",
        "EngineVersion": "13.12",
        "Status": "available",
        "AllocatedStorage": 500,
        "DBClusterMembers": [
            {"DBInstanceIdentifier": "prod-aurora-writer", "IsClusterWriter": True},
            {"DBInstanceIdentifier": "prod-aurora-reader-1", "IsClusterWriter": False},
            {"DBInstanceIdentifier": "prod-aurora-reader-2", "IsClusterWriter": False},
        ],
        "DBClusterParameterGroup": "prod-aurora-pg13-params",
    }


@pytest.fixture
def sample_rds_instance_config() -> dict[str, Any]:
    """Sample standalone RDS instance configuration for testing.

    Returns:
        Dictionary mimicking a describe_db_instances response item.
    """
    return {
        "DBInstanceIdentifier": "prod-rds-pg13",
        "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:prod-rds-pg13",
        "Engine": "postgres",
        "EngineVersion": "13.12",
        "DBInstanceClass": "db.r6g.xlarge",
        "DBInstanceStatus": "available",
        "AllocatedStorage": 200,
        "MultiAZ": True,
        "StorageType": "gp3",
        "DBParameterGroups": [
            {
                "DBParameterGroupName": "prod-pg13-params",
                "ParameterApplyStatus": "in-sync",
            }
        ],
    }


@pytest.fixture
def tmp_config_file(tmp_path: Any) -> str:
    """Create a temporary YAML configuration file.

    Returns:
        Path to the temporary config file.
    """
    config_content = """
region: us-east-1
profile: default
poll_interval_seconds: 10
deployment_timeout_seconds: 1800
storage_warning_threshold_pct: 75
"""
    config_path = tmp_path / "rds-migrate.yml"
    config_path.write_text(config_content)
    return str(config_path)
