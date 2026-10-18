"""Tests for the ``rollback`` CLI command and its --delete-green safety."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from rds_migrate.cli import cli


def _run(args: list[str], input_text: str | None = None) -> tuple[object, MagicMock]:
    client = MagicMock()
    with patch("rds_migrate.cli.get_rds_client", return_value=client):
        result = CliRunner().invoke(cli, ["rollback", "--deployment-id", "bgd-12345", *args], input=input_text)
    return result, client


class TestRollbackCli:
    def test_keeps_green_by_default(self) -> None:
        result, client = _run([])
        assert result.exit_code == 0  # type: ignore[attr-defined]
        client.delete_blue_green_deployment.assert_called_once_with(
            BlueGreenDeploymentIdentifier="bgd-12345",
            DeleteTarget=False,
        )

    def test_default_does_not_prompt(self) -> None:
        result, _ = _run([])
        assert "cannot be undone" not in result.output  # type: ignore[attr-defined]

    def test_delete_green_confirmed(self) -> None:
        result, client = _run(["--delete-green"], input_text="y\n")
        assert result.exit_code == 0  # type: ignore[attr-defined]
        client.delete_blue_green_deployment.assert_called_once_with(
            BlueGreenDeploymentIdentifier="bgd-12345",
            DeleteTarget=True,
        )

    def test_delete_green_declined_makes_no_call(self) -> None:
        result, client = _run(["--delete-green"], input_text="n\n")
        assert result.exit_code != 0  # type: ignore[attr-defined]
        client.delete_blue_green_deployment.assert_not_called()

    def test_yes_skips_prompt(self) -> None:
        result, client = _run(["--delete-green", "--yes"])
        assert result.exit_code == 0  # type: ignore[attr-defined]
        assert client.delete_blue_green_deployment.call_args.kwargs["DeleteTarget"] is True
