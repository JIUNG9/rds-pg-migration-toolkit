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

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.console import Console
from rich.table import Table


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
    # TODO: Implement — call describe_db_instances or describe_db_clusters
    # Parse EngineVersion, extract major version, validate upgrade path
    # AWS supports: 13->14, 13->15, 13->16, 14->15, 14->16, 15->16
    return CheckResult(
        name="PG Version Check",
        status=CheckStatus.SKIP,
        message="Not yet implemented — will validate upgrade path.",
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
    # TODO: Implement:
    # 1. Get the parameter group name from describe_db_instances
    # 2. Get all custom (non-default) parameters from describe_db_parameters
    # 3. Cross-reference with REMOVED_PARAMETERS for the version pair
    # 4. Return FAIL with details if any removed parameters are found
    return CheckResult(
        name="Parameter Compatibility",
        status=CheckStatus.SKIP,
        message="Not yet implemented — will check for removed parameters.",
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
    # TODO: Implement:
    # 1. Query pg_extension via RDS Data API or direct connection
    # 2. Check each extension against EXTENSION_COMPATIBILITY_NOTES
    # 3. Return WARN with notes for each flagged extension
    return CheckResult(
        name="Extension Compatibility",
        status=CheckStatus.SKIP,
        message="Not yet implemented — will check extension compatibility.",
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
    # TODO: Implement:
    # 1. Get AllocatedStorage and current usage from CloudWatch FreeStorageSpace
    # 2. Calculate utilization percentage
    # 3. WARN if > 80%, FAIL if > 95%
    return CheckResult(
        name="Storage Space",
        status=CheckStatus.SKIP,
        message="Not yet implemented — will check storage utilization.",
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
    # TODO: Implement:
    # 1. Check engine (must be postgres, not mysql)
    # 2. Check if Multi-AZ or Aurora
    # 3. Verify no cross-region read replicas
    # 4. Check engine version >= 12.x for blue-green support
    return CheckResult(
        name="Blue-Green Support",
        status=CheckStatus.SKIP,
        message="Not yet implemented — will check blue-green eligibility.",
    )
