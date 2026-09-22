"""Bundle CLI reports filesystem activation separately from runtime application."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import typer
from typer.testing import CliRunner

from mindroom.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

runner = CliRunner()


def test_install_receipt_matches_existing_fingerprint_command(tmp_path: Path) -> None:
    """JSON must be clean and reusable by the existing reload confirmation CLI."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    result = runner.invoke(app, ["config", "install-bundle", str(source), "--target", str(target), "--json"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "installed"
    fingerprint = runner.invoke(app, ["config", "fingerprint", "--path", str(target / "config.yaml")])
    assert receipt["fingerprint"] == fingerprint.stdout.strip()
    assert receipt["config_path"] == str(target / "config.yaml")
    assert len(receipt["digest"]) == 64


def test_invalid_bundle_exits_nonzero_and_never_publishes(tmp_path: Path) -> None:
    """Native YAML failures must become a clear CLI error, preserving active files."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: !include absent.yaml\n")
    target = tmp_path / "active"
    result = runner.invoke(app, ["config", "install-bundle", str(source), "--target", str(target), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "failed"
    assert not target.exists()


def test_invalid_options_return_json_error(tmp_path: Path) -> None:
    """Conflicting modes must be a user-facing error instead of an uncaught exception."""
    result = runner.invoke(
        app,
        [
            "config",
            "install-bundle",
            str(tmp_path / "source"),
            "--target",
            str(tmp_path / "active"),
            "--initialize-only",
            "--force",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "failed"


def test_source_digest_guard_rejects_changed_input(tmp_path: Path) -> None:
    """A supplied source guard must reach native validation before publication."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    result = runner.invoke(
        app,
        [
            "config",
            "install-bundle",
            str(source),
            "--target",
            str(target),
            "--expected-digest",
            "0" * 64,
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "digest" in json.loads(result.stdout)["detail"]
    assert not target.exists()


def test_receipt_output_failure_can_be_retried_without_rotating_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken output after publication must leave a retryable complete installation."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: {}\n")
    target = tmp_path / "active"
    args = ["config", "install-bundle", str(source), "--target", str(target), "--json"]

    def fail_output(*_args: object, **_kwargs: object) -> None:
        msg = "receipt output failed"
        raise BrokenPipeError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(typer, "echo", fail_output)
        assert runner.invoke(app, args).exit_code != 0
    assert (target / "config.yaml").read_text() == "agents: {}\n"
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "unchanged"
    assert not (tmp_path / "active.previous").exists()


def test_run_bootstraps_once_and_loads_installed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime bootstrap must validate before startup and load the newly installed .env."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.yaml").write_text("agents: {}\n")
    (source / ".env").write_text("BUNDLE_VALUE=installed\n")
    target = tmp_path / "active"
    observed = []

    async def start_runtime(*, runtime_paths: RuntimePaths, **_kwargs: object) -> None:
        observed.append(runtime_paths.env_value("BUNDLE_VALUE"))

    monkeypatch.setattr("mindroom.orchestrator.main", start_runtime)
    monkeypatch.setattr("mindroom.cli.main.check_env_keys", lambda *_args, **_kwargs: None)
    args = [
        "run",
        "--no-api",
        "--config",
        str(target / "custom.yaml"),
        "--storage-path",
        str(tmp_path / "state"),
        "--bootstrap-config-bundle",
        str(source),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert observed == ["installed"]
    (target / ".env").write_text("BUNDLE_VALUE=authored\n")
    (source / "custom.yaml").write_text("bad: [")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert observed == ["installed", "authored"]
    assert not (tmp_path / "active.previous").exists()


def test_run_invalid_bootstrap_never_starts_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid initial candidate cannot become active or start the service."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("agents: !include missing.yaml\n")
    monkeypatch.setattr("mindroom.orchestrator.main", lambda **_kwargs: pytest.fail("Runtime must not start"))
    target = tmp_path / "active"
    result = runner.invoke(
        app,
        ["run", "--no-api", "--config", str(target / "config.yaml"), "--bootstrap-config-bundle", str(source)],
    )
    assert result.exit_code == 2, result.output
    assert not target.exists()
