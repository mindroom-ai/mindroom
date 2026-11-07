"""Tests for a plugin module imported by its own name, outside its plugin's load."""

from __future__ import annotations

import importlib
import json
import re
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry import ToolMetadataValidationError, builtin_tool_registry
from tests.conftest import runtime_paths_for
from tests.test_plugins import _bind_runtime_paths, _bump_mtime, _forgetting_modules, _load, _tool_plugin_source

if TYPE_CHECKING:
    from pathlib import Path


def _package_plugin_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path]:
    """Return a config naming the importable plugin package ``stray_pkg``, and the package's tools module."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "stray_pkg"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "stray-pkg", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(_tool_plugin_source("stray_pkg_tool"), encoding="utf-8")
    # Python imports the package through a link, as from a linked install.
    linked_site_packages = tmp_path / "linked-site-packages"
    linked_site_packages.symlink_to(site_packages)
    monkeypatch.syspath_prepend(str(linked_site_packages))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return _bind_runtime_paths(Config(plugins=["stray_pkg"]), config_path), tools_path


@pytest.mark.usefixtures("restored_builtin_tools")
def test_stray_import_of_a_loaded_plugin_tools_module_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin's tools module imported by its own name registers nothing, and the plugin keeps loading its tools."""
    config, tools_path = _package_plugin_config(tmp_path, monkeypatch)

    with _forgetting_modules("stray_pkg"):
        assert "stray_pkg_tool" in _load(config, runtime_paths_for(config)).tools

        with pytest.raises(ToolMetadataValidationError, match=re.escape(str(tools_path.parent.resolve()))):
            importlib.import_module("stray_pkg.tools")
        assert "stray_pkg_tool" not in builtin_tool_registry()

        # The plugin's next load executes its tools module again and still registers the tool.
        _bump_mtime(tools_path)
        assert "stray_pkg_tool" in _load(config, runtime_paths_for(config)).tools


@pytest.mark.usefixtures("restored_builtin_tools")
def test_a_module_outside_loaded_plugins_still_registers_a_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading a plugin leaves registrations from modules outside its directory built-in tools."""
    config, _ = _package_plugin_config(tmp_path, monkeypatch)
    with _forgetting_modules("stray_pkg"):
        _load(config, runtime_paths_for(config))

    @register_tool_with_metadata(
        name="registered_outside_plugins",
        display_name="Registered outside plugins",
        description="A tool this test module registers",
        category=ToolCategory.DEVELOPMENT,
        file_access=ToolFileAccess.NONE,
    )
    def outside_tools() -> type:
        return object

    assert builtin_tool_registry().get("registered_outside_plugins") is not None
