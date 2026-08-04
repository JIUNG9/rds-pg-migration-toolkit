"""
Shared utilities for rds-pg-migration-toolkit.

Provides:
  - AWS client factory (with profile and region support)
  - Logging configuration
  - Configuration file loading (YAML)
  - Common constants and helpers
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import boto3
import yaml

logger = logging.getLogger("rds_migrate")

# Supported PostgreSQL major version upgrade paths on AWS RDS.
# Source version -> list of supported target versions.
SUPPORTED_UPGRADE_PATHS: dict[int, list[int]] = {
    12: [13, 14, 15, 16],
    13: [14, 15, 16],
    14: [15, 16],
    15: [16],
}

# Default configuration values.
DEFAULTS: dict[str, Any] = {
    "region": "us-east-1",
    "profile": "default",
    "poll_interval_seconds": 30,
    "deployment_timeout_seconds": 3600,
    "storage_warning_threshold_pct": 80,
    "storage_critical_threshold_pct": 95,
    "bloat_warning_threshold_pct": 20,
    "reindex_size_threshold_mb": 1024,
}


def configure_logging(verbose: bool = False) -> None:
    """Configure logging for the toolkit.

    Args:
        verbose: If True, set log level to DEBUG. Otherwise, INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from boto3 and botocore
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_rds_client(
    profile: str = "default",
    region: str = "us-east-1",
) -> Any:
    """Create a boto3 RDS client with the specified profile and region.

    Args:
        profile: AWS CLI profile name.
        region: AWS region.

    Returns:
        boto3 RDS client.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("rds")


def get_events_client(
    profile: str = "default",
    region: str = "us-east-1",
) -> Any:
    """Create a boto3 EventBridge client.

    Args:
        profile: AWS CLI profile name.
        region: AWS region.

    Returns:
        boto3 EventBridge client.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("events")


def get_lambda_client(
    profile: str = "default",
    region: str = "us-east-1",
) -> Any:
    """Create a boto3 Lambda client.

    Args:
        profile: AWS CLI profile name.
        region: AWS region.

    Returns:
        boto3 Lambda client.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("lambda")


def get_cloudwatch_client(
    profile: str = "default",
    region: str = "us-east-1",
) -> Any:
    """Create a boto3 CloudWatch client.

    Args:
        profile: AWS CLI profile name.
        region: AWS region.

    Returns:
        boto3 CloudWatch client.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("cloudwatch")


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load configuration from a YAML file.

    If no path is provided, looks for a config file in the following order:
      1. ./rds-migrate.yml
      2. ~/.config/rds-migrate/config.yml

    Args:
        config_path: Explicit path to a YAML config file.

    Returns:
        Configuration dictionary. Returns DEFAULTS if no config file found.
    """
    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            user_config = yaml.safe_load(f) or {}
        return {**DEFAULTS, **user_config}

    # Search default locations
    search_paths = [
        Path.cwd() / "rds-migrate.yml",
        Path.home() / ".config" / "rds-migrate" / "config.yml",
    ]

    for path in search_paths:
        if path.exists():
            logger.debug("Loading config from %s", path)
            with open(path) as f:
                user_config = yaml.safe_load(f) or {}
            return {**DEFAULTS, **user_config}

    logger.debug("No config file found, using defaults.")
    return DEFAULTS.copy()


def resolve_engine_version(
    rds_client: Any,
    target_major_version: str,
    engine: str = "postgres",
) -> str:
    """Resolve a major version number to the latest available minor version.

    For example, "16" might resolve to "16.3" based on what is currently
    available in the AWS region.

    Args:
        rds_client: boto3 RDS client.
        target_major_version: Major version string (e.g., "16").
        engine: Database engine (default: "postgres").

    Returns:
        Full engine version string (e.g., "16.3").

    Raises:
        ValueError: If no matching engine version is found.
    """
    logger.info("Resolving engine version for %s %s", engine, target_major_version)

    versions: list[str] = []
    paginator_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Engine": engine}
        if paginator_token:
            kwargs["Marker"] = paginator_token
        resp = rds_client.describe_db_engine_versions(**kwargs)
        for v in resp.get("DBEngineVersions", []):
            ver = v["EngineVersion"]
            if ver.startswith(f"{target_major_version}."):
                versions.append(ver)
        paginator_token = resp.get("Marker")
        if not paginator_token:
            break

    if not versions:
        raise ValueError(
            f"No engine versions found for {engine} {target_major_version}.x "
            f"in the current region."
        )

    # Sort by minor version numerically (e.g., 16.1, 16.2, 16.12)
    def _sort_key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    versions.sort(key=_sort_key)
    latest = versions[-1]
    logger.info("Resolved %s %s -> %s", engine, target_major_version, latest)
    return latest


def get_instance_arn(
    rds_client: Any,
    instance_id: str,
) -> str:
    """Get the ARN of an RDS instance or Aurora cluster.

    Tries describe_db_instances first, then describe_db_clusters.

    Args:
        rds_client: boto3 RDS client.
        instance_id: Instance or cluster identifier.

    Returns:
        ARN string.

    Raises:
        ValueError: If the instance/cluster is not found.
    """
    logger.info("Resolving ARN for %s", instance_id)

    # Try standalone RDS instance first
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        arn = str(resp["DBInstances"][0]["DBInstanceArn"])
        logger.debug("Resolved %s as RDS instance: %s", instance_id, arn)
        return arn
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            raise

    # Fall back to Aurora cluster
    try:
        resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
        arn = str(resp["DBClusters"][0]["DBClusterArn"])
        logger.debug("Resolved %s as Aurora cluster: %s", instance_id, arn)
        return arn
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBClusterNotFound" not in str(exc):
            raise

    raise ValueError(
        f"Instance or cluster '{instance_id}' not found. "
        f"Verify the identifier and region."
    )


def validate_upgrade_path(source_major: int, target_major: int) -> bool:
    """Validate that the upgrade path is supported by AWS RDS.

    Args:
        source_major: Source PostgreSQL major version.
        target_major: Target PostgreSQL major version.

    Returns:
        True if the upgrade path is supported.
    """
    supported_targets = SUPPORTED_UPGRADE_PATHS.get(source_major, [])
    return target_major in supported_targets
