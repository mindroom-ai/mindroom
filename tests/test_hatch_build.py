"""Tests for the custom Hatch frontend build hook."""

import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def hatch_build_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import ``hatch_build`` with a stub Hatchling interface."""
    interface_module = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    interface_module.BuildHookInterface = type("BuildHookInterface", (), {})

    monkeypatch.setitem(sys.modules, "hatchling", types.ModuleType("hatchling"))
    monkeypatch.setitem(sys.modules, "hatchling.builders", types.ModuleType("hatchling.builders"))
    monkeypatch.setitem(sys.modules, "hatchling.builders.hooks", types.ModuleType("hatchling.builders.hooks"))
    monkeypatch.setitem(
        sys.modules,
        "hatchling.builders.hooks.plugin",
        types.ModuleType("hatchling.builders.hooks.plugin"),
    )
    monkeypatch.setitem(sys.modules, "hatchling.builders.hooks.plugin.interface", interface_module)
    sys.modules.pop("hatch_build", None)
    return importlib.import_module("hatch_build")


def test_get_output_dir_for_standard_build_stays_out_of_dist(
    hatch_build_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """Wheel builds should not leave non-package artifacts in the publish directory."""
    frontend_dir = tmp_path / "frontend"
    build_dir = tmp_path / "dist"

    output_dir = hatch_build_module._get_output_dir(frontend_dir, str(build_dir), "standard")

    assert output_dir == tmp_path / ".frontend-build" / "frontend-dist"
    assert output_dir.parent != build_dir


def test_get_output_dir_for_editable_build_uses_repo_frontend_dist(
    hatch_build_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """Editable installs should still write to the repo frontend dist directory."""
    frontend_dir = tmp_path / "frontend"

    output_dir = hatch_build_module._get_output_dir(frontend_dir, str(tmp_path / "dist"), "editable")

    assert output_dir == frontend_dir / "dist"


def test_run_command_retries_once_before_succeeding(
    hatch_build_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Transient subprocess failures should be retried before surfacing."""
    calls = {"count": 0}
    sleeps: list[float] = []

    def fake_run(cmd: list[str], *, check: bool, cwd: Path) -> None:
        assert check is True
        assert cwd == tmp_path
        calls["count"] += 1
        if calls["count"] == 1:
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(hatch_build_module.subprocess, "run", fake_run)
    monkeypatch.setattr(hatch_build_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    hatch_build_module._run_command(
        ["bun", "install", "--frozen-lockfile"],
        cwd=tmp_path,
        retries=2,
        retry_delay_seconds=0.25,
    )

    assert calls["count"] == 2
    assert sleeps == [0.25]


def test_build_frontend_retries_bun_install_only(
    hatch_build_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the network-dependent bun install step should use retries."""
    frontend_dir = tmp_path / "frontend"
    output_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()

    calls: list[tuple[list[str], Path, int, float]] = []

    def fake_run_command(
        cmd: list[str],
        *,
        cwd: Path,
        retries: int = 1,
        retry_delay_seconds: float = 0.0,
    ) -> None:
        calls.append((cmd, cwd, retries, retry_delay_seconds))

    monkeypatch.setattr(hatch_build_module, "_run_command", fake_run_command)

    hatch_build_module._build_frontend(frontend_dir, output_dir, "/usr/local/bin/bun")

    assert calls == [
        (
            ["/usr/local/bin/bun", "install", "--frozen-lockfile"],
            frontend_dir,
            hatch_build_module._BUN_INSTALL_MAX_ATTEMPTS,
            hatch_build_module._BUN_INSTALL_RETRY_DELAY_SECONDS,
        ),
        (["/usr/local/bin/bun", "run", "tsc"], frontend_dir, 1, 0.0),
        (
            ["/usr/local/bin/bun", "run", "vite", "build", "--outDir", str(output_dir)],
            frontend_dir,
            1,
            0.0,
        ),
    ]
