"""
Post-migration maintenance for PostgreSQL on RDS/Aurora.

After a blue-green switchover, the new primary has a cold buffer cache and
stale optimizer statistics. This module automates:
  1. ANALYZE on all tables (refresh planner statistics)
  2. REINDEX on critical indexes (rebuild potentially bloated indexes)
  3. Bloat detection (identify tables/indexes needing VACUUM or REINDEX)

IMPORTANT: Do NOT run VACUUM FULL after migration — it acquires ACCESS EXCLUSIVE
locks and can cause downtime. Use regular VACUUM and ANALYZE instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MaintenanceTask:
    """A single maintenance task and its result."""

    name: str
    table_or_index: str
    status: str  # "completed", "failed", "skipped"
    duration_ms: float | None = None
    message: str = ""


@dataclass
class MaintenanceReport:
    """Report of all maintenance tasks executed."""

    instance_id: str
    tasks: list[MaintenanceTask] = field(default_factory=list)

    @property
    def completed_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "completed")

    @property
    def failed_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "failed")

    def __str__(self) -> str:
        lines = [
            f"Maintenance Report: {self.instance_id}",
            f"Tasks: {len(self.tasks)} total, {self.completed_count} completed, {self.failed_count} failed",
            "",
        ]
        for task in self.tasks:
            duration_str = f" ({task.duration_ms:.0f}ms)" if task.duration_ms else ""
            lines.append(f"  [{task.status.upper()}] {task.name} on {task.table_or_index}{duration_str}")
            if task.message:
                lines.append(f"           {task.message}")
        return "\n".join(lines)


def run_maintenance(
    instance_id: str,
    run_analyze: bool = False,
    run_reindex: bool = False,
    check_bloat: bool = False,
    connection_string: str | None = None,
) -> MaintenanceReport:
    """Run post-migration maintenance tasks.

    Requires a direct PostgreSQL connection (not the RDS API). Provide
    a connection string or configure it in the YAML config.

    Args:
        instance_id: RDS instance identifier (for reporting).
        run_analyze: If True, run ANALYZE on all user tables.
        run_reindex: If True, run REINDEX on critical indexes.
        check_bloat: If True, check for table and index bloat.
        connection_string: PostgreSQL connection string
                          (e.g., "host=... port=5432 dbname=... user=... password=...").

    Returns:
        MaintenanceReport with results of all tasks.
    """
    report = MaintenanceReport(instance_id=instance_id)

    if not any([run_analyze, run_reindex, check_bloat]):
        logger.warning("No maintenance tasks requested. Use --analyze, --reindex, or --check-bloat.")
        return report

    # TODO: Establish connection using connection_string or resolve from instance_id
    # conn = _get_connection(connection_string)

    if run_analyze:
        analyze_tasks = analyze_all_tables(connection_string)
        report.tasks.extend(analyze_tasks)

    if run_reindex:
        reindex_tasks = reindex_critical_indexes(connection_string)
        report.tasks.extend(reindex_tasks)

    if check_bloat:
        bloat_tasks = detect_bloat(connection_string)
        report.tasks.extend(bloat_tasks)

    return report


def analyze_all_tables(
    connection_string: str | None = None,
) -> list[MaintenanceTask]:
    """Run ANALYZE on all user tables to refresh optimizer statistics.

    After a blue-green switchover, pg_statistic data may be stale because
    the green environment was created from a snapshot. Running ANALYZE
    ensures the query planner has accurate statistics.

    This function iterates over all tables in all user schemas (excluding
    pg_catalog and information_schema) and runs ANALYZE on each.

    Args:
        connection_string: PostgreSQL connection string.

    Returns:
        List of MaintenanceTask results, one per table.
    """
    # TODO: Implement:
    # 1. Connect to the database
    # 2. Query information_schema.tables for all user tables
    # 3. For each table, run ANALYZE and measure duration
    # 4. Return list of MaintenanceTask results
    #
    # SQL to get tables:
    #   SELECT schemaname, tablename
    #   FROM pg_tables
    #   WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    #   ORDER BY pg_total_relation_size(schemaname || '.' || tablename) DESC;
    #
    # Run largest tables first to warm the cache faster
    logger.info("Running ANALYZE on all user tables...")
    return [
        MaintenanceTask(
            name="ANALYZE",
            table_or_index="*",
            status="skipped",
            message="Not yet implemented — requires database connection.",
        )
    ]


def reindex_critical_indexes(
    connection_string: str | None = None,
) -> list[MaintenanceTask]:
    """Run REINDEX on critical indexes to rebuild potentially bloated indexes.

    After major version upgrades, indexes may have different internal formats
    or accumulated bloat from the replication process. REINDEX CONCURRENTLY
    is preferred to avoid locking.

    Only indexes above a configurable size threshold are reindexed.

    Args:
        connection_string: PostgreSQL connection string.

    Returns:
        List of MaintenanceTask results, one per index.
    """
    # TODO: Implement:
    # 1. Connect to the database
    # 2. Query pg_indexes joined with pg_relation_size to find large indexes
    # 3. For each index above threshold, run REINDEX INDEX CONCURRENTLY
    # 4. Return list of MaintenanceTask results
    #
    # Use REINDEX CONCURRENTLY to avoid ACCESS EXCLUSIVE locks
    # Threshold: indexes > 1GB by default (configurable)
    logger.info("Running REINDEX on critical indexes...")
    return [
        MaintenanceTask(
            name="REINDEX",
            table_or_index="*",
            status="skipped",
            message="Not yet implemented — requires database connection.",
        )
    ]


def detect_bloat(
    connection_string: str | None = None,
) -> list[MaintenanceTask]:
    """Detect table and index bloat after migration.

    Uses the pgstattuple extension or statistical estimation to identify
    tables and indexes with significant bloat. Reports findings but does
    not take action (VACUUM FULL requires careful manual planning).

    Args:
        connection_string: PostgreSQL connection string.

    Returns:
        List of MaintenanceTask results with bloat estimates.
    """
    # TODO: Implement:
    # 1. Connect to the database
    # 2. Use pg_stat_user_tables to estimate dead tuple ratio
    # 3. Optionally use pgstattuple for more accurate estimates
    # 4. Flag tables with > 20% bloat
    # 5. Return findings as MaintenanceTask results
    #
    # Estimation query:
    #   SELECT schemaname, relname,
    #          n_dead_tup,
    #          n_live_tup,
    #          CASE WHEN n_live_tup > 0
    #               THEN round(n_dead_tup::numeric / n_live_tup * 100, 2)
    #               ELSE 0 END AS dead_ratio
    #   FROM pg_stat_user_tables
    #   WHERE n_dead_tup > 10000
    #   ORDER BY n_dead_tup DESC;
    logger.info("Checking for table and index bloat...")
    return [
        MaintenanceTask(
            name="Bloat Check",
            table_or_index="*",
            status="skipped",
            message="Not yet implemented — requires database connection.",
        )
    ]
