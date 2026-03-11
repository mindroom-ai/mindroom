"""Tests for the custom Hatch frontend build hook."""

import importlib
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


def test_standard_wheel_build_force_includes_bundled_frontend(
    hatch_build_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Standard wheel builds should bundle the compiled frontend into the package."""
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()

    hook = hatch_build_module.FrontendBuildHook()
    hook.target_name = "wheel"
    hook.root = str(tmp_path)
    hook.directory = str(tmp_path / "dist")

    build_calls: list[tuple[Path, Path, str]] = []

    def _fake_build(frontend_path: Path, output_dir: Path, bun: str) -> None:
        build_calls.append((frontend_path, output_dir, bun))

    monkeypatch.setattr(hatch_build_module, "_build_frontend", _fake_build)
    monkeypatch.setattr(hatch_build_module.shutil, "which", lambda name: "/usr/bin/bun" if name == "bun" else None)

    build_data: dict[str, object] = {}
    hook.initialize("standard", build_data)

    output_dir = tmp_path / ".frontend-build" / "frontend-dist"
    assert build_calls == [(frontend_dir, output_dir, "/usr/bin/bun")]
    assert build_data == {"force_include": {str(output_dir): "mindroom/_frontend"}}
