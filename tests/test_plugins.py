"""Tests for plugin loading and registration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import mindroom.plugins as plugin_module
from mindroom.config.main import Config
from mindroom.plugins import load_plugins
from mindroom.skills import get_plugin_skill_roots, set_plugin_skill_roots
from mindroom.tools_metadata import TOOL_METADATA, TOOL_REGISTRY, get_tool_by_name

if TYPE_CHECKING:
    import pytest


def test_load_plugins_registers_tools_and_skills(tmp_path: Path) -> None:
    """Load a plugin that registers a tool and provides a skills directory."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)

    manifest = {
        "name": "demo-plugin",
        "tools_module": "tools.py",
        "skills": ["skills"],
    }
    (plugin_root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tools_metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='demo_plugin',\n"
        "    display_name='Demo Plugin',\n"
        "    description='Demo plugin tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    skill_dir = plugin_root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = Config(plugins=["./plugins/demo"])

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()

    try:
        plugins = load_plugins(config, config_path=config_path)
        assert [plugin.name for plugin in plugins] == ["demo-plugin"]
        assert "demo_plugin" in TOOL_REGISTRY
        tool = get_tool_by_name("demo_plugin")
        assert tool.name == "demo"
        assert (plugin_root / "skills").resolve() in get_plugin_skill_roots()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_from_python_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Load a plugin from an importable Python package."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "demo_pkg"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")

    manifest = {
        "name": "demo-pkg",
        "tools_module": "tools.py",
        "skills": ["skills"],
    }
    (plugin_root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tools_metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo_pkg', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='demo_pkg_tool',\n"
        "    display_name='Demo Package Plugin',\n"
        "    description='Demo package plugin tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_pkg_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    skill_dir = plugin_root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(site_packages))

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = Config(plugins=["demo_pkg"])

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()

    try:
        plugins = load_plugins(config, config_path=config_path)
        assert [plugin.name for plugin in plugins] == ["demo-pkg"]
        assert plugins[0].root == plugin_root.resolve()
        assert "demo_pkg_tool" in TOOL_REGISTRY
        tool = get_tool_by_name("demo_pkg_tool")
        assert tool.name == "demo_pkg"
        assert (plugin_root / "skills").resolve() in get_plugin_skill_roots()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_resolve_plugin_root_relative_to_config_dir_not_cwd(tmp_path: Path) -> None:
    """Relative plugin paths should resolve from the config directory."""
    config_dir = tmp_path / "cfg"
    plugin_root = config_dir / "plugins" / "demo"
    plugin_root.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    original_cwd = Path.cwd()
    other_cwd = tmp_path / "other"
    other_cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(other_cwd)
    try:
        resolved = plugin_module._resolve_plugin_root("./plugins/demo", config_path=config_path)
    finally:
        os.chdir(original_cwd)

    assert resolved == plugin_root.resolve()
