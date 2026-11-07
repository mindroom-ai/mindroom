"""Tests for a plugin module imported by its own name, outside its plugin's load."""

from __future__ import annotations

import importlib
import re
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry import ToolMetadataValidationError, builtin_tool_registry
from tests.conftest import runtime_paths_for
from tests.test_plugins import (
    _bind_runtime_paths,
    _forgetting_modules,
    _load,
    _minimal_runtime_paths,
    _write_working_tool_plugin,
)

if TYPE_CHECKING:
    from pathlib import Path


_HELPER_TOOLS_SOURCES = {
    "secondary_gmail": (
        "from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig, register_google_workspace_tools\n"
        "register_google_workspace_tools(GoogleWorkspaceConfig(name='secondary', display_name='Secondary', "
        "client_config_service='secondary_google_oauth_client', allowed_hosted_domains=('secondary.example',), "
        "services=('gmail',)))\n"
    ),
    "partner_atlassian": (
        "from mindroom.tool_system.atlassian_connections import (\n"
        "    AtlassianConnectionConfig,\n"
        "    register_atlassian_connection_tools,\n"
        ")\n"
        "register_atlassian_connection_tools(AtlassianConnectionConfig(name='partner', display_name='Partner', "
        "site_url='https://partner.atlassian.net', products=('confluence',)))\n"
    ),
}


def _package_plugin_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools_source: str | None = None,
) -> tuple[Config, Path]:
    """Return a config naming the importable plugin package ``stray_pkg``, and the package's directory.

    Its tools module registers ``stray_pkg_tool``, or runs ``tools_source`` instead.
    """
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "stray_pkg"
    _write_working_tool_plugin(plugin_root, plugin_name="stray-pkg", tool_name="stray_pkg_tool")
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    if tools_source is not None:
        (plugin_root / "tools.py").write_text(tools_source, encoding="utf-8")
    # Python imports the package through a link, as from a linked install.
    linked_site_packages = tmp_path / "linked-site-packages"
    linked_site_packages.symlink_to(site_packages)
    monkeypatch.syspath_prepend(str(linked_site_packages))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return _bind_runtime_paths(Config(plugins=["stray_pkg"]), config_path), plugin_root


@pytest.mark.usefixtures("restored_builtin_tools")
def test_stray_import_of_a_loaded_plugin_tools_module_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin's tools module imported by its own name registers nothing, and the plugin keeps loading its tools."""
    config, plugin_root = _package_plugin_config(tmp_path, monkeypatch)

    with _forgetting_modules("stray_pkg"):
        assert "stray_pkg_tool" in _load(config, runtime_paths_for(config)).tools

        with pytest.raises(ToolMetadataValidationError, match=re.escape(str(plugin_root.resolve()))):
            importlib.import_module("stray_pkg.tools")
        assert "stray_pkg_tool" not in builtin_tool_registry()

        # The plugin's next load executes its tools module again and still registers the tool.
        assert "stray_pkg_tool" in _load(config, runtime_paths_for(config)).tools


@pytest.mark.usefixtures("restored_builtin_tools")
def test_a_module_outside_loaded_plugins_still_registers_a_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading a plugin leaves registrations from modules outside its directory built-in tools."""
    config, _ = _package_plugin_config(tmp_path, monkeypatch)
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

    assert "registered_outside_plugins" in builtin_tool_registry()


@pytest.mark.parametrize("tool_name", sorted(_HELPER_TOOLS_SOURCES))
@pytest.mark.usefixtures("restored_builtin_tools")
def test_stray_import_of_a_module_registering_through_a_plugin_helper_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    """A plugin helper whose factories live in MindRoom's modules registers nothing outside a plugin load."""
    config, _ = _package_plugin_config(tmp_path, monkeypatch, _HELPER_TOOLS_SOURCES[tool_name])

    with _forgetting_modules("stray_pkg"):
        assert tool_name in _load(config, runtime_paths_for(config)).tools

        with pytest.raises(ToolMetadataValidationError, match="only while a plugin module loads"):
            importlib.import_module("stray_pkg.tools")
        assert tool_name not in builtin_tool_registry()
        assert tool_name in _load(config, runtime_paths_for(config)).tools


@pytest.mark.usefixtures("restored_builtin_tools")
def test_a_plugin_imported_while_a_later_plugin_entry_resolves_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later entry's package that imports an earlier plugin's tools module as it resolves registers nothing."""
    site_packages = tmp_path / "site-packages"
    _write_working_tool_plugin(site_packages / "first_pkg", plugin_name="first-pkg", tool_name="first_pkg_tool")
    (site_packages / "first_pkg" / "__init__.py").write_text("", encoding="utf-8")
    # Resolving python:second_ns.plugin imports second_ns, which imports the first plugin's tools module.
    (site_packages / "second_ns" / "plugin").mkdir(parents=True)
    (site_packages / "second_ns" / "__init__.py").write_text("import first_pkg.tools\n", encoding="utf-8")
    (site_packages / "second_ns" / "plugin" / "mindroom.plugin.json").write_text('{"name": "second"}', encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))
    config = Config(plugins=["first_pkg", "python:second_ns.plugin"])
    runtime_paths = _minimal_runtime_paths(tmp_path)

    with _forgetting_modules("first_pkg", "second_ns"):
        plugins = _load(config, runtime_paths)

    assert [plugin.name for plugin in plugins.loaded_plugins] == ["first-pkg"]
    assert [skipped.path for skipped in plugins.generation.skipped] == ["python:second_ns.plugin"]
    assert "first_pkg_tool" in plugins.tools
    assert "first_pkg_tool" not in builtin_tool_registry()
