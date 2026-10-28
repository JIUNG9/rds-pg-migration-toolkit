"""Tests for the ``maintain`` CLI exit code."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from rds_migrate.cli import cli
from rds_migrate.maintain import MaintenanceReport, MaintenanceTask


def _invoke(args: list[str], report: MaintenanceReport):
    with patch("rds_migrate.maintain.run_maintenance", return_value=report):
        return CliRunner().invoke(cli, ["maintain", "--instance", "db-1", *args])


class TestMaintainExitCode:
    def test_exits_1_when_a_task_could_not_run(self) -> None:
        report = MaintenanceReport("db-1", [MaintenanceTask("ANALYZE", "*", "skipped", not_run=True)])
        result = _invoke(["--analyze"], report)
        assert result.exit_code == 1
        assert "SKIPPED" in result.output

    def test_exits_0_when_every_task_ran(self) -> None:
        report = MaintenanceReport("db-1", [MaintenanceTask("ANALYZE", "public.t", "completed")])
        assert _invoke(["--analyze"], report).exit_code == 0

    def test_benign_skip_is_not_an_error(self) -> None:
        report = MaintenanceReport("db-1", [MaintenanceTask("REINDEX", "*", "skipped", message="No indexes")])
        assert _invoke(["--reindex"], report).exit_code == 0


class TestMaintainEndToEnd:
    def test_no_connection_string_exits_1(self) -> None:
        mock_psycopg2 = MagicMock()
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            result = CliRunner().invoke(cli, ["maintain", "--instance", "db-1", "--analyze"])
        assert result.exit_code == 1
        assert "No connection string" in result.output
        mock_psycopg2.connect.assert_not_called()

    def test_missing_psycopg2_exits_1(self) -> None:
        with patch.dict(sys.modules, {"psycopg2": None}):
            result = CliRunner().invoke(
                cli, ["maintain", "--instance", "db-1", "--check-bloat", "--connection-string", "dbname=x"]
            )
        assert result.exit_code == 1
        assert "rds-pg-migration-toolkit[db]" in result.output

    def test_reindex_below_threshold_exits_0(self) -> None:
        mock_psycopg2 = MagicMock()
        cur = mock_psycopg2.connect.return_value.cursor.return_value
        cur.fetchall.return_value = [("public", "small_idx", 1024)]
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
            result = CliRunner().invoke(
                cli, ["maintain", "--instance", "db-1", "--reindex", "--connection-string", "dbname=x"]
            )
        assert result.exit_code == 0
