"""Tests for `mindroom plugins check` on the plugin forms a `plugins:` entry accepts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _write_manifest(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    (root / "mindroom.plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")


@pytest.fixture
def working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory, beside an importable package that is a plugin and ships two more."""
    package_root = tmp_path / "site-packages" / "check_demo_pkg"
    _write_manifest(package_root, "packaged-root")
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    _write_manifest(package_root / "bundled", "packaged-bundled")
    _write_manifest(package_root / "plugins" / "demo", "packaged-demo")
    monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
    working_directory = tmp_path / "work"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    return working_directory


@pytest.mark.parametrize(
    ("spec", "plugin_name"),
    [
        ("pkg:check_demo_pkg:plugins/demo", "packaged-demo"),
        ("python:check_demo_pkg", "packaged-root"),
        ("check_demo_pkg:bundled", "packaged-bundled"),
    ],
)
@pytest.mark.usefixtures("working_directory")
def test_plugins_check_cli_checks_a_plugin_in_an_installed_package(spec: str, plugin_name: str) -> None:
    """Package specs, with or without a `:subpath`, check the plugin the loader would load."""
    result = runner.invoke(app, ["plugins", "check", spec])

    assert result.exit_code == 0, result.output
    assert f"Plugin is compatible: {plugin_name}" in result.stdout


def test_plugins_check_cli_prefers_a_working_directory_plugin_over_a_package(working_directory: Path) -> None:
    """A bare name resolves against the working directory first, as a config entry does against its directory."""
    _write_manifest(working_directory / "check_demo_pkg", "local-demo")

    result = runner.invoke(app, ["plugins", "check", "check_demo_pkg"])

    assert result.exit_code == 0, result.output
    assert "Plugin is compatible: local-demo" in result.stdout


@pytest.mark.usefixtures("working_directory")
def test_plugins_check_cli_reports_a_package_spec_that_does_not_resolve() -> None:
    """A package spec naming no importable package fails with the loader's reason and no traceback."""
    # The console wraps at the terminal width, which would split the message.
    result = runner.invoke(
        app,
        ["plugins", "check", "pkg:missing_check_demo_pkg:plugins/demo"],
        env={"COLUMNS": "1000"},
    )

    assert result.exit_code == 1
    assert "Configured plugin module could not be resolved: pkg:missing_check_demo_pkg:plugins/demo" in result.stdout
    assert "Traceback (most recent call last)" not in result.output
