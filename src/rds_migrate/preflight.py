"""
Pre-flight checks for PostgreSQL major version upgrades on RDS/Aurora.

Validates the following before initiating a blue-green deployment:
  1. Current PostgreSQL version
  2. Parameter group compatibility (removed/changed parameters)
  3. Extension compatibility with target version
  4. Available storage space
  5. Blue-green deployment support for the instance type

Based on real issues encountered migrating 49 instances from PG 13 to PG 16.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.console import Console
from rich.table import Table

from rds_migrate.utils import validate_upgrade_path

logger = logging.getLogger("rds_migrate")


class CheckStatus(Enum):
    """Status of a pre-flight check."""

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass
class CheckResult:
    """Result of a single pre-flight check."""

    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    """Aggregated report of all pre-flight checks."""

    instance_id: str
    target_version: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True if no checks failed."""
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        """Return True if any checks produced warnings."""
        return any(c.status == CheckStatus.WARN for c in self.checks)

    def render_table(self) -> str:
        """Render the report as a rich table."""
        console = Console(record=True)
        table = Table(title=f"Pre-flight Report: {self.instance_id}")
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Message")

        status_styles = {
            CheckStatus.PASS: "[green]PASS[/green]",
            CheckStatus.FAIL: "[red]FAIL[/red]",
            CheckStatus.WARN: "[yellow]WARN[/yellow]",
            CheckStatus.SKIP: "[dim]SKIP[/dim]",
        }

        for check in self.checks:
            table.add_row(
                check.name,
                status_styles.get(check.status, str(check.status)),
                check.message,
            )

        console.print(table)
        return console.export_text()


# Parameters removed or changed between PG major versions.
# Key: (source_major, target_major), Value: list of removed parameter names.
REMOVED_PARAMETERS: dict[tuple[int, int], list[str]] = {
    (13, 16): [
        "max_logical_replication_workers",
    ],
    (14, 16): [
        "max_logical_replication_workers",
    ],
}

# Extensions known to have compatibility issues across PG versions.
EXTENSION_COMPATIBILITY_NOTES: dict[str, str] = {
    "postgis": "Ensure PostGIS is updated to a version compatible with the target PG version.",
    "pg_stat_statements": "Reset statistics after migration — counters are not preserved.",
    "pglogical": "Verify pglogical version supports the target PG version.",
}


def _region_from_arn(arn: str) -> str:
    """Return the region segment of an AWS ARN, or "" if it isn't parseable.

    ARN shape: arn:aws:rds:REGION:account:db:id — region is index 3. Checking only
    for the presence of a colon isn't enough; a truncated ARN would IndexError.
    """
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 else ""


def run_preflight_checks(
    rds_client: Any,
    instance_id: str,
    target_version: str,
    config_path: str | None = None,
) -> PreflightReport:
    """Run all pre-flight checks and return a report.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance identifier or Aurora cluster identifier.
        target_version: Target PostgreSQL major version (e.g., "16").
        config_path: Optional path to a YAML configuration file with
                     custom checks or overrides.

    Returns:
        PreflightReport with results of all checks.
    """
    report = PreflightReport(instance_id=instance_id, target_version=target_version)

    # Run each check and append results
    report.checks.append(check_current_version(rds_client, instance_id, target_version))
    report.checks.append(check_parameter_compatibility(rds_client, instance_id, target_version))
    report.checks.append(check_extension_compatibility(rds_client, instance_id, target_version))
    report.checks.append(check_storage_space(rds_client, instance_id))
    report.checks.append(check_blue_green_support(rds_client, instance_id))

    return report


def check_current_version(
    rds_client: Any,
    instance_id: str,
    target_version: str,
) -> CheckResult:
    """Check the current PostgreSQL version and validate upgrade path.

    Ensures the instance is running a PG version that supports direct
    upgrade to the target version.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.
        target_version: Target PG major version.

    Returns:
        CheckResult with PASS if upgrade path is valid.
    """
    target_major = int(target_version.split(".")[0])

    # Try standalone RDS instance first, then Aurora cluster
    engine_version = None
    engine = None
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instance = resp["DBInstances"][0]
        engine_version = instance["EngineVersion"]
        engine = instance["Engine"]
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBInstanceNotFound" in str(exc):
            pass
        else:
            raise

    if engine_version is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            cluster = resp["DBClusters"][0]
            engine_version = cluster["EngineVersion"]
            engine = cluster["Engine"]
        except Exception as exc:
            return CheckResult(
                name="PG Version Check",
                status=CheckStatus.FAIL,
                message=f"Could not describe instance or cluster: {exc}",
            )

    source_major = int(engine_version.split(".")[0])
    logger.info(
        "Instance %s running %s %s (major: %d), target: %d",
        instance_id, engine, engine_version, source_major, target_major,
    )

    if source_major == target_major:
        return CheckResult(
            name="PG Version Check",
            status=CheckStatus.PASS,
            message=f"Already on target version {engine_version}.",
            details={"engine_version": engine_version, "engine": engine},
        )

    if not validate_upgrade_path(source_major, target_major):
        return CheckResult(
            name="PG Version Check",
            status=CheckStatus.FAIL,
            message=f"Upgrade from PG {source_major} to {target_major} is not supported.",
            details={"source_major": source_major, "target_major": target_major},
        )

    return CheckResult(
        name="PG Version Check",
        status=CheckStatus.PASS,
        message=f"Upgrade path {source_major} -> {target_major} is valid (current: {engine_version}).",
        details={
            "engine_version": engine_version,
            "engine": engine,
            "source_major": source_major,
            "target_major": target_major,
        },
    )


def check_parameter_compatibility(
    rds_client: Any,
    instance_id: str,
    target_version: str,
) -> CheckResult:
    """Check parameter group for removed or changed parameters.

    Critical check: parameters like max_logical_replication_workers were
    removed in PG 16. If present in a custom parameter group, the
    blue-green deployment will fail.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.
        target_version: Target PG major version.

    Returns:
        CheckResult with FAIL if incompatible parameters are found.
    """
    target_major = int(target_version.split(".")[0])

    # Determine current version and parameter group name
    param_group_name = None
    source_major = None
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instance = resp["DBInstances"][0]
        source_major = int(instance["EngineVersion"].split(".")[0])
        param_group_name = instance["DBParameterGroups"][0]["DBParameterGroupName"]
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            raise

    if param_group_name is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            cluster = resp["DBClusters"][0]
            source_major = int(cluster["EngineVersion"].split(".")[0])
            param_group_name = cluster.get("DBClusterParameterGroup")
        except Exception as exc:
            return CheckResult(
                name="Parameter Compatibility",
                status=CheckStatus.FAIL,
                message=f"Could not determine parameter group: {exc}",
            )

    if param_group_name is None or source_major is None:
        return CheckResult(
            name="Parameter Compatibility",
            status=CheckStatus.SKIP,
            message="Could not determine parameter group name.",
        )

    # Look up removed parameters for this version pair
    removed = REMOVED_PARAMETERS.get((source_major, target_major), [])
    if not removed:
        return CheckResult(
            name="Parameter Compatibility",
            status=CheckStatus.PASS,
            message=f"No known parameter removals for PG {source_major} -> {target_major}.",
        )

    # Get user-modified parameters from the parameter group.
    # Only user-modified params matter — defaults are handled by AWS automatically.
    user_params: list[str] = []
    paginator_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "DBParameterGroupName": param_group_name,
            "Source": "user",
        }
        if paginator_token:
            kwargs["Marker"] = paginator_token
        param_resp = rds_client.describe_db_parameters(**kwargs)
        for param in param_resp.get("Parameters", []):
            user_params.append(param["ParameterName"])
        paginator_token = param_resp.get("Marker")
        if not paginator_token:
            break

    # Cross-reference user-modified params against the removed list
    incompatible = [p for p in user_params if p in removed]
    if incompatible:
        logger.warning(
            "Instance %s has incompatible parameters: %s",
            instance_id, ", ".join(incompatible),
        )
        return CheckResult(
            name="Parameter Compatibility",
            status=CheckStatus.FAIL,
            message=(
                f"Incompatible parameters found: {', '.join(incompatible)}. "
                f"These were removed in PG {target_major} and will block blue-green deployment."
            ),
            details={
                "param_group": param_group_name,
                "incompatible_params": incompatible,
            },
        )

    return CheckResult(
        name="Parameter Compatibility",
        status=CheckStatus.PASS,
        message=f"All {len(user_params)} user-modified parameters are compatible with PG {target_major}.",
        details={"param_group": param_group_name, "user_param_count": len(user_params)},
    )


def check_extension_compatibility(
    rds_client: Any,
    instance_id: str,
    target_version: str,
) -> CheckResult:
    """Check installed extensions for known compatibility issues.

    Queries the instance's installed extensions and cross-references them
    against known compatibility notes for the target version.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.
        target_version: Target PG major version.

    Returns:
        CheckResult with WARN if extensions have known issues.
    """
    target_major = int(target_version.split(".")[0])

    # Get the parameter group to look for shared_preload_libraries —
    # this is the best indicator of installed extensions without a direct
    # DB connection. Extensions like pg_stat_statements, pglogical appear here.
    param_group_name = None
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instance = resp["DBInstances"][0]
        param_group_name = instance["DBParameterGroups"][0]["DBParameterGroupName"]
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            raise

    if param_group_name is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            cluster = resp["DBClusters"][0]
            param_group_name = cluster.get("DBClusterParameterGroup")
        except Exception:
            return CheckResult(
                name="Extension Compatibility",
                status=CheckStatus.SKIP,
                message="Could not determine parameter group for extension check.",
            )

    if param_group_name is None:
        return CheckResult(
            name="Extension Compatibility",
            status=CheckStatus.SKIP,
            message="No parameter group found.",
        )

    # Scan parameters for shared_preload_libraries and extension-related settings
    flagged: list[str] = []
    try:
        param_resp = rds_client.describe_db_parameters(
            DBParameterGroupName=param_group_name,
            Source="user",
        )
        for param in param_resp.get("Parameters", []):
            name = param["ParameterName"]
            value = param.get("ParameterValue", "")
            # Check shared_preload_libraries for known extensions
            if name == "shared_preload_libraries" and value:
                for ext_name in value.split(","):
                    ext_name = ext_name.strip()
                    if ext_name in EXTENSION_COMPATIBILITY_NOTES:
                        flagged.append(
                            f"{ext_name}: {EXTENSION_COMPATIBILITY_NOTES[ext_name]}"
                        )
            # Check if any parameter name matches a known extension
            for ext_name, note in EXTENSION_COMPATIBILITY_NOTES.items():
                if ext_name in name and ext_name not in [f.split(":")[0] for f in flagged]:
                    flagged.append(f"{ext_name}: {note}")
    except Exception as exc:
        return CheckResult(
            name="Extension Compatibility",
            status=CheckStatus.WARN,
            message=f"Could not read parameter group for extension check: {exc}",
        )

    if flagged:
        return CheckResult(
            name="Extension Compatibility",
            status=CheckStatus.WARN,
            message=f"Found {len(flagged)} extension(s) with known PG {target_major} notes.",
            details={"flagged_extensions": flagged},
        )

    return CheckResult(
        name="Extension Compatibility",
        status=CheckStatus.PASS,
        message="No extension compatibility issues detected.",
    )


def check_storage_space(
    rds_client: Any,
    instance_id: str,
) -> CheckResult:
    """Check available storage space for blue-green deployment.

    Blue-green deployments create a full copy of the database. Ensure
    there is sufficient storage headroom.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.

    Returns:
        CheckResult with WARN if storage is above 80% utilization.
    """
    from datetime import datetime, timedelta, timezone

    from rds_migrate.utils import DEFAULTS, get_cloudwatch_client

    # Get allocated storage from describe response
    allocated_gb = None
    is_aurora = False
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instance = resp["DBInstances"][0]
        allocated_gb = instance.get("AllocatedStorage")
        engine = instance.get("Engine", "")
        is_aurora = "aurora" in engine
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            raise

    if allocated_gb is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            is_aurora = True
        except Exception:
            return CheckResult(
                name="Storage Space",
                status=CheckStatus.SKIP,
                message="Could not determine storage configuration.",
            )

    # Aurora uses auto-scaling storage — always pass with a note
    if is_aurora:
        return CheckResult(
            name="Storage Space",
            status=CheckStatus.PASS,
            message="Aurora cluster uses auto-scaling storage — no fixed limit to check.",
            details={"is_aurora": True},
        )

    if allocated_gb is None or allocated_gb == 0:
        return CheckResult(
            name="Storage Space",
            status=CheckStatus.SKIP,
            message="Could not determine allocated storage.",
        )

    # Query CloudWatch for FreeStorageSpace
    try:
        # Extract region from the RDS client's meta if available,
        # otherwise fall back to default
        region = getattr(rds_client, "_client_config", None)
        region_name = getattr(region, "region_name", None) if region else None
        cw_client = get_cloudwatch_client(region=region_name or DEFAULTS["region"])

        now = datetime.now(timezone.utc)
        cw_resp = cw_client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="FreeStorageSpace",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
            StartTime=now - timedelta(minutes=30),
            EndTime=now,
            Period=300,
            Statistics=["Average"],
        )
        datapoints = cw_resp.get("Datapoints", [])
        if not datapoints:
            return CheckResult(
                name="Storage Space",
                status=CheckStatus.WARN,
                message=f"No CloudWatch data available. Allocated: {allocated_gb} GB.",
                details={"allocated_gb": allocated_gb},
            )

        # FreeStorageSpace is in bytes
        free_bytes = max(dp["Average"] for dp in datapoints)
        free_gb = free_bytes / (1024 ** 3)
        used_pct = ((allocated_gb - free_gb) / allocated_gb) * 100

        warning_threshold = DEFAULTS["storage_warning_threshold_pct"]
        critical_threshold = DEFAULTS["storage_critical_threshold_pct"]

        if used_pct >= critical_threshold:
            return CheckResult(
                name="Storage Space",
                status=CheckStatus.FAIL,
                message=(
                    f"Storage critically full: {used_pct:.1f}% used "
                    f"({free_gb:.1f} GB free of {allocated_gb} GB). "
                    f"Blue-green deployment requires space for a full copy."
                ),
                details={"used_pct": used_pct, "free_gb": free_gb, "allocated_gb": allocated_gb},
            )

        if used_pct >= warning_threshold:
            return CheckResult(
                name="Storage Space",
                status=CheckStatus.WARN,
                message=(
                    f"Storage usage high: {used_pct:.1f}% used "
                    f"({free_gb:.1f} GB free of {allocated_gb} GB)."
                ),
                details={"used_pct": used_pct, "free_gb": free_gb, "allocated_gb": allocated_gb},
            )

        return CheckResult(
            name="Storage Space",
            status=CheckStatus.PASS,
            message=f"Storage OK: {used_pct:.1f}% used ({free_gb:.1f} GB free of {allocated_gb} GB).",
            details={"used_pct": used_pct, "free_gb": free_gb, "allocated_gb": allocated_gb},
        )
    except Exception as exc:
        return CheckResult(
            name="Storage Space",
            status=CheckStatus.WARN,
            message=f"Could not query CloudWatch metrics: {exc}. Allocated: {allocated_gb} GB.",
            details={"allocated_gb": allocated_gb},
        )


def check_blue_green_support(
    rds_client: Any,
    instance_id: str,
) -> CheckResult:
    """Check if the instance type supports blue-green deployments.

    Not all instance types and configurations support blue-green deployments.
    For example, instances with read replicas in different regions or
    certain older instance classes may not be eligible.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.

    Returns:
        CheckResult with FAIL if blue-green deployment is not supported.
    """
    # Blue-green deployments require:
    #   - PostgreSQL engine (not MySQL)
    #   - Engine version >= 12.x
    #   - No cross-region read replicas
    #   - Instance status == 'available'
    engine = None
    engine_version = None
    status = None
    cross_region_replicas: list[str] = []

    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instance = resp["DBInstances"][0]
        engine = instance.get("Engine", "")
        engine_version = instance.get("EngineVersion", "")
        status = instance.get("DBInstanceStatus", "")
        # Check for cross-region read replicas by inspecting replica ARNs
        for replica_arn in instance.get("ReadReplicaDBInstanceIdentifiers", []):
            if isinstance(replica_arn, str) and ":rds:" in replica_arn:
                # Extract region from ARN: arn:aws:rds:REGION:account:db:id
                replica_region = _region_from_arn(replica_arn)
                instance_region = _region_from_arn(instance.get("DBInstanceArn", ""))
                # Only claim a cross-region replica when both regions are known —
                # an unparseable ARN is not evidence of anything.
                if replica_region and instance_region and replica_region != instance_region:
                    cross_region_replicas.append(replica_arn)
    except (KeyError, IndexError):
        pass
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            raise

    if engine is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            cluster = resp["DBClusters"][0]
            engine = cluster.get("Engine", "")
            engine_version = cluster.get("EngineVersion", "")
            status = cluster.get("Status", "")
        except Exception as exc:
            return CheckResult(
                name="Blue-Green Support",
                status=CheckStatus.FAIL,
                message=f"Could not describe instance or cluster: {exc}",
            )

    # Validate engine type
    if engine not in ("postgres", "aurora-postgresql"):
        return CheckResult(
            name="Blue-Green Support",
            status=CheckStatus.FAIL,
            message=f"Engine '{engine}' does not support PostgreSQL blue-green deployments.",
            details={"engine": engine},
        )

    # Validate minimum version (blue-green requires PG >= 12)
    major = int(engine_version.split(".")[0]) if engine_version else 0
    if major < 12:
        return CheckResult(
            name="Blue-Green Support",
            status=CheckStatus.FAIL,
            message=f"PG {engine_version} does not support blue-green deployments (requires >= 12).",
            details={"engine_version": engine_version},
        )

    # Check for cross-region replicas
    if cross_region_replicas:
        return CheckResult(
            name="Blue-Green Support",
            status=CheckStatus.FAIL,
            message=(
                f"Instance has {len(cross_region_replicas)} cross-region read replica(s). "
                f"Blue-green deployment is not supported with cross-region replicas."
            ),
            details={"cross_region_replicas": cross_region_replicas},
        )

    # Check instance status
    if status != "available":
        return CheckResult(
            name="Blue-Green Support",
            status=CheckStatus.FAIL,
            message=f"Instance status is '{status}' — must be 'available' for blue-green deployment.",
            details={"status": status},
        )

    return CheckResult(
        name="Blue-Green Support",
        status=CheckStatus.PASS,
        message=f"Instance eligible for blue-green deployment ({engine} {engine_version}).",
        details={"engine": engine, "engine_version": engine_version},
    )
