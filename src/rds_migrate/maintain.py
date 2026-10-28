"""
Post-migration maintenance for PostgreSQL on RDS/Aurora.

After a blue-green switchover, the new primary has a cold buffer cache and
stale optimizer statistics. This module automates:
  1. ANALYZE on all tables (refresh planner statistics)
  2. REINDEX on critical indexes (rebuild potentially bloated indexes)
  3. Bloat detection (identify tables/indexes needing VACUUM or REINDEX)

IMPORTANT: Do NOT run VACUUM FULL after migration -- it acquires ACCESS EXCLUSIVE
locks and can cause downtime. Use regular VACUUM and ANALYZE instead.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from rds_migrate.utils import DEFAULTS

logger = logging.getLogger(__name__)

_PSYCOPG2_INSTALL_MSG = (
    "psycopg2 is required for maintenance operations. "
    "Install with: pip install rds-pg-migration-toolkit[db]"
)


@dataclass
class MaintenanceTask:
    """A single maintenance task and its result."""

    name: str
    table_or_index: str
    status: str  # "completed", "failed", "skipped"
    duration_ms: float | None = None
    message: str = ""
    # True when a requested task could not run (no psycopg2, no connection string).
    # A "skipped" task with this unset is benign, e.g. no index above the size threshold.
    not_run: bool = False


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

    @property
    def not_run_count(self) -> int:
        return sum(1 for t in self.tasks if t.not_run)

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
    logger.info("Running ANALYZE on all user tables...")

    try:
        import psycopg2
    except ImportError:
        return [
            MaintenanceTask(
                name="ANALYZE",
                table_or_index="*",
                status="skipped",
                message=_PSYCOPG2_INSTALL_MSG,
                not_run=True,
            )
        ]

    if not connection_string:
        return [
            MaintenanceTask(
                name="ANALYZE",
                table_or_index="*",
                status="skipped",
                message="No connection string provided.",
                not_run=True,
            )
        ]

    tasks: list[MaintenanceTask] = []
    try:
        conn = psycopg2.connect(connection_string)
        conn.autocommit = True
        cur = conn.cursor()

        # Get all user tables, ordered by size (largest first)
        cur.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY pg_total_relation_size("
            "'\"' || schemaname || '\".\"' || tablename || '\"') DESC"
        )
        tables = cur.fetchall()

        for schema, table in tables:
            qualified = f"{schema}.{table}"
            start = time.perf_counter()
            try:
                cur.execute(f'ANALYZE VERBOSE "{schema}"."{table}"')
                elapsed_ms = (time.perf_counter() - start) * 1000
                tasks.append(
                    MaintenanceTask(
                        name="ANALYZE",
                        table_or_index=qualified,
                        status="completed",
                        duration_ms=elapsed_ms,
                    )
                )
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                tasks.append(
                    MaintenanceTask(
                        name="ANALYZE",
                        table_or_index=qualified,
                        status="failed",
                        duration_ms=elapsed_ms,
                        message=str(exc),
                    )
                )

        cur.close()
        conn.close()
    except Exception as exc:
        tasks.append(
            MaintenanceTask(
                name="ANALYZE",
                table_or_index="*",
                status="failed",
                message=f"Connection error: {exc}",
            )
        )

    return tasks


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
    logger.info("Running REINDEX on critical indexes...")

    try:
        import psycopg2
    except ImportError:
        return [
            MaintenanceTask(
                name="REINDEX",
                table_or_index="*",
                status="skipped",
                message=_PSYCOPG2_INSTALL_MSG,
                not_run=True,
            )
        ]

    if not connection_string:
        return [
            MaintenanceTask(
                name="REINDEX",
                table_or_index="*",
                status="skipped",
                message="No connection string provided.",
                not_run=True,
            )
        ]

    threshold_bytes = DEFAULTS["reindex_size_threshold_mb"] * 1024 * 1024
    tasks: list[MaintenanceTask] = []

    try:
        conn = psycopg2.connect(connection_string)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute(
            "SELECT schemaname, indexname, "
            "pg_relation_size(quote_ident(schemaname) || '.' || quote_ident(indexname)) AS index_size "
            "FROM pg_indexes "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY index_size DESC"
        )
        indexes = cur.fetchall()

        reindexed = False
        for schema, indexname, index_size in indexes:
            qualified = f"{schema}.{indexname}"
            if index_size < threshold_bytes:
                continue

            reindexed = True
            size_mb = index_size / (1024 * 1024)
            start = time.perf_counter()
            try:
                cur.execute(f'REINDEX INDEX CONCURRENTLY "{schema}"."{indexname}"')
                elapsed_ms = (time.perf_counter() - start) * 1000
                tasks.append(
                    MaintenanceTask(
                        name="REINDEX",
                        table_or_index=qualified,
                        status="completed",
                        duration_ms=elapsed_ms,
                        message=f"Size: {size_mb:.0f}MB",
                    )
                )
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                tasks.append(
                    MaintenanceTask(
                        name="REINDEX",
                        table_or_index=qualified,
                        status="failed",
                        duration_ms=elapsed_ms,
                        message=str(exc),
                    )
                )

        if not reindexed:
            tasks.append(
                MaintenanceTask(
                    name="REINDEX",
                    table_or_index="*",
                    status="skipped",
                    message=f"No indexes above threshold ({DEFAULTS['reindex_size_threshold_mb']}MB).",
                )
            )

        cur.close()
        conn.close()
    except Exception as exc:
        tasks.append(
            MaintenanceTask(
                name="REINDEX",
                table_or_index="*",
                status="failed",
                message=f"Connection error: {exc}",
            )
        )

    return tasks


def detect_bloat(
    connection_string: str | None = None,
) -> list[MaintenanceTask]:
    """Detect table and index bloat after migration.

    Uses pg_stat_user_tables to estimate dead tuple ratios and flag
    tables exceeding the configured threshold. Reports findings but does
    not take action (VACUUM FULL requires careful manual planning).

    Args:
        connection_string: PostgreSQL connection string.

    Returns:
        List of MaintenanceTask results with bloat estimates.
    """
    logger.info("Checking for table and index bloat...")

    try:
        import psycopg2
    except ImportError:
        return [
            MaintenanceTask(
                name="Bloat Check",
                table_or_index="*",
                status="skipped",
                message=_PSYCOPG2_INSTALL_MSG,
                not_run=True,
            )
        ]

    if not connection_string:
        return [
            MaintenanceTask(
                name="Bloat Check",
                table_or_index="*",
                status="skipped",
                message="No connection string provided.",
                not_run=True,
            )
        ]

    threshold_pct = DEFAULTS["bloat_warning_threshold_pct"]
    tasks: list[MaintenanceTask] = []

    try:
        conn = psycopg2.connect(connection_string)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute(
            "SELECT schemaname, relname, n_dead_tup, n_live_tup, "
            "CASE WHEN n_live_tup > 0 "
            "THEN round(n_dead_tup::numeric / n_live_tup * 100, 2) "
            "ELSE 0 END AS dead_ratio "
            "FROM pg_stat_user_tables "
            "WHERE n_dead_tup > 10000 "
            "ORDER BY n_dead_tup DESC"
        )
        rows = cur.fetchall()

        flagged = False
        for schema, relname, n_dead, n_live, dead_ratio in rows:
            qualified = f"{schema}.{relname}"
            dead_ratio_float = float(dead_ratio)
            if dead_ratio_float >= threshold_pct:
                flagged = True
                tasks.append(
                    MaintenanceTask(
                        name="Bloat Check",
                        table_or_index=qualified,
                        status="completed",
                        message=f"Dead tuple ratio: {dead_ratio_float:.1f}% "
                                f"({n_dead:,} dead / {n_live:,} live). "
                                f"Consider VACUUM ANALYZE.",
                    )
                )

        if not flagged:
            tasks.append(
                MaintenanceTask(
                    name="Bloat Check",
                    table_or_index="*",
                    status="completed",
                    message=f"No tables exceed {threshold_pct}% dead tuple ratio.",
                )
            )

        cur.close()
        conn.close()
    except Exception as exc:
        tasks.append(
            MaintenanceTask(
                name="Bloat Check",
                table_or_index="*",
                status="failed",
                message=f"Connection error: {exc}",
            )
        )

    return tasks
