"""Tests for plugin loading and registration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import mindroom.tool_system.plugins as plugin_module
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import EVENT_MESSAGE_RECEIVED, HookRegistry
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from tests.conftest import bind_runtime_paths, runtime_paths_for


def _bind_runtime_paths(config: Config, config_path: Path) -> Config:
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    return bind_runtime_paths(config, runtime_paths)


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
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
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
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        assert [plugin.name for plugin in plugins] == ["demo-plugin"]
        assert "demo_plugin" in _TOOL_REGISTRY
        tool = get_tool_by_name("demo_plugin", runtime_paths_for(config), worker_target=None)
        assert tool.name == "demo"
        assert (plugin_root / "skills").resolve() in _get_plugin_skill_roots()
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
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
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
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
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        assert [plugin.name for plugin in plugins] == ["demo-pkg"]
        assert plugins[0].root == plugin_root.resolve()
        assert "demo_pkg_tool" in _TOOL_REGISTRY
        tool = get_tool_by_name("demo_pkg_tool", runtime_paths_for(config), worker_target=None)
        assert tool.name == "demo_pkg"
        assert (plugin_root / "skills").resolve() in _get_plugin_skill_roots()
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
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
        resolved = plugin_module._resolve_plugin_root("./plugins/demo", resolve_runtime_paths(config_path=config_path))
    finally:
        os.chdir(original_cwd)

    assert resolved == plugin_root.resolve()


def test_load_plugins_uses_bound_runtime_paths(tmp_path: Path) -> None:
    """Plugin loading should resolve relative paths from the config's bound runtime context."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    plugins = load_plugins(config, runtime_paths_for(config))

    assert [plugin.name for plugin in plugins] == ["demo-plugin"]


def test_load_plugins_rejects_manifest_name_with_colon(tmp_path: Path) -> None:
    """Colon-containing plugin manifest names should fail during config/runtime binding."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "origin:plugin", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    with pytest.raises(ValueError, match="Plugin manifest name must not contain ':'"):
        _bind_runtime_paths(Config(plugins=["./plugins/bad-name"]), config_path)


def test_load_plugins_rejects_duplicate_manifest_names_before_materialization(tmp_path: Path) -> None:
    """Duplicate plugin manifest names should fail before any plugin module imports run."""
    first_root = tmp_path / "plugins" / "first"
    second_root = tmp_path / "plugins" / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    manifest = {"name": "shared-plugin", "tools_module": "tools.py", "skills": []}
    (first_root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (second_root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    first_import_marker = tmp_path / "first-imported"
    second_import_marker = tmp_path / "second-imported"
    (first_root / "tools.py").write_text(
        f"from pathlib import Path\nPath({str(first_import_marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (second_root / "tools.py").write_text(
        "raise RuntimeError('broken duplicate should not import')\n",
        encoding="utf-8",
    )
    (second_root / "hooks.py").write_text(
        f"from pathlib import Path\nPath({str(second_import_marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        with pytest.raises(ValueError, match="Duplicate plugin manifest names configured"):
            _bind_runtime_paths(Config(plugins=["./plugins/first", "./plugins/second"]), config_path)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)

    assert not first_import_marker.exists()
    assert not second_import_marker.exists()


def test_config_normalizes_string_and_object_plugin_entries() -> None:
    """Root config should normalize bare strings into structured plugin entries."""
    config = Config(
        plugins=[
            "./plugins/simple",
            {
                "path": "./plugins/advanced",
                "settings": {"api_key": "secret"},
                "hooks": {"audit": {"enabled": False}},
            },
        ],
    )

    assert [plugin.path for plugin in config.plugins] == ["./plugins/simple", "./plugins/advanced"]
    assert config.plugins[0].settings == {}
    assert config.plugins[1].settings == {"api_key": "secret"}
    assert config.plugins[1].hooks["audit"].enabled is False


def test_load_plugins_discovers_hooks_from_tools_module_when_hooks_module_missing(tmp_path: Path) -> None:
    """Decorated hooks in tools_module should be auto-discovered."""
    plugin_root = tmp_path / "plugins" / "tools-hooks"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "tools-hooks", "tools_module": "plugin.py"}),
        encoding="utf-8",
    )
    (plugin_root / "plugin.py").write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    ctx.suppress = True\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/tools-hooks"]), config_path)

    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)

    assert [hook.hook_name for hook in registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["audit"]


def test_load_plugins_discovers_hooks_from_dedicated_hooks_module(tmp_path: Path) -> None:
    """A manifest hooks_module should be scanned independently from tools_module."""
    plugin_root = tmp_path / "plugins" / "separate-hooks"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "separate-hooks",
                "tools_module": "tools.py",
                "hooks_module": "hooks.py",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text("TOOLS_IMPORTED = True\n", encoding="utf-8")
    (plugin_root / "hooks.py").write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received', name='from-hooks-module')\n"
        "async def audit(ctx):\n"
        "    del ctx\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/separate-hooks"]), config_path)

    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)

    assert [hook.hook_name for hook in registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["from-hooks-module"]


def test_load_plugins_reuses_same_module_when_tools_and_hooks_share_file(tmp_path: Path) -> None:
    """One shared tools/hooks file should be imported only once."""
    plugin_root = tmp_path / "plugins" / "same-file"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "same-file",
                "tools_module": "plugin.py",
                "hooks_module": "plugin.py",
            },
        ),
        encoding="utf-8",
    )
    counter_path = plugin_root / "imports.txt"
    (plugin_root / "plugin.py").write_text(
        "from pathlib import Path\n"
        "from mindroom.hooks import hook\n"
        "\n"
        "_COUNTER = Path(__file__).with_name('imports.txt')\n"
        "count = int(_COUNTER.read_text() or '0') if _COUNTER.exists() else 0\n"
        "_COUNTER.write_text(str(count + 1))\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(
        Config(plugins=[{"path": "./plugins/same-file"}]),
        config_path,
    )

    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_tool_cache = plugin_module._TOOL_MODULE_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    try:
        plugins = load_plugins(config, runtime_paths_for(config))
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._TOOL_MODULE_CACHE.clear()
        plugin_module._TOOL_MODULE_CACHE.update(original_tool_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)

    assert len(plugins[0].discovered_hooks) == 1
    assert counter_path.read_text(encoding="utf-8") == "1"
