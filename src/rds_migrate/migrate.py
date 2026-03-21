"""
Migration logic for PostgreSQL major version upgrades via blue-green deployments.

Handles:
  - Creating blue-green deployments for both standalone RDS and Aurora clusters
  - Monitoring deployment progress with rich progress bars
  - Performing switchover when the green environment is ready
  - Rolling back failed deployments

Aurora clusters and standalone RDS instances have different API behaviors and
are handled separately within each function.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from rds_migrate.preflight import REMOVED_PARAMETERS
from rds_migrate.utils import get_instance_arn, resolve_engine_version

logger = logging.getLogger(__name__)

# Terminal states — polling stops when the deployment reaches one of these.
_TERMINAL_STATES = {"AVAILABLE", "SWITCHOVER_COMPLETED", "FAILED", "DELETED"}


class DeploymentStatus(Enum):
    """Status of a blue-green deployment."""

    PROVISIONING = "PROVISIONING"
    AVAILABLE = "AVAILABLE"
    SWITCHOVER_IN_PROGRESS = "SWITCHOVER_IN_PROGRESS"
    SWITCHOVER_COMPLETED = "SWITCHOVER_COMPLETED"
    DELETE_IN_PROGRESS = "DELETE_IN_PROGRESS"
    DELETED = "DELETED"
    FAILED = "FAILED"


@dataclass
class DeploymentResult:
    """Result of a blue-green deployment operation."""

    deployment_id: str
    status: DeploymentStatus
    source_instance: str
    target_version: str
    message: str
    green_instance_id: str | None = None
    switchover_time: float | None = None

    def __str__(self) -> str:
        lines = [
            f"Deployment: {self.deployment_id}",
            f"Status:     {self.status.value}",
            f"Source:     {self.source_instance}",
            f"Target PG:  {self.target_version}",
            f"Message:    {self.message}",
        ]
        if self.green_instance_id:
            lines.append(f"Green ID:   {self.green_instance_id}")
        if self.switchover_time:
            lines.append(f"Switchover: {self.switchover_time:.1f}s")
        return "\n".join(lines)


def create_blue_green_deployment(
    rds_client: Any,
    instance_id: str,
    target_version: str,
    wait: bool = True,
    switchover: bool = False,
    timeout: int = 3600,
) -> DeploymentResult:
    """Create a blue-green deployment for a PostgreSQL major version upgrade.

    This function determines whether the target is an Aurora cluster or
    standalone RDS instance and delegates accordingly.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or Aurora cluster identifier.
        target_version: Target PostgreSQL major version (e.g., "16").
        wait: If True, poll until deployment is AVAILABLE or times out.
        switchover: If True, automatically perform switchover once ready.
        timeout: Maximum time in seconds to wait for deployment.

    Returns:
        DeploymentResult with deployment status and details.

    Raises:
        click.ClickException: If the deployment fails or times out.
    """
    is_aurora = _is_aurora_cluster(rds_client, instance_id)

    if is_aurora:
        return _create_aurora_deployment(
            rds_client, instance_id, target_version, wait, switchover, timeout
        )
    else:
        return _create_rds_deployment(
            rds_client, instance_id, target_version, wait, switchover, timeout
        )


def _is_aurora_cluster(rds_client: Any, instance_id: str) -> bool:
    """Determine if the given identifier refers to an Aurora cluster.

    Tries describe_db_clusters first — if it returns a result, the
    identifier is an Aurora cluster. Otherwise falls back to standalone RDS.

    Args:
        rds_client: boto3 RDS client.
        instance_id: Identifier to check.

    Returns:
        True if the identifier is an Aurora cluster, False if standalone RDS.
    """
    logger.info("Checking if %s is an Aurora cluster...", instance_id)
    try:
        resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
        clusters = resp.get("DBClusters", [])
        if clusters:
            logger.info("%s is an Aurora cluster with %d members",
                        instance_id, len(clusters[0].get("DBClusterMembers", [])))
            return True
    except Exception as exc:
        if "DBClusterNotFound" in str(exc):
            logger.debug("%s is not an Aurora cluster: %s", instance_id, exc)
        else:
            raise
    return False


def _create_target_parameter_group(
    rds_client: Any,
    source_param_group: str,
    source_major: int,
    target_major: int,
    deployment_name: str,
    is_cluster: bool = False,
) -> str:
    """Create a target parameter group for the green environment.

    Copies user-modified parameters from the source group, filtering out
    parameters removed in the target PG version (e.g. max_logical_replication_workers
    removed in PG 16).

    Args:
        rds_client: boto3 RDS client.
        source_param_group: Source parameter group name.
        source_major: Source PG major version.
        target_major: Target PG major version.
        deployment_name: Used to generate the target group name.
        is_cluster: True for cluster parameter groups.

    Returns:
        Name of the created target parameter group.
    """
    target_group_name = f"{deployment_name}-pg{target_major}"
    family = f"{'aurora-' if is_cluster else ''}postgres{target_major}"

    removed = set(REMOVED_PARAMETERS.get((source_major, target_major), []))

    logger.info(
        "Creating target parameter group %s (family: %s, filtering %d removed params)",
        target_group_name, family, len(removed),
    )

    if is_cluster:
        rds_client.create_db_cluster_parameter_group(
            DBClusterParameterGroupName=target_group_name,
            DBParameterGroupFamily=family,
            Description=f"Blue-green target for {deployment_name} (PG {target_major})",
        )
    else:
        rds_client.create_db_parameter_group(
            DBParameterGroupName=target_group_name,
            DBParameterGroupFamily=family,
            Description=f"Blue-green target for {deployment_name} (PG {target_major})",
        )

    # Copy user-modified parameters, excluding removed ones
    source_params_resp = rds_client.describe_db_parameters(
        DBParameterGroupName=source_param_group,
        Source="user",
    ) if not is_cluster else rds_client.describe_db_cluster_parameters(
        DBClusterParameterGroupName=source_param_group,
        Source="user",
    )

    params_to_copy = []
    for param in source_params_resp.get("Parameters", []):
        name = param["ParameterName"]
        if name in removed:
            logger.warning("Filtering removed parameter: %s", name)
            continue
        if param.get("ParameterValue") is not None:
            params_to_copy.append({
                "ParameterName": name,
                "ParameterValue": param["ParameterValue"],
                "ApplyMethod": param.get("ApplyMethod", "pending-reboot"),
            })

    if params_to_copy:
        if is_cluster:
            rds_client.modify_db_cluster_parameter_group(
                DBClusterParameterGroupName=target_group_name,
                Parameters=params_to_copy,
            )
        else:
            rds_client.modify_db_parameter_group(
                DBParameterGroupName=target_group_name,
                Parameters=params_to_copy,
            )
        logger.info("Copied %d parameters to target group", len(params_to_copy))

    return target_group_name


def _create_rds_deployment(
    rds_client: Any,
    instance_id: str,
    target_version: str,
    wait: bool,
    switchover: bool,
    timeout: int,
) -> DeploymentResult:
    """Create a blue-green deployment for a standalone RDS PostgreSQL instance.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance identifier.
        target_version: Target PG major version.
        wait: Whether to poll for completion.
        switchover: Whether to auto-switchover.
        timeout: Timeout in seconds.

    Returns:
        DeploymentResult with RDS-specific details.
    """
    logger.info("Creating RDS blue-green deployment for %s -> PG %s", instance_id, target_version)

    # Resolve instance details
    source_arn = get_instance_arn(rds_client, instance_id)
    target_engine_version = resolve_engine_version(rds_client, target_version)

    # Get source parameter group and version for filtering
    resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
    instance = resp["DBInstances"][0]
    source_param_group = instance["DBParameterGroups"][0]["DBParameterGroupName"]
    source_major = int(instance["EngineVersion"].split(".")[0])
    target_major = int(target_version.split(".")[0])

    # Create target parameter group with incompatible params filtered out
    deployment_name = f"bgd-{instance_id}"
    target_param_group = _create_target_parameter_group(
        rds_client, source_param_group, source_major, target_major,
        deployment_name, is_cluster=False,
    )

    # Create the blue-green deployment
    bg_resp = rds_client.create_blue_green_deployment(
        BlueGreenDeploymentName=deployment_name,
        Source=source_arn,
        TargetEngineVersion=target_engine_version,
        TargetDBParameterGroupName=target_param_group,
    )

    deployment = bg_resp["BlueGreenDeployment"]
    deployment_id = deployment["BlueGreenDeploymentIdentifier"]
    logger.info("Created deployment %s", deployment_id)

    result = DeploymentResult(
        deployment_id=deployment_id,
        status=DeploymentStatus.PROVISIONING,
        source_instance=instance_id,
        target_version=target_version,
        message=f"Blue-green deployment created (target: {target_engine_version}).",
    )

    if wait:
        final_status = _poll_deployment_status(rds_client, deployment_id, timeout)
        result.status = final_status
        if final_status == DeploymentStatus.AVAILABLE:
            result.message = "Deployment ready for switchover."
        elif final_status == DeploymentStatus.FAILED:
            result.message = "Deployment failed. Check AWS console for details."

    if switchover and result.status == DeploymentStatus.AVAILABLE:
        result = _perform_switchover(rds_client, deployment_id, result)

    return result


def _create_aurora_deployment(
    rds_client: Any,
    cluster_id: str,
    target_version: str,
    wait: bool,
    switchover: bool,
    timeout: int,
) -> DeploymentResult:
    """Create a blue-green deployment for an Aurora PostgreSQL cluster.

    Aurora blue-green deployments operate at the cluster level and include
    all reader instances. The API call is create_blue_green_deployment with
    the source being the cluster ARN.

    Args:
        rds_client: boto3 RDS client.
        cluster_id: Aurora cluster identifier.
        target_version: Target PG major version.
        wait: Whether to poll for completion.
        switchover: Whether to auto-switchover.
        timeout: Timeout in seconds.

    Returns:
        DeploymentResult with Aurora-specific details.
    """
    logger.info("Creating Aurora blue-green deployment for %s -> PG %s", cluster_id, target_version)

    # Resolve cluster details
    resp = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    cluster = resp["DBClusters"][0]
    source_arn = cluster["DBClusterArn"]
    source_param_group = cluster.get("DBClusterParameterGroup", "")
    source_major = int(cluster["EngineVersion"].split(".")[0])
    target_major = int(target_version.split(".")[0])

    target_engine_version = resolve_engine_version(
        rds_client, target_version, engine="aurora-postgresql"
    )

    # Create target cluster parameter group
    deployment_name = f"bgd-{cluster_id}"
    target_param_group = _create_target_parameter_group(
        rds_client, source_param_group, source_major, target_major,
        deployment_name, is_cluster=True,
    )

    # Create the blue-green deployment
    bg_resp = rds_client.create_blue_green_deployment(
        BlueGreenDeploymentName=deployment_name,
        Source=source_arn,
        TargetEngineVersion=target_engine_version,
        TargetDBClusterParameterGroupName=target_param_group,
    )

    deployment = bg_resp["BlueGreenDeployment"]
    deployment_id = deployment["BlueGreenDeploymentIdentifier"]
    logger.info("Created Aurora deployment %s", deployment_id)

    result = DeploymentResult(
        deployment_id=deployment_id,
        status=DeploymentStatus.PROVISIONING,
        source_instance=cluster_id,
        target_version=target_version,
        message=f"Aurora blue-green deployment created (target: {target_engine_version}).",
    )

    if wait:
        final_status = _poll_deployment_status(rds_client, deployment_id, timeout)
        result.status = final_status
        if final_status == DeploymentStatus.AVAILABLE:
            result.message = "Aurora deployment ready for switchover."
        elif final_status == DeploymentStatus.FAILED:
            result.message = "Aurora deployment failed. Check AWS console."

    if switchover and result.status == DeploymentStatus.AVAILABLE:
        result = _perform_switchover(rds_client, deployment_id, result)

    return result


def _poll_deployment_status(
    rds_client: Any,
    deployment_id: str,
    timeout: int = 3600,
    poll_interval: int = 30,
) -> DeploymentStatus:
    """Poll a blue-green deployment until it reaches a terminal state.

    Terminal states: AVAILABLE, SWITCHOVER_COMPLETED, FAILED, DELETED.

    Args:
        rds_client: boto3 RDS client.
        deployment_id: Blue-green deployment identifier.
        timeout: Maximum time to wait in seconds.
        poll_interval: Seconds between polls.

    Returns:
        Final DeploymentStatus.

    Raises:
        TimeoutError: If the deployment does not reach a terminal state within timeout.
    """
    console = Console()
    start_time = time.time()
    last_status = ""

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Waiting for {deployment_id}...", total=None)

        while time.time() - start_time < timeout:
            resp = rds_client.describe_blue_green_deployments(
                BlueGreenDeploymentIdentifier=deployment_id
            )
            deployments = resp.get("BlueGreenDeployments", [])
            if not deployments:
                raise ValueError(f"Deployment {deployment_id} not found")

            status = deployments[0].get("Status", "UNKNOWN")

            if status != last_status:
                logger.info("Deployment %s status: %s", deployment_id, status)
                progress.update(task, description=f"[bold]{deployment_id}[/bold] — {status}")
                last_status = status

            if status in _TERMINAL_STATES:
                return DeploymentStatus(status)

            time.sleep(poll_interval)

    raise TimeoutError(
        f"Deployment {deployment_id} did not complete within {timeout}s "
        f"(last status: {last_status})"
    )


def _perform_switchover(
    rds_client: Any,
    deployment_id: str,
    result: DeploymentResult,
) -> DeploymentResult:
    """Perform the switchover from blue to green environment.

    The switchover swaps the DNS endpoints — the green environment becomes
    the new production. Write interruption is typically 4-6 seconds.

    Args:
        rds_client: boto3 RDS client.
        deployment_id: Blue-green deployment identifier.
        result: Current DeploymentResult to update.

    Returns:
        Updated DeploymentResult with switchover details.
    """
    logger.info("Initiating switchover for %s", deployment_id)
    switchover_start = time.time()

    rds_client.switchover_blue_green_deployment(
        BlueGreenDeploymentIdentifier=deployment_id,
    )

    # Poll until switchover completes
    final_status = _poll_deployment_status(
        rds_client, deployment_id, timeout=600, poll_interval=5,
    )

    switchover_elapsed = time.time() - switchover_start
    result.status = final_status
    result.switchover_time = switchover_elapsed

    if final_status == DeploymentStatus.SWITCHOVER_COMPLETED:
        result.message = f"Switchover completed in {switchover_elapsed:.1f}s."
    else:
        result.message = f"Switchover ended with status {final_status.value}."

    return result


def rollback_deployment(
    rds_client: Any,
    deployment_id: str,
    delete_green: bool = True,
) -> DeploymentResult:
    """Roll back a blue-green deployment by deleting the green environment.

    This should be called when:
      - Pre-switchover: deployment failed or is no longer needed
      - Post-switchover: not applicable (use point-in-time recovery instead)

    Args:
        rds_client: boto3 RDS client.
        deployment_id: Blue-green deployment identifier.
        delete_green: If True, delete the green environment instances.

    Returns:
        DeploymentResult indicating rollback status.
    """
    logger.info("Rolling back deployment %s (delete_green=%s)", deployment_id, delete_green)

    try:
        rds_client.delete_blue_green_deployment(
            BlueGreenDeploymentIdentifier=deployment_id,
            DeleteTarget=delete_green,
        )
    except Exception as exc:
        return DeploymentResult(
            deployment_id=deployment_id,
            status=DeploymentStatus.FAILED,
            source_instance="unknown",
            target_version="unknown",
            message=f"Rollback failed: {exc}",
        )

    return DeploymentResult(
        deployment_id=deployment_id,
        status=DeploymentStatus.DELETED if delete_green else DeploymentStatus.DELETE_IN_PROGRESS,
        source_instance="unknown",
        target_version="unknown",
        message=(
            "Green environment deletion initiated."
            if delete_green
            else "Deployment cancelled (green environment preserved)."
        ),
    )
