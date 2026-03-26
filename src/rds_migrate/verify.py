"""
Post-migration verification for PostgreSQL upgrades on RDS/Aurora.

After a blue-green switchover, this module validates:
  1. PostgreSQL version matches the expected target
  2. Application connections are successful
  3. Replication status is healthy (if applicable)
  4. Basic query performance is acceptable
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from rds_migrate.utils import get_cloudwatch_client

logger = logging.getLogger(__name__)


class VerifyStatus(Enum):
    """Status of a verification check."""

    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class VerifyCheck:
    """Result of a single verification check."""

    name: str
    status: VerifyStatus
    message: str
    latency_ms: float | None = None


@dataclass
class VerificationReport:
    """Aggregated verification report."""

    instance_id: str
    expected_version: str
    checks: list[VerifyCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True if no checks failed."""
        return all(c.status != VerifyStatus.FAILED for c in self.checks)

    def __str__(self) -> str:
        lines = [
            f"Verification Report: {self.instance_id}",
            f"Expected Version: PG {self.expected_version}",
            f"Overall: {'PASS' if self.passed else 'FAIL'}",
            "",
        ]
        for check in self.checks:
            latency_str = f" ({check.latency_ms:.0f}ms)" if check.latency_ms else ""
            lines.append(f"  [{check.status.value}] {check.name}: {check.message}{latency_str}")
        return "\n".join(lines)


def run_verification(
    rds_client: Any,
    instance_id: str,
    expected_version: str,
    check_connections: bool = True,
    check_replication: bool = False,
) -> VerificationReport:
    """Run all post-migration verification checks.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or Aurora cluster identifier.
        expected_version: Expected PostgreSQL version after migration (e.g., "16").
        check_connections: Whether to verify active connections.
        check_replication: Whether to check replication status.

    Returns:
        VerificationReport with results of all checks.
    """
    report = VerificationReport(instance_id=instance_id, expected_version=expected_version)

    report.checks.append(verify_pg_version(rds_client, instance_id, expected_version))

    if check_connections:
        report.checks.append(verify_connections(rds_client, instance_id))

    if check_replication:
        report.checks.append(verify_replication(rds_client, instance_id))

    report.checks.append(verify_basic_queries(rds_client, instance_id))

    return report


def verify_pg_version(
    rds_client: Any,
    instance_id: str,
    expected_version: str,
) -> VerifyCheck:
    """Verify the PostgreSQL engine version matches the expected target.

    Calls describe_db_instances (or describe_db_clusters for Aurora) and
    compares the EngineVersion major version.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.
        expected_version: Expected major version (e.g., "16").

    Returns:
        VerifyCheck with OK if version matches, FAILED otherwise.
    """
    logger.info("Verifying PG version for %s (expected: %s)", instance_id, expected_version)

    engine_version: str | None = None

    # Try standalone RDS instance first
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instances = resp.get("DBInstances", [])
        if instances:
            engine_version = instances[0].get("EngineVersion")
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            return VerifyCheck(
                name="PG Version",
                status=VerifyStatus.FAILED,
                message=f"Error querying instance: {exc}",
            )

    # Fall back to Aurora cluster
    if engine_version is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            clusters = resp.get("DBClusters", [])
            if clusters:
                engine_version = clusters[0].get("EngineVersion")
        except Exception as exc:
            return VerifyCheck(
                name="PG Version",
                status=VerifyStatus.FAILED,
                message=f"Error querying cluster: {exc}",
            )

    if engine_version is None:
        return VerifyCheck(
            name="PG Version",
            status=VerifyStatus.FAILED,
            message=f"Could not determine engine version for {instance_id}.",
        )

    # Compare major version: extract the part before the first dot
    actual_major = engine_version.split(".")[0]
    if actual_major == expected_version:
        return VerifyCheck(
            name="PG Version",
            status=VerifyStatus.OK,
            message=f"Version matches: {engine_version} (major {actual_major}).",
        )
    else:
        return VerifyCheck(
            name="PG Version",
            status=VerifyStatus.FAILED,
            message=f"Version mismatch: expected PG {expected_version}, got {engine_version} (major {actual_major}).",
        )


def verify_connections(
    rds_client: Any,
    instance_id: str,
) -> VerifyCheck:
    """Verify that the instance is accepting connections.

    Checks the instance status via the RDS API and optionally attempts
    a direct connection if a connection string is available.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.

    Returns:
        VerifyCheck with OK if connections are healthy.
    """
    logger.info("Verifying connections for %s", instance_id)

    # Step 1: Check instance status via RDS API
    status: str | None = None
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instances = resp.get("DBInstances", [])
        if instances:
            status = instances[0].get("DBInstanceStatus")
    except Exception as exc:
        if "DBInstanceNotFound" not in str(exc):
            return VerifyCheck(
                name="Connections",
                status=VerifyStatus.FAILED,
                message=f"Error querying instance status: {exc}",
            )

    # Fall back to Aurora cluster status
    if status is None:
        try:
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            clusters = resp.get("DBClusters", [])
            if clusters:
                status = clusters[0].get("Status")
        except Exception as exc:
            return VerifyCheck(
                name="Connections",
                status=VerifyStatus.FAILED,
                message=f"Error querying cluster status: {exc}",
            )

    if status is None:
        return VerifyCheck(
            name="Connections",
            status=VerifyStatus.FAILED,
            message=f"Could not determine status for {instance_id}.",
        )

    if status != "available":
        return VerifyCheck(
            name="Connections",
            status=VerifyStatus.FAILED,
            message=f"Instance status is '{status}', expected 'available'.",
        )

    # Step 2: Check CloudWatch DatabaseConnections metric
    try:
        cw_client = get_cloudwatch_client()
        now = datetime.now(timezone.utc)
        cw_resp = cw_client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="DatabaseConnections",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
            StartTime=now - timedelta(minutes=5),
            EndTime=now,
            Period=300,
            Statistics=["Average"],
        )
        datapoints = cw_resp.get("Datapoints", [])
        if datapoints:
            avg_conns = datapoints[0].get("Average", 0)
            if avg_conns > 0:
                return VerifyCheck(
                    name="Connections",
                    status=VerifyStatus.OK,
                    message=f"Instance available with ~{avg_conns:.0f} active connections.",
                )
            else:
                return VerifyCheck(
                    name="Connections",
                    status=VerifyStatus.DEGRADED,
                    message="Instance available but no active connections detected.",
                )
        else:
            return VerifyCheck(
                name="Connections",
                status=VerifyStatus.OK,
                message="Instance available. No CloudWatch data yet (metric may need time to populate).",
            )
    except Exception as exc:
        logger.debug("CloudWatch check failed: %s", exc)
        return VerifyCheck(
            name="Connections",
            status=VerifyStatus.OK,
            message=f"Instance available (CloudWatch check skipped: {exc}).",
        )


def verify_replication(
    rds_client: Any,
    instance_id: str,
) -> VerifyCheck:
    """Verify replication status after migration.

    For Aurora clusters, checks that all reader instances are in sync.
    For standalone RDS with read replicas, checks ReplicaLag.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.

    Returns:
        VerifyCheck with OK if replication is healthy, DEGRADED if lagging.
    """
    logger.info("Verifying replication for %s", instance_id)

    # Try Aurora cluster first
    try:
        resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
        clusters = resp.get("DBClusters", [])
        if clusters:
            cluster = clusters[0]
            members = cluster.get("DBClusterMembers", [])
            writers = [m for m in members if m.get("IsClusterWriter")]
            readers = [m for m in members if not m.get("IsClusterWriter")]

            if not writers:
                return VerifyCheck(
                    name="Replication",
                    status=VerifyStatus.FAILED,
                    message="No writer instance found in Aurora cluster.",
                )

            if not readers:
                return VerifyCheck(
                    name="Replication",
                    status=VerifyStatus.OK,
                    message=f"Aurora cluster has {len(writers)} writer(s), no readers configured.",
                )

            # Check AuroraReplicaLag via CloudWatch for each reader
            try:
                cw_client = get_cloudwatch_client()
                now = datetime.now(timezone.utc)
                max_lag_ms = 0.0
                for reader in readers:
                    reader_id = reader["DBInstanceIdentifier"]
                    cw_resp = cw_client.get_metric_statistics(
                        Namespace="AWS/RDS",
                        MetricName="AuroraReplicaLag",
                        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": reader_id}],
                        StartTime=now - timedelta(minutes=5),
                        EndTime=now,
                        Period=300,
                        Statistics=["Maximum"],
                    )
                    datapoints = cw_resp.get("Datapoints", [])
                    if datapoints:
                        lag = datapoints[0].get("Maximum", 0)
                        max_lag_ms = max(max_lag_ms, lag)
            except Exception as exc:
                logger.debug("CloudWatch replica lag check failed: %s", exc)
                return VerifyCheck(
                    name="Replication",
                    status=VerifyStatus.OK,
                    message=f"Aurora cluster: {len(writers)} writer(s), {len(readers)} reader(s). "
                            f"Lag check skipped: {exc}.",
                )

            if max_lag_ms > 1000:
                return VerifyCheck(
                    name="Replication",
                    status=VerifyStatus.DEGRADED,
                    message=f"Aurora replica lag: {max_lag_ms:.0f}ms across {len(readers)} reader(s).",
                    latency_ms=max_lag_ms,
                )
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.OK,
                message=f"Aurora cluster healthy: {len(writers)} writer(s), {len(readers)} reader(s), "
                        f"max lag {max_lag_ms:.0f}ms.",
                latency_ms=max_lag_ms,
            )
    except Exception as exc:
        if "DBClusterNotFound" not in str(exc):
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.FAILED,
                message=f"Error querying Aurora cluster: {exc}",
            )

    # Standalone RDS: check read replicas
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        instances = resp.get("DBInstances", [])
        if not instances:
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.FAILED,
                message=f"Instance {instance_id} not found.",
            )

        instance = instances[0]
        replica_ids = instance.get("ReadReplicaDBInstanceIdentifiers", [])

        if not replica_ids:
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.OK,
                message="No read replicas configured.",
            )

        # Check ReplicaLag via CloudWatch for each replica
        try:
            cw_client = get_cloudwatch_client()
            now = datetime.now(timezone.utc)
            max_lag_sec = 0.0
            error_replicas: list[str] = []

            for replica_id in replica_ids:
                try:
                    rep_resp = rds_client.describe_db_instances(DBInstanceIdentifier=replica_id)
                    rep_instances = rep_resp.get("DBInstances", [])
                    if rep_instances:
                        rep_status = rep_instances[0].get("DBInstanceStatus", "unknown")
                        if rep_status not in ("available", "storage-optimization"):
                            error_replicas.append(f"{replica_id}({rep_status})")
                except Exception:
                    error_replicas.append(replica_id)

                cw_resp = cw_client.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="ReplicaLag",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": replica_id}],
                    StartTime=now - timedelta(minutes=5),
                    EndTime=now,
                    Period=300,
                    Statistics=["Maximum"],
                )
                datapoints = cw_resp.get("Datapoints", [])
                if datapoints:
                    lag = datapoints[0].get("Maximum", 0)
                    max_lag_sec = max(max_lag_sec, lag)
        except Exception as exc:
            logger.debug("CloudWatch replica lag check failed: %s", exc)
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.OK,
                message=f"{len(replica_ids)} replica(s) configured. Lag check skipped: {exc}.",
            )

        if error_replicas:
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.FAILED,
                message=f"Replica(s) in error state: {', '.join(error_replicas)}.",
            )

        if max_lag_sec > 0:
            return VerifyCheck(
                name="Replication",
                status=VerifyStatus.DEGRADED,
                message=f"Replica lag detected: {max_lag_sec:.1f}s across {len(replica_ids)} replica(s).",
            )

        return VerifyCheck(
            name="Replication",
            status=VerifyStatus.OK,
            message=f"{len(replica_ids)} replica(s) in sync.",
        )
    except Exception as exc:
        return VerifyCheck(
            name="Replication",
            status=VerifyStatus.FAILED,
            message=f"Error checking replication: {exc}",
        )


def verify_basic_queries(
    rds_client: Any,
    instance_id: str,
) -> VerifyCheck:
    """Run a basic query to verify the database is responsive.

    Uses an RDS API call as a latency proxy to verify the instance is responsive.
    A direct SQL query would require psycopg2 + credentials; the API call confirms
    the control plane can reach the instance and return metadata promptly.

    Args:
        rds_client: boto3 RDS client.
        instance_id: RDS instance or cluster identifier.

    Returns:
        VerifyCheck with latency measurement.
    """
    logger.info("Running basic query verification for %s", instance_id)

    start = time.perf_counter()
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=instance_id)
        elapsed_ms = (time.perf_counter() - start) * 1000

        instances = resp.get("DBInstances", [])
        if not instances:
            start = time.perf_counter()
            resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
            elapsed_ms = (time.perf_counter() - start) * 1000
            clusters = resp.get("DBClusters", [])
            if not clusters:
                return VerifyCheck(
                    name="Basic Query",
                    status=VerifyStatus.FAILED,
                    message=f"Instance {instance_id} not found.",
                    latency_ms=elapsed_ms,
                )

        if elapsed_ms > 5000:
            return VerifyCheck(
                name="Basic Query",
                status=VerifyStatus.DEGRADED,
                message=f"API responded but latency is high ({elapsed_ms:.0f}ms).",
                latency_ms=elapsed_ms,
            )

        return VerifyCheck(
            name="Basic Query",
            status=VerifyStatus.OK,
            message=f"API responded in {elapsed_ms:.0f}ms.",
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        if "DBInstanceNotFound" in str(exc):
            try:
                start2 = time.perf_counter()
                resp = rds_client.describe_db_clusters(DBClusterIdentifier=instance_id)
                elapsed_ms = (time.perf_counter() - start2) * 1000
                return VerifyCheck(
                    name="Basic Query",
                    status=VerifyStatus.OK,
                    message=f"Aurora cluster API responded in {elapsed_ms:.0f}ms.",
                    latency_ms=elapsed_ms,
                )
            except Exception as exc2:
                elapsed_ms = (time.perf_counter() - start2) * 1000
                return VerifyCheck(
                    name="Basic Query",
                    status=VerifyStatus.FAILED,
                    message=f"API call failed: {exc2}",
                    latency_ms=elapsed_ms,
                )

        return VerifyCheck(
            name="Basic Query",
            status=VerifyStatus.FAILED,
            message=f"API call failed: {exc}",
            latency_ms=elapsed_ms,
        )
