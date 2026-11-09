"""Tests for a plugin module imported by its own name, outside its plugin's load."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry import builtin_tool_registry
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


# The tool each plugin helper registers, the helper, and a tools module that calls it.
_HELPER_TOOLS_SOURCES = {
    "secondary_gmail": (
        "register_google_workspace_tools",
        "from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig, register_google_workspace_tools\n"
        "register_google_workspace_tools(GoogleWorkspaceConfig(name='secondary', display_name='Secondary', "
        "client_config_service='secondary_google_oauth_client', allowed_hosted_domains=('secondary.example',), "
        "services=('gmail',)))\n",
    ),
    "partner_atlassian": (
        "register_atlassian_connection_tools",
        "from mindroom.tool_system.atlassian_connections import (\n"
        "    AtlassianConnectionConfig,\n"
        "    register_atlassian_connection_tools,\n"
        ")\n"
        "register_atlassian_connection_tools(AtlassianConnectionConfig(name='partner', display_name='Partner', "
        "site_url='https://partner.atlassian.net', products=('confluence',)))\n",
    ),
}


def _warnings(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [log for log in logs if log["log_level"] == "warning"]


def _package_plugin_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools_source: str | None = None,
) -> tuple[Config, Path]:
    """Return a config naming the importable plugin package ``stray_pkg``, and the package's directory.

    Its tools module registers ``stray_pkg_tool``, or runs ``tools_source`` instead. The package's directory is on
    ``sys.path`` too, so ``tools`` names a second copy of its tools module, the stray import.
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
    monkeypatch.syspath_prepend(str(plugin_root))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return _bind_runtime_paths(Config(plugins=["stray_pkg"]), config_path), plugin_root


@pytest.mark.usefixtures("restored_builtin_tools")
def test_stray_import_of_a_loaded_plugin_tools_module_registers_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second copy of a plugin's tools module works but registers nothing, and the plugin keeps its tools."""
    config, plugin_root = _package_plugin_config(tmp_path, monkeypatch)

    with _forgetting_modules("stray_pkg", "tools"):
        assert "stray_pkg_tool" in _load(config, runtime_paths_for(config)).tools

        with capture_logs() as logs:
            stray_tools = importlib.import_module("tools")
        assert stray_tools is not importlib.import_module("stray_pkg.tools")
        assert stray_tools.demo_plugin_tools().__name__ == "DemoTool"
        assert "stray_pkg_tool" not in builtin_tool_registry()
        assert [(log["tool"], log["plugin_root"], log["module"]) for log in _warnings(logs)] == [
            ("stray_pkg_tool", str(plugin_root.resolve()), "tools"),
        ]

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
def test_stray_import_of_a_module_registering_through_a_plugin_helper_registers_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    """A plugin helper whose factories live in MindRoom's modules registers nothing outside a plugin load."""
    helper, tools_source = _HELPER_TOOLS_SOURCES[tool_name]
    config, _ = _package_plugin_config(tmp_path, monkeypatch, tools_source)

    with _forgetting_modules("stray_pkg", "tools"):
        assert tool_name in _load(config, runtime_paths_for(config)).tools

        with capture_logs() as logs:
            importlib.import_module("tools")
        assert tool_name not in builtin_tool_registry()
        assert [log["caller"] for log in _warnings(logs)] == [helper]
        assert tool_name in _load(config, runtime_paths_for(config)).tools


@pytest.mark.usefixtures("restored_builtin_tools")
def test_stray_import_from_a_plugin_whose_manifest_fails_registers_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured plugin skipped for its manifest still keeps its tools module out of the built-in tools."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "broken_pkg"
    _write_working_tool_plugin(plugin_root, plugin_name="broken-pkg", tool_name="broken_pkg_tool")
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text('{"tools_module": "tools.py"}', encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    with _forgetting_modules("broken_pkg"):
        plugins = _load(Config(plugins=["broken_pkg"]), _minimal_runtime_paths(tmp_path))
        assert [skipped.path for skipped in plugins.generation.skipped] == ["broken_pkg"]
        importlib.import_module("broken_pkg.tools")
    assert "broken_pkg_tool" not in builtin_tool_registry()
