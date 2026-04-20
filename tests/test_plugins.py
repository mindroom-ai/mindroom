"""Tests for plugin loading and registration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import mindroom.tool_system.metadata as metadata_module
import mindroom.tool_system.plugin_imports as plugin_module
import mindroom.tools  # noqa: F401
from mindroom.config.main import Config, ConfigRuntimeValidationError, load_config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.hooks import EVENT_MESSAGE_RECEIVED, HookRegistry
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.plugins import load_plugins, reload_plugins
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


def _minimal_runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def test_validate_with_runtime_does_not_mask_unexpected_tool_validation_type_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected internal type errors should not be rewritten into config validation failures."""
    runtime_paths = _minimal_runtime_paths(tmp_path)
    message = "unexpected backend type error"

    def _raise_type_error(_self: Config, _runtime_paths: object) -> None:
        raise TypeError(message)

    monkeypatch.setattr(Config, "_validate_authored_tool_entries", _raise_type_error)

    with pytest.raises(TypeError, match="unexpected backend type error"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
            runtime_paths,
        )


def test_validate_with_runtime_does_not_mask_unexpected_tool_validation_value_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected internal value errors should escape instead of becoming 422-style config errors."""
    runtime_paths = _minimal_runtime_paths(tmp_path)
    message = "unexpected backend value error"

    def _raise_value_error(_self: Config, _runtime_paths: object) -> None:
        raise ValueError(message)

    monkeypatch.setattr(Config, "_validate_authored_tool_entries", _raise_value_error)

    with pytest.raises(ValueError, match="unexpected backend value error"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
            runtime_paths,
        )


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
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_resolved_tool_metadata_for_runtime_does_not_mutate_live_registry(tmp_path: Path) -> None:
    """Validation should resolve plugin metadata without touching the live registry state."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
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

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        resolved_metadata = metadata_module.resolved_tool_metadata_for_runtime(
            runtime_paths_for(config_with_plugin),
            config_with_plugin,
        )
        assert "demo_plugin" in resolved_metadata
        assert "demo_plugin" not in TOOL_METADATA
        assert original_registry == _TOOL_REGISTRY
        assert original_metadata == TOOL_METADATA
        assert original_module_cache == plugin_module._MODULE_IMPORT_CACHE
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
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
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_from_explicit_python_package_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit python: specs should resolve importable plugin packages."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "demo_pkg"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-pkg", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site_packages))

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["python:demo_pkg"]), config_path)
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        assert [plugin.name for plugin in plugins] == ["demo-pkg"]
        assert plugins[0].root == plugin_root.resolve()
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_explicit_python_plugin_spec_requires_importable_module(tmp_path: Path) -> None:
    """Explicit python: specs should fail closed when the module cannot be resolved."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        with pytest.raises(
            ConfigRuntimeValidationError,
            match="Configured plugin module could not be resolved",
        ):
            _bind_runtime_paths(Config(plugins=["python:missing_demo_pkg"]), config_path)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
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


@pytest.mark.parametrize(
    "plugin_name",
    [
        "origin:plugin",
        "../../escaped",
        "plugin/name",
        r"plugin\\name",
        "UpperCase",
        ".hidden",
        "   ",
    ],
)
def test_load_plugins_rejects_invalid_manifest_name(tmp_path: Path, plugin_name: str) -> None:
    """Invalid plugin manifest names should fail during config/runtime binding."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": plugin_name, "tools_module": None, "skills": []}),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid plugin name"):
        _bind_runtime_paths(Config(plugins=["./plugins/bad-name"]), config_path)


@pytest.mark.parametrize("plugin_name", ["_demo", "-dash", "demo_plugin"])
def test_load_plugins_accepts_safe_manifest_names(tmp_path: Path, plugin_name: str) -> None:
    """Path-safe, provenance-safe plugin names should bind and load successfully."""
    plugin_root = tmp_path / "plugins" / "safe-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": plugin_name, "tools_module": None, "skills": []}),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/safe-name"]), config_path)
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        assert [plugin.name for plugin in plugins] == [plugin_name]
    finally:
        set_plugin_skill_roots(original_plugin_roots)


@pytest.mark.parametrize(
    ("manifest_content", "expected_error"),
    [
        (None, "Plugin manifest missing"),
        ('{"name": "good_plugin",', "Failed to parse plugin manifest"),
        (json.dumps(["not", "an", "object"]), "Plugin manifest must be a JSON object"),
        (
            json.dumps({"name": "good_plugin", "tools_module": 123, "skills": []}),
            "Plugin tools_module must be a string",
        ),
        (
            json.dumps({"name": "good_plugin", "hooks_module": 123, "skills": []}),
            "Plugin hooks_module must be a string",
        ),
        (json.dumps({"name": "good_plugin", "skills": [1]}), "Plugin skills must be a list of strings"),
    ],
)
def test_load_plugins_rejects_malformed_manifests(
    tmp_path: Path,
    manifest_content: str | None,
    expected_error: str,
) -> None:
    """Configured plugins with malformed manifests should fail binding instead of being skipped."""
    plugin_root = tmp_path / "plugins" / "bad-plugin"
    plugin_root.mkdir(parents=True)
    if manifest_content is not None:
        (plugin_root / "mindroom.plugin.json").write_text(manifest_content, encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ValueError, match=expected_error):
        _bind_runtime_paths(Config(plugins=["./plugins/bad-plugin"]), config_path)


def test_load_plugins_rejects_missing_plugin_directory(tmp_path: Path) -> None:
    """Configured plugins must exist on disk instead of being silently skipped."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ValueError, match="Configured plugin path does not exist"):
        _bind_runtime_paths(Config(plugins=["./plugins/missing"]), config_path)


def test_load_plugins_skips_missing_plugin_directory_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime loading should warn and continue when one plugin path is missing."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    mock_logger = MagicMock()
    missing_root = (tmp_path / "plugins" / "missing").resolve()

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        assert load_plugins(Config(plugins=["./plugins/missing"]), runtime_paths) == []
        mock_logger.warning.assert_any_call("Plugin path does not exist, skipping", path=str(missing_root))
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_warns_once_for_repeated_missing_plugin_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated runtime loads should not spam the same missing-plugin warning."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    mock_logger = MagicMock()
    missing_root = (tmp_path / "plugins" / "missing").resolve()
    original_warned_messages = plugin_module._WARNED_PLUGIN_MESSAGES.copy()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        assert load_plugins(Config(plugins=["./plugins/missing"]), runtime_paths) == []
        assert load_plugins(Config(plugins=["./plugins/missing"]), runtime_paths) == []
        matching_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args == ("Plugin path does not exist, skipping",) and call.kwargs == {"path": str(missing_root)}
        ]
        assert len(matching_calls) == 1
    finally:
        plugin_module._WARNED_PLUGIN_MESSAGES.clear()
        plugin_module._WARNED_PLUGIN_MESSAGES.update(original_warned_messages)


def test_load_plugins_rejects_missing_tools_module(tmp_path: Path) -> None:
    """A declared plugin tools module must exist."""
    plugin_root = tmp_path / "plugins" / "bad-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ValueError, match="Plugin tools module not found"):
        _bind_runtime_paths(Config(plugins=["./plugins/bad-plugin"]), config_path)


def test_validate_with_runtime_does_not_mutate_plugin_skill_roots(tmp_path: Path) -> None:
    """Runtime validation should not swap global plugin skill roots before activation."""
    original_plugin_roots = _get_plugin_skill_roots()
    sentinel_root = tmp_path / "existing-plugin-skills"
    sentinel_root.mkdir()
    set_plugin_skill_roots([sentinel_root])

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": None, "skills": ["skills"]}),
        encoding="utf-8",
    )
    (plugin_root / "skills").mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    try:
        _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
        assert _get_plugin_skill_roots() == [sentinel_root.resolve()]
    finally:
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_warns_once_for_repeated_non_bundled_plugin_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated plugin registry rebuilds should not repeat the same non-bundled warning."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": None, "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook(event='message:received')\n"
        "async def demo_hook(context):\n"
        "    return None\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    config = Config(plugins=["./plugins/demo"])

    mock_logger = MagicMock()
    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_warned_messages = plugin_module._WARNED_PLUGIN_MESSAGES.copy()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        assert [plugin.name for plugin in load_plugins(config, runtime_paths)] == ["demo-plugin"]
        assert [plugin.name for plugin in load_plugins(config, runtime_paths)] == ["demo-plugin"]
        matching_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args == ("Loading non-bundled plugin",) and call.kwargs == {"path": str(plugin_root.resolve())}
        ]
        assert len(matching_calls) == 1
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        plugin_module._WARNED_PLUGIN_MESSAGES.clear()
        plugin_module._WARNED_PLUGIN_MESSAGES.update(original_warned_messages)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_revalidates_skill_dirs_when_manifest_cache_is_warm(tmp_path: Path) -> None:
    """Warm manifest cache entries must not preserve deleted skill directories."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": None, "skills": ["skills"]}),
        encoding="utf-8",
    )
    skill_dir = plugin_root / "skills"
    skill_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    try:
        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["demo-plugin"]
        skill_dir.rmdir()
        assert load_plugins(config, runtime_paths_for(config)) == []
        assert _get_plugin_skill_roots() == []
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_validate_with_runtime_does_not_leak_plugin_tools_after_failure(tmp_path: Path) -> None:
    """Runtime validation should roll back plugin tool registration when validation fails later."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='leaked_plugin_tool',\n"
        "    display_name='Leaked Plugin Tool',\n"
        "    description='Should not leak from failed validation',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        bad_config = Config.model_validate(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/demo"],
                "defaults": {"tools": ["missing_tool"]},
            },
        )
        with pytest.raises(ValueError, match="Unknown tool 'missing_tool'"):
            _bind_runtime_paths(bad_config, config_path)

        assert "leaked_plugin_tool" not in _TOOL_REGISTRY
        assert "leaked_plugin_tool" not in TOOL_METADATA

        follow_up_config = Config.model_validate(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "test",
                        "tools": ["leaked_plugin_tool"],
                    },
                },
                "plugins": [],
            },
        )
        with pytest.raises(ValueError, match="Unknown tool 'leaked_plugin_tool'"):
            _bind_runtime_paths(follow_up_config, config_path)
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)


def test_validate_with_runtime_does_not_mutate_live_tool_registry_on_success(tmp_path: Path) -> None:
    """Successful runtime validation should not publish plugin tools into the live registry."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='validated_plugin_tool',\n"
        "    display_name='Validated Plugin Tool',\n"
        "    description='Should stay out of the live registry during validation',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    authored_config = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "test",
                "tools": ["validated_plugin_tool"],
            },
        },
        "plugins": ["./plugins/demo"],
    }

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        validated = Config.validate_with_runtime(authored_config, runtime_paths)
        assert validated.get_agent("assistant").tool_names == ["validated_plugin_tool"]
        assert "validated_plugin_tool" not in _TOOL_REGISTRY
        assert "validated_plugin_tool" not in TOOL_METADATA
        assert original_module_cache == plugin_module._MODULE_IMPORT_CACHE
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)


def test_validate_with_runtime_rejects_invalid_dedicated_hooks_module(tmp_path: Path) -> None:
    """Runtime validation should fail when a dedicated hooks module cannot be imported."""
    plugin_root = tmp_path / "plugins" / "broken-hooks"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "broken-hooks",
                "tools_module": "tools.py",
                "hooks_module": "hooks.py",
                "skills": [],
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text("TOOLS_IMPORTED = True\n", encoding="utf-8")
    (plugin_root / "hooks.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )

    with pytest.raises(ConfigRuntimeValidationError, match=r"hooks\.py"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/broken-hooks"],
            },
            runtime_paths,
        )


def test_validate_with_runtime_does_not_mutate_live_registry_for_package_helper_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation should keep helper-module plugin tools out of the live registry."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "demo_pkg"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-pkg", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helpers.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class HelperTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='helper_toolkit', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='helper_tool',\n"
        "    display_name='Helper Tool',\n"
        "    description='Defined in an imported helper module',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def helper_tools():\n"
        "    return HelperTool\n",
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text("from demo_pkg.helpers import helper_tools\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(site_packages))

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        validated = Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "test",
                        "tools": ["helper_tool"],
                    },
                },
                "plugins": ["demo_pkg"],
            },
            runtime_paths,
        )
        assert validated.get_agent("assistant").tool_names == ["helper_tool"]
        assert "helper_tool" not in _TOOL_REGISTRY
        assert "helper_tool" not in TOOL_METADATA
        assert original_registry == _TOOL_REGISTRY
        assert original_metadata == TOOL_METADATA
        assert original_module_cache == plugin_module._MODULE_IMPORT_CACHE
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)


def test_load_plugins_removes_tools_for_successfully_removed_plugins(tmp_path: Path) -> None:
    """Removing a previously loaded plugin should also remove its tool registrations."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='removed_plugin_tool',\n"
        "    display_name='Removed Plugin Tool',\n"
        "    description='Should disappear when the plugin is removed',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    config_without_plugin = _bind_runtime_paths(Config(plugins=[]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
            "demo_plugin",
        ]
        assert "removed_plugin_tool" in _TOOL_REGISTRY
        assert "removed_plugin_tool" in TOOL_METADATA

        assert load_plugins(config_without_plugin, runtime_paths_for(config_without_plugin)) == []
        assert "removed_plugin_tool" not in _TOOL_REGISTRY
        assert "removed_plugin_tool" not in TOOL_METADATA

        follow_up_config = Config.model_validate(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "test",
                        "tools": ["removed_plugin_tool"],
                    },
                },
                "plugins": [],
            },
        )
        with pytest.raises(ValueError, match="Unknown tool 'removed_plugin_tool'"):
            _bind_runtime_paths(follow_up_config, config_path)
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_re_registers_tools_when_plugin_is_re_enabled(tmp_path: Path) -> None:
    """Re-enabling an unchanged plugin in the same process should restore its tools."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='toggled_plugin_tool',\n"
        "    display_name='Toggled Plugin Tool',\n"
        "    description='Should return when the plugin is re-enabled',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    config_without_plugin = _bind_runtime_paths(Config(plugins=[]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
            "demo_plugin",
        ]
        assert "toggled_plugin_tool" in _TOOL_REGISTRY

        assert load_plugins(config_without_plugin, runtime_paths_for(config_without_plugin)) == []
        assert "toggled_plugin_tool" not in _TOOL_REGISTRY

        assert [plugin.name for plugin in load_plugins(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
            "demo_plugin",
        ]
        assert "toggled_plugin_tool" in _TOOL_REGISTRY

        follow_up_config = Config.model_validate(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "test",
                        "tools": ["toggled_plugin_tool"],
                    },
                },
                "plugins": ["./plugins/demo"],
            },
        )
        bound = _bind_runtime_paths(follow_up_config, config_path)
        assert bound.get_agent("assistant").tool_names == ["toggled_plugin_tool"]
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_preserves_metadata_only_built_in_tools(tmp_path: Path) -> None:
    """Syncing the plugin overlay must keep metadata-only built-ins visible."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_without_plugins = _bind_runtime_paths(Config(plugins=[]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert load_plugins(config_without_plugins, runtime_paths_for(config_without_plugins)) == []
        for tool_name in ("memory", "delegate", "self_config", "compact_context"):
            assert tool_name in TOOL_METADATA
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_removes_stale_tools_when_enabled_plugin_changes_exports(tmp_path: Path) -> None:
    """Reloading an enabled plugin should drop tool names it no longer registers."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
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
        "    name='old_tool',\n"
        "    display_name='Old Tool',\n"
        "    description='Old plugin tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["demo_plugin"]
        assert "old_tool" in _TOOL_REGISTRY

        tools_path.write_text(
            "from agno.tools import Toolkit\n"
            "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
            "\n"
            "class DemoTool(Toolkit):\n"
            "    def __init__(self) -> None:\n"
            "        super().__init__(name='demo', tools=[])\n"
            "\n"
            "@register_tool_with_metadata(\n"
            "    name='new_tool',\n"
            "    display_name='New Tool',\n"
            "    description='New plugin tool',\n"
            "    category=ToolCategory.DEVELOPMENT,\n"
            ")\n"
            "def demo_plugin_tools():\n"
            "    return DemoTool\n",
            encoding="utf-8",
        )
        stat_result = tools_path.stat()
        os.utime(tools_path, (stat_result.st_atime, stat_result.st_mtime + 1))

        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["demo_plugin"]
        assert "old_tool" not in _TOOL_REGISTRY
        assert "old_tool" not in TOOL_METADATA
        assert "new_tool" in _TOOL_REGISTRY
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_rejects_built_in_tool_name_collisions(tmp_path: Path) -> None:
    """Plugin tools must not shadow built-in tool registrations."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='calculator',\n"
        "    display_name='Calculator Override',\n"
        "    description='Should fail',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ConfigRuntimeValidationError, match="conflicts with built-in tool 'calculator'"):
        _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)


def test_load_plugins_rejects_plugin_tool_name_collisions(tmp_path: Path) -> None:
    """Active plugins must not register the same tool name."""
    first_root = tmp_path / "plugins" / "first"
    second_root = tmp_path / "plugins" / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "first_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (second_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "second_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    for root, display_name in ((first_root, "First Tool"), (second_root, "Second Tool")):
        (root / "tools.py").write_text(
            "from agno.tools import Toolkit\n"
            "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
            "\n"
            "class DemoTool(Toolkit):\n"
            "    def __init__(self) -> None:\n"
            "        super().__init__(name='demo', tools=[])\n"
            "\n"
            "@register_tool_with_metadata(\n"
            "    name='shared_tool',\n"
            f"    display_name='{display_name}',\n"
            "    description='Should conflict',\n"
            "    category=ToolCategory.DEVELOPMENT,\n"
            ")\n"
            "def demo_plugin_tools():\n"
            "    return DemoTool\n",
            encoding="utf-8",
        )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(
        ConfigRuntimeValidationError,
        match="Plugin tool 'shared_tool' conflicts between plugins 'first_plugin' and 'second_plugin'",
    ):
        _bind_runtime_paths(Config(plugins=["./plugins/first", "./plugins/second"]), config_path)


def test_load_plugins_rejects_duplicate_tool_names_within_one_plugin(tmp_path: Path) -> None:
    """One plugin module must not register the same tool name twice."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class FirstTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='first', tools=[])\n"
        "\n"
        "class SecondTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='second', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='dup_tool',\n"
        "    display_name='First Duplicate',\n"
        "    description='Should fail',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def first_plugin_tool():\n"
        "    return FirstTool\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='dup_tool',\n"
        "    display_name='Second Duplicate',\n"
        "    description='Should also fail',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def second_plugin_tool():\n"
        "    return SecondTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(
        ConfigRuntimeValidationError,
        match="Plugin tool 'dup_tool' is registered multiple times",
    ):
        _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)


def test_load_plugins_preserves_tools_when_manifest_name_changes(tmp_path: Path) -> None:
    """Changing only the manifest plugin name should force a logical module reload."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='renamed_tool',\n"
        "    display_name='Renamed Tool',\n"
        "    description='Should survive manifest rename',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["demo_plugin"]
        assert "renamed_tool" in _TOOL_REGISTRY

        manifest_path.write_text(
            json.dumps({"name": "renamed_plugin", "tools_module": "tools.py", "skills": []}),
            encoding="utf-8",
        )
        stat_result = manifest_path.stat()
        os.utime(manifest_path, (stat_result.st_atime, stat_result.st_mtime + 1))

        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["renamed_plugin"]
        assert "renamed_tool" in _TOOL_REGISTRY

        follow_up_config = Config.model_validate(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "test",
                        "tools": ["renamed_tool"],
                    },
                },
                "plugins": ["./plugins/demo"],
            },
        )
        bound = _bind_runtime_paths(follow_up_config, config_path)
        assert bound.get_agent("assistant").tool_names == ["renamed_tool"]
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_config_tolerates_missing_and_broken_plugins_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup config loads should skip broken optional plugins and keep valid tools."""
    good_root = tmp_path / "plugins" / "good"
    bad_root = tmp_path / "plugins" / "bad"
    good_root.mkdir(parents=True)
    bad_root.mkdir(parents=True)
    (good_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (bad_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (good_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='good_plugin_tool',\n"
        "    display_name='Good Plugin Tool',\n"
        "    description='Should not leak after failure',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )
    (bad_root / "tools.py").write_text("from definitely_missing_plugin_dependency import broken\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-5.4\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    tools:\n"
            "      - good_plugin_tool\n"
            "plugins:\n"
            "  - ./plugins/good\n"
            "  - ./plugins/missing\n"
            "  - ./plugins/bad\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    mock_logger = MagicMock()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
        assert config.get_agent("assistant").tool_names == ["good_plugin_tool"]
        mock_logger.warning.assert_any_call(
            "Plugin path does not exist, skipping",
            path=str((tmp_path / "plugins" / "missing").resolve()),
        )
        assert any(
            call.args == ("Failed to load plugin, skipping",)
            and call.kwargs["path"] == str(bad_root.resolve())
            and "definitely_missing_plugin_dependency" in call.kwargs["error"]
            for call in mock_logger.warning.call_args_list
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_skips_later_broken_plugin_and_keeps_earlier_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later plugin failure should not roll back already loaded valid plugins."""
    good_root = tmp_path / "plugins" / "good"
    bad_root = tmp_path / "plugins" / "bad"
    good_root.mkdir(parents=True)
    bad_root.mkdir(parents=True)
    (good_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (bad_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (good_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='good_plugin_tool',\n"
        "    display_name='Good Plugin Tool',\n"
        "    description='Should stay loaded after a later failure',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )
    (bad_root / "tools.py").write_text("from definitely_missing_plugin_dependency import broken\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    config = Config(plugins=["./plugins/good", "./plugins/bad"])

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    mock_logger = MagicMock()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        plugins = load_plugins(config, runtime_paths)
        assert [plugin.name for plugin in plugins] == ["good_plugin"]
        assert "good_plugin_tool" in _TOOL_REGISTRY
        assert "good_plugin_tool" in TOOL_METADATA
        assert any(
            call.args == ("Failed to load plugin, skipping",)
            and call.kwargs["path"] == str(bad_root.resolve())
            and "definitely_missing_plugin_dependency" in call.kwargs["error"]
            for call in mock_logger.warning.call_args_list
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


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
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        with pytest.raises(ValueError, match="Duplicate plugin manifest names configured"):
            _bind_runtime_paths(Config(plugins=["./plugins/first", "./plugins/second"]), config_path)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
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
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
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
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
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
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    config = Config(plugins=[{"path": "./plugins/same-file"}])

    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    try:
        plugins = load_plugins(config, runtime_paths)
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)

    assert len(plugins[0].discovered_hooks) == 1
    assert counter_path.read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_reload_plugins_invalidates_helper_modules_under_plugin_root(tmp_path: Path) -> None:
    """Reloads should evict helper modules, not just the top-level hooks file cache."""
    plugin_root = tmp_path / "plugins" / "helper-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "helper-reload", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helper.py").write_text("VALUE = 'before'\n", encoding="utf-8")
    (plugin_root / "hooks.py").write_text(
        "from .helper import VALUE\n"
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received', name='helper-value')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return VALUE\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/helper-reload"]), config_path)

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    try:
        initial = reload_plugins(config, runtime_paths_for(config))
        assert await initial.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)[0].callback(None) == "before"

        (plugin_root / "helper.py").write_text("VALUE = 'after'\n", encoding="utf-8")
        reloaded = reload_plugins(config, runtime_paths_for(config))

        assert await reloaded.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)[0].callback(None) == "after"
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_reload_plugins_cancels_module_global_tasks_once(tmp_path: Path) -> None:
    """Reloads should cancel deduped task globals across module-level holders."""
    plugin_root = tmp_path / "plugins" / "task-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "task-reload", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helper.py").write_text("MARKER = True\n", encoding="utf-8")
    (plugin_root / "hooks.py").write_text(
        "from . import helper\n"
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return helper.MARKER\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/task-reload"]), config_path)
    runtime_paths = runtime_paths_for(config)

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    shared_task = asyncio.create_task(asyncio.Event().wait())
    extra_task = asyncio.create_task(asyncio.Event().wait())
    try:
        reload_plugins(config, runtime_paths)
        hooks_path = (plugin_root / "hooks.py").resolve()
        hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path].module
        helper_module = sys.modules[
            f"{plugin_module._MODULE_IMPORT_CACHE[hooks_path].module_name.rsplit('.', 1)[0]}.helper"
        ]
        hooks_module._AUTO_POKE_TASK = shared_task
        hooks_module._snooze_tasks = {"shared": shared_task, "extra": extra_task}
        helper_module._AUTO_POKE_TASK = shared_task

        result = reload_plugins(config, runtime_paths)
        await asyncio.sleep(0)

        assert result.cancelled_task_count == 2
        assert shared_task.cancelled()
        assert extra_task.cancelled()
    finally:
        await asyncio.gather(shared_task, extra_task, return_exceptions=True)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


def test_reload_plugins_raises_when_configured_plugin_becomes_invalid(tmp_path: Path) -> None:
    """Explicit reloads should fail closed when a configured plugin breaks."""
    plugin_root = tmp_path / "plugins" / "broken-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken-reload", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/broken-reload"]), config_path)

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    try:
        initial = reload_plugins(config, runtime_paths_for(config))
        assert initial.active_plugin_names == ("broken-reload",)

        hooks_path.unlink()

        with pytest.raises(plugin_module.PluginValidationError, match="Plugin hooks module not found"):
            reload_plugins(config, runtime_paths_for(config))
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)
