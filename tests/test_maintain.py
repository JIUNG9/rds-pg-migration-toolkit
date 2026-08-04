"""
Unit tests for post-migration maintenance.

Tests the maintain module's ANALYZE, REINDEX, and bloat detection
functionality with graceful psycopg2 degradation.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


from rds_migrate.maintain import (
    MaintenanceReport,
    MaintenanceTask,
    analyze_all_tables,
    detect_bloat,
    reindex_critical_indexes,
    run_maintenance,
)


class TestMaintenanceReport:
    """Tests for the MaintenanceReport dataclass."""

    def test_completed_count(self) -> None:
        """Should count completed tasks."""
        report = MaintenanceReport(
            instance_id="test-db",
            tasks=[
                MaintenanceTask("ANALYZE", "public.users", "completed"),
                MaintenanceTask("ANALYZE", "public.orders", "completed"),
                MaintenanceTask("ANALYZE", "public.logs", "failed", message="timeout"),
            ],
        )
        assert report.completed_count == 2
        assert report.failed_count == 1

    def test_str_representation(self) -> None:
        """Should produce readable output."""
        report = MaintenanceReport(
            instance_id="prod-db",
            tasks=[
                MaintenanceTask("ANALYZE", "public.users", "completed", duration_ms=150.0),
            ],
        )
        output = str(report)
        assert "prod-db" in output
        assert "1 total" in output
        assert "1 completed" in output


class TestRunMaintenance:
    """Tests for the run_maintenance orchestrator."""

    def test_no_tasks_requested(self) -> None:
        """Should return empty report when no flags are set."""
        report = run_maintenance("test-db")
        assert len(report.tasks) == 0

    def test_all_flags(self) -> None:
        """Should include all task types when all flags are set."""
        report = run_maintenance(
            "test-db", run_analyze=True, run_reindex=True, check_bloat=True,
        )
        task_names = {t.name for t in report.tasks}
        assert "ANALYZE" in task_names
        assert "REINDEX" in task_names
        assert "Bloat Check" in task_names


class TestAnalyzeAllTables:
    """Tests for the analyze_all_tables function."""

    def test_analyze_without_psycopg2(self) -> None:
        """Should return SKIP with install instructions when psycopg2 is missing."""
        tasks = analyze_all_tables("host=localhost dbname=test")
        assert len(tasks) == 1
        assert tasks[0].status == "skipped"
        assert "pip install" in tasks[0].message
        assert "rds-pg-migration-toolkit[db]" in tasks[0].message

    def test_analyze_runs_on_tables(self) -> None:
        """Should ANALYZE each user table and return completed tasks."""
        mock_psycopg2 = MagicMock()
        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("public", "users"),
            ("public", "orders"),
        ]
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            tasks = analyze_all_tables("host=localhost dbname=test")
        assert len(tasks) == 2
        assert all(t.status == "completed" for t in tasks)
        assert tasks[0].table_or_index == "public.users"
        assert tasks[1].table_or_index == "public.orders"

    def test_connection_error(self) -> None:
        """Should handle connection failures gracefully."""
        mock_psycopg2 = MagicMock()
        mock_psycopg2.connect.side_effect = Exception("Connection refused")
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            tasks = analyze_all_tables("host=bad port=5432 dbname=test")
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
        assert "Connection error" in tasks[0].message


class TestReindexCriticalIndexes:
    """Tests for the reindex_critical_indexes function."""

    def test_reindex_skips_small(self) -> None:
        """Should skip indexes below the size threshold (1GB default)."""
        mock_psycopg2 = MagicMock()
        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("public", "users_pkey", 50 * 1024 * 1024),
            ("public", "orders_pkey", 100 * 1024 * 1024),
            ("public", "items_idx", 500 * 1024 * 1024),
        ]
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            tasks = reindex_critical_indexes("host=localhost dbname=test")
        assert len(tasks) == 1
        assert tasks[0].status == "skipped"
        assert "threshold" in tasks[0].message.lower()

    def test_reindex_processes_large_indexes(self) -> None:
        """Should REINDEX indexes above the threshold."""
        mock_psycopg2 = MagicMock()
        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("public", "big_idx", 2 * 1024 * 1024 * 1024),
            ("public", "small_idx", 100 * 1024 * 1024),
        ]
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            tasks = reindex_critical_indexes("host=localhost dbname=test")
        assert len(tasks) == 1
        assert tasks[0].status == "completed"
        assert tasks[0].table_or_index == "public.big_idx"


class TestDetectBloat:
    """Tests for the detect_bloat function."""

    def test_bloat_detection(self) -> None:
        """Should flag tables with dead tuple ratio above threshold (20%)."""
        mock_psycopg2 = MagicMock()
        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("public", "audit_log", 500000, 1000000, 50.00),
            ("public", "sessions", 100000, 200000, 50.00),
        ]
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            tasks = detect_bloat("host=localhost dbname=test")
        assert len(tasks) == 2
        assert all(t.status == "completed" for t in tasks)
        assert "50.0%" in tasks[0].message
        assert "VACUUM" in tasks[0].message
        assert tasks[0].table_or_index == "public.audit_log"

    def test_no_bloat_detected(self) -> None:
        """Should report clean when no tables exceed threshold."""
        mock_psycopg2 = MagicMock()
        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("public", "users", 15000, 1000000, 1.50),
        ]
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            tasks = detect_bloat("host=localhost dbname=test")
        assert len(tasks) == 1
        assert "No tables exceed" in tasks[0].message
