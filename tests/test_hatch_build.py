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
