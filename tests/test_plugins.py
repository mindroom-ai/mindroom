"""Tests for plugin loading and registration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import mindroom.tool_system.metadata as metadata_module
import mindroom.tool_system.plugin_imports as plugin_module
import mindroom.tool_system.plugins as plugins_module
import mindroom.tools  # noqa: F401
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.config.main import Config, ConfigRuntimeValidationError, RuntimeConfig, load_config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.hooks import EVENT_MESSAGE_RECEIVED, HookRegistry
from mindroom.mcp.registry import resolved_mcp_tool_state
from mindroom.oauth.registry import (
    clear_oauth_provider_cache,
    load_oauth_providers,
    load_oauth_providers_for_snapshot,
)
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, get_tool_by_name
from mindroom.tool_system.plugins import PluginReloadResult, get_configured_plugin_roots, load_plugins
from mindroom.tool_system.registry_state import capture_tool_registry_snapshot, restore_tool_registry_snapshot
from mindroom.tool_system.skills import _get_plugin_skill_roots, build_agent_skills, set_plugin_skill_roots
from tests.config_test_utils import runtime_config_from_data
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType


def _bind_runtime_paths(config: Config, config_path: Path) -> RuntimeConfig:
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    return bind_runtime_paths(config, runtime_paths)


def _reload_plugins(
    config: RuntimeConfig,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = False,
) -> PluginReloadResult:
    """Prepare and apply one plugin snapshot for loader-level tests."""
    prepared = plugins_module.prepare_plugin_reload(
        config,
        runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    return plugins_module.apply_prepared_plugin_reload(prepared, cancel_existing_tasks=True)


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


def _write_broken_tool_plugin(plugin_root: Path, tool_name: str = "broken_plugin_tool") -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={tool_name!r},\n"
        "    display_name='Broken Plugin Tool',\n"
        "    description='Tool declared by a plugin that fails after registration',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def broken_plugin_tools():\n"
        "    return BrokenTool\n"
        "\n"
        "raise ImportError('missing optional plugin dependency')\n",
        encoding="utf-8",
    )


def _write_pre_registration_broken_tool_plugin(plugin_root: Path, tool_name: str = "broken_plugin_tool") -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from definitely_missing_plugin_dependency import broken\n"
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={tool_name!r},\n"
        "    display_name='Broken Plugin Tool',\n"
        "    description='Tool declared by a plugin that fails before registration',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def broken_plugin_tools():\n"
        "    return BrokenTool\n",
        encoding="utf-8",
    )


def _write_mid_registration_broken_tool_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='registered_before_failure',\n"
        "    display_name='Registered Before Failure',\n"
        "    description='Tool declared before failure',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def registered_before_failure_tools():\n"
        "    return BrokenTool\n"
        "\n"
        "raise ImportError('missing optional plugin dependency')\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='declared_after_failure',\n"
        "    display_name='Declared After Failure',\n"
        "    description='Tool declared after failure',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def declared_after_failure_tools():\n"
        "    return BrokenTool\n",
        encoding="utf-8",
    )


def _write_working_tool_plugin(plugin_root: Path, *, plugin_name: str, tool_name: str) -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": plugin_name, "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class WorkingTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='working', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={tool_name!r},\n"
        "    display_name='Working Plugin Tool',\n"
        "    description='Tool declared by a working plugin',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def working_plugin_tools():\n"
        "    return WorkingTool\n",
        encoding="utf-8",
    )


def _write_oauth_reload_plugin(plugin_root: Path, display_name: str) -> None:
    """Write one OAuth plugin whose provider name comes from a relative helper."""
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "oauth-reload", "oauth_module": "oauth_provider.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helper.py").write_text(f"DISPLAY_NAME = {display_name!r}\n", encoding="utf-8")
    (plugin_root / "oauth_provider.py").write_text(
        "from .helper import DISPLAY_NAME\n"
        "from mindroom.oauth import OAuthProvider\n"
        "\n"
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    del settings, runtime_paths\n"
        "    return [OAuthProvider(\n"
        "        id='plugin_oauth_reload',\n"
        "        display_name=DISPLAY_NAME,\n"
        "        authorization_url='https://auth.example.test/authorize',\n"
        "        token_url='https://auth.example.test/token',\n"
        "        scopes=('plugin.read',),\n"
        "        credential_service='plugin_oauth_reload',\n"
        "        client_config_services=('plugin_oauth_reload_oauth_client',),\n"
        "    )]\n",
        encoding="utf-8",
    )


@contextmanager
def _preserved_plugin_loader_state(*, module_prefixes: tuple[str, ...] = ()) -> Iterator[None]:
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_active_runtime_state = plugins_module._ACTIVE_PLUGIN_RUNTIME_STATE
    original_modules = set(sys.modules)

    try:
        yield
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        plugins_module._ACTIVE_PLUGIN_RUNTIME_STATE = original_active_runtime_state
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_") or any(
                module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in module_prefixes
            ):
                sys.modules.pop(module_name, None)


def test_runtime_validation_does_not_mask_unexpected_tool_validation_type_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected internal type errors should not be rewritten into config validation failures."""
    runtime_paths = _minimal_runtime_paths(tmp_path)
    message = "unexpected backend type error"

    def _raise_type_error(_config: Config, _snapshot: object) -> None:
        raise TypeError(message)

    monkeypatch.setattr("mindroom.config.runtime._validate_authored_tool_entries", _raise_type_error)

    with pytest.raises(TypeError, match="unexpected backend type error"):
        runtime_config_from_data(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
            runtime_paths,
        )


def test_runtime_validation_does_not_mask_unexpected_tool_validation_value_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected internal value errors should escape instead of becoming 422-style config errors."""
    runtime_paths = _minimal_runtime_paths(tmp_path)
    message = "unexpected backend value error"

    def _raise_value_error(_config: Config, _snapshot: object) -> None:
        raise ValueError(message)

    monkeypatch.setattr("mindroom.config.runtime._validate_authored_tool_entries", _raise_value_error)

    with pytest.raises(ValueError, match="unexpected backend value error"):
        runtime_config_from_data(
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        assert [plugin.name for plugin in plugins] == ["demo-plugin"]
        assert "demo_plugin" in TOOL_REGISTRY
        tool = get_tool_by_name("demo_plugin", runtime_paths_for(config), worker_target=None)
        assert tool.name == "demo"
        assert (plugin_root / "skills").resolve() in _get_plugin_skill_roots()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
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
        assert original_registry == TOOL_REGISTRY
        assert original_metadata == TOOL_METADATA
        assert original_module_cache == plugin_module._MODULE_IMPORT_CACHE
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        assert [plugin.name for plugin in plugins] == ["demo-pkg"]
        assert plugins[0].root == plugin_root.resolve()
        assert "demo_pkg_tool" in TOOL_REGISTRY
        tool = get_tool_by_name("demo_pkg_tool", runtime_paths_for(config), worker_target=None)
        assert tool.name == "demo_pkg"
        assert (plugin_root / "skills").resolve() in _get_plugin_skill_roots()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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


def test_load_plugins_skips_system_exit_while_resolving_explicit_python_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken package import side effects should not terminate tolerant plugin loading."""
    site_packages = tmp_path / "site-packages"
    package_root = site_packages / "bad_pkg"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("raise SystemExit('package exit')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = Config(plugins=["python:bad_pkg.sub"])

    with _preserved_plugin_loader_state(module_prefixes=("bad_pkg",)):
        assert load_plugins(config, runtime_paths) == []


def test_load_plugins_propagates_keyboard_interrupt_while_resolving_explicit_python_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator interrupts during plugin spec resolution should still terminate startup."""
    site_packages = tmp_path / "site-packages"
    package_root = site_packages / "bad_pkg"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("raise KeyboardInterrupt('stop')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = Config(plugins=["python:bad_pkg.sub"])

    with _preserved_plugin_loader_state(module_prefixes=("bad_pkg",)), pytest.raises(KeyboardInterrupt):
        load_plugins(config, runtime_paths)


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


def test_get_configured_plugin_roots_memoizes_resolved_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Repeated configured-root reads should not re-resolve paths for the same config snapshot."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    runtime_paths = runtime_paths_for(config)
    resolve_calls: list[str] = []
    original_resolve_plugin_root = plugin_module._resolve_plugin_root

    def counted_resolve_plugin_root(plugin_path: str, runtime_paths: RuntimePaths) -> Path:
        resolve_calls.append(plugin_path)
        return original_resolve_plugin_root(plugin_path, runtime_paths)

    plugins_module._clear_configured_plugin_roots_cache()
    monkeypatch.setattr(plugin_module, "_resolve_plugin_root", counted_resolve_plugin_root)
    try:
        first_roots = get_configured_plugin_roots(config, runtime_paths)
        second_roots = get_configured_plugin_roots(config, runtime_paths)
    finally:
        plugins_module._clear_configured_plugin_roots_cache()

    assert first_roots == (plugin_root.resolve(),)
    assert second_roots == first_roots
    assert resolve_calls == ["./plugins/demo"]


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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        assert load_plugins(Config(plugins=["./plugins/missing"]), runtime_paths) == []
        mock_logger.warning.assert_any_call("Plugin path does not exist, skipping", path=str(missing_root))
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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


def test_runtime_validation_does_not_mutate_plugin_skill_roots(tmp_path: Path) -> None:
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
    original_registry = TOOL_REGISTRY.copy()
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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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


def test_runtime_validation_does_not_leak_plugin_tools_after_failure(tmp_path: Path) -> None:
    """Runtime validation should roll back plugin tool registration when validation fails later."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
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

        assert "leaked_plugin_tool" not in TOOL_REGISTRY
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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)


def test_runtime_validation_cleans_healthy_package_when_later_plugin_fails(tmp_path: Path) -> None:
    """A rejected multi-plugin snapshot must remove packages imported before the failure."""
    good_root = tmp_path / "plugins" / "good"
    bad_root = tmp_path / "plugins" / "bad"
    good_root.mkdir(parents=True)
    bad_root.mkdir(parents=True)
    (good_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (bad_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (good_root / "hooks.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    (bad_root / "hooks.py").write_text("raise RuntimeError('rejected candidate')\n", encoding="utf-8")
    runtime_paths = _minimal_runtime_paths(tmp_path)
    existing_modules = set(sys.modules)

    with pytest.raises(ConfigRuntimeValidationError, match="rejected candidate"):
        RuntimeConfig.from_authored(
            Config(plugins=["./plugins/good", "./plugins/bad"]),
            runtime_paths,
        )

    assert not any("__validation__" in module_name for module_name in set(sys.modules) - existing_modules)


def test_runtime_validation_does_not_mutate_live_tool_registry_on_success(tmp_path: Path) -> None:
    """Successful runtime validation should not publish plugin tools into the live registry."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        validated = runtime_config_from_data(authored_config, runtime_paths)
        assert validated.get_agent("assistant").tool_names == ["validated_plugin_tool"]
        assert "validated_plugin_tool" not in TOOL_REGISTRY
        assert "validated_plugin_tool" not in TOOL_METADATA
        assert original_module_cache == plugin_module._MODULE_IMPORT_CACHE
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)


def test_runtime_validation_rejects_invalid_dedicated_hooks_module(tmp_path: Path) -> None:
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
        runtime_config_from_data(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/broken-hooks"],
            },
            runtime_paths,
        )


def test_runtime_validation_does_not_mutate_live_registry_for_package_helper_imports(
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        validated = runtime_config_from_data(
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
        assert "helper_tool" not in TOOL_REGISTRY
        assert "helper_tool" not in TOOL_METADATA
        assert original_registry == TOOL_REGISTRY
        assert original_metadata == TOOL_METADATA
        assert original_module_cache == plugin_module._MODULE_IMPORT_CACHE
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
            "demo_plugin",
        ]
        assert "removed_plugin_tool" in TOOL_REGISTRY
        assert "removed_plugin_tool" in TOOL_METADATA

        assert load_plugins(config_without_plugin, runtime_paths_for(config_without_plugin)) == []
        assert "removed_plugin_tool" not in TOOL_REGISTRY
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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
            "demo_plugin",
        ]
        assert "toggled_plugin_tool" in TOOL_REGISTRY

        assert load_plugins(config_without_plugin, runtime_paths_for(config_without_plugin)) == []
        assert "toggled_plugin_tool" not in TOOL_REGISTRY

        assert [plugin.name for plugin in load_plugins(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
            "demo_plugin",
        ]
        assert "toggled_plugin_tool" in TOOL_REGISTRY

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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert load_plugins(config_without_plugins, runtime_paths_for(config_without_plugins)) == []
        for tool_name in ("memory", "delegate", "self_config", "compact_context"):
            assert tool_name in TOOL_METADATA
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_preserves_non_plugin_dynamic_tools(tmp_path: Path) -> None:
    """Syncing the plugin overlay must not claim another dynamic registry namespace."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_without_plugins = _bind_runtime_paths(Config(plugins=[]), config_path)
    dynamic_factory = MagicMock()
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        TOOL_REGISTRY["mcp_demo"] = dynamic_factory
        dynamic_metadata = replace(TOOL_METADATA["shell"], name="mcp_demo", factory=dynamic_factory)
        TOOL_METADATA["mcp_demo"] = dynamic_metadata

        assert load_plugins(config_without_plugins, runtime_paths_for(config_without_plugins)) == []
        assert TOOL_REGISTRY["mcp_demo"] is dynamic_factory
        assert TOOL_METADATA["mcp_demo"] is dynamic_metadata
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        set_plugin_skill_roots(original_plugin_roots)


def test_prepare_plugin_reload_replaces_live_mcp_entries_in_staged_registry(tmp_path: Path) -> None:
    """A staged registry must contain only the target config's MCP entries."""
    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = runtime_config_from_data(
        {
            "mcp_servers": {
                "beta": {
                    "transport": "stdio",
                    "command": "beta-command",
                },
            },
        },
        runtime_paths,
    )
    live_registry, live_metadata = resolved_mcp_tool_state(
        Config(
            mcp_servers={
                "alpha": {
                    "transport": "stdio",
                    "command": "alpha-command",
                },
            },
        ),
    )
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    try:
        TOOL_REGISTRY.update(live_registry)
        TOOL_METADATA.update(live_metadata)

        prepared = plugins_module.prepare_plugin_reload(config, runtime_paths)

        assert "mcp_alpha" not in prepared.tool_registry_snapshot.registry
        assert "mcp_alpha" not in prepared.tool_registry_snapshot.metadata
        assert "mcp_beta" in prepared.tool_registry_snapshot.registry
        assert "mcp_beta" in prepared.tool_registry_snapshot.metadata
        assert "mcp_beta" in prepared.resolved_tool_state.validation_snapshot
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["demo_plugin"]
        assert "old_tool" in TOOL_REGISTRY

        tools_path.write_text(
            "from agno.tools import Toolkit\n"
            "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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
        assert "old_tool" not in TOOL_REGISTRY
        assert "old_tool" not in TOOL_METADATA
        assert "new_tool" in TOOL_REGISTRY
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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
            "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()

    try:
        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["demo_plugin"]
        assert "renamed_tool" in TOOL_REGISTRY

        manifest_path.write_text(
            json.dumps({"name": "renamed_plugin", "tools_module": "tools.py", "skills": []}),
            encoding="utf-8",
        )
        stat_result = manifest_path.stat()
        os.utime(manifest_path, (stat_result.st_atime, stat_result.st_mtime + 1))

        assert [plugin.name for plugin in load_plugins(config, runtime_paths_for(config))] == ["renamed_plugin"]
        assert "renamed_tool" in TOOL_REGISTRY

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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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
    original_registry = TOOL_REGISTRY.copy()
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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_config_tolerates_agent_reference_to_tool_declared_by_broken_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup config loads should allow unavailable tools only from skipped broken plugins."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_broken_tool_plugin(plugin_root)

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
            "      - shell\n"
            "      - broken_plugin_tool\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
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
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.runtime.logger", mock_logger)

    with _preserved_plugin_loader_state():
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)

        assert "broken_plugin_tool" not in config.resolve_entity("assistant").available_tools
        assert config.resolve_entity("assistant").available_tools == ["shell", "scheduler"]
        assert any(
            call.args == ("Plugin tool unavailable because plugin failed to load",)
            and call.kwargs["tool_name"] == "broken_plugin_tool"
            and call.kwargs["config_path"] == "agents.assistant.tools[1]"
            for call in mock_logger.warning.call_args_list
        )


def test_load_config_tolerates_unavailable_ast_plugin_tool_with_authored_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overrides for filtered unavailable plugin tools should not block tolerant startup."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_pre_registration_broken_tool_plugin(plugin_root)

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
            "      - broken_plugin_tool:\n"
            "          unreachable_option: ignored\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
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
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.runtime.logger", mock_logger)

    with _preserved_plugin_loader_state():
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)

        assert config.resolve_entity("assistant").available_tools == ["scheduler"]
        assert any(
            call.args == ("Plugin tool unavailable because plugin failed to load",)
            and call.kwargs["tool_name"] == "broken_plugin_tool"
            and call.kwargs["config_path"] == "agents.assistant.tools[0]"
            for call in mock_logger.warning.call_args_list
        )


def test_unavailable_plugin_tool_is_validation_only_not_runtime_metadata(tmp_path: Path) -> None:
    """Skipped plugin tools should not appear in public runtime tool metadata."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_pre_registration_broken_tool_plugin(plugin_root)

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
            "      - broken_plugin_tool\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
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
    config = Config(plugins=["./plugins/broken"])

    with _preserved_plugin_loader_state():
        runtime_metadata = metadata_module.resolved_tool_metadata_for_runtime(
            runtime_paths,
            config,
            tolerate_plugin_load_errors=True,
        )
        validation_snapshot = metadata_module.resolved_tool_validation_snapshot_for_runtime(
            runtime_paths,
            config,
            tolerate_plugin_load_errors=True,
        )

        assert "broken_plugin_tool" not in runtime_metadata
        assert validation_snapshot["broken_plugin_tool"].runtime_loadable is False
        assert validation_snapshot["broken_plugin_tool"].unavailable_due_to_plugin_load_error is True


def test_load_config_tolerates_tool_declared_after_broken_plugin_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial plugin registration should not hide later literal tool declarations."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_mid_registration_broken_tool_plugin(plugin_root)

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
            "      - declared_after_failure\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
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
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.runtime.logger", mock_logger)

    with _preserved_plugin_loader_state():
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)

        assert config.resolve_entity("assistant").available_tools == ["scheduler"]
        assert any(
            call.args == ("Plugin tool unavailable because plugin failed to load",)
            and call.kwargs["tool_name"] == "declared_after_failure"
            and call.kwargs["config_path"] == "agents.assistant.tools[0]"
            for call in mock_logger.warning.call_args_list
        )


def test_load_config_tolerates_deferred_reference_to_tool_declared_by_broken_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tolerant startup should drop unavailable skipped-plugin deferred tools."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_broken_tool_plugin(plugin_root)

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
            "      - name: broken_plugin_tool\n"
            "        defer: true\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
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
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.runtime.logger", mock_logger)

    with _preserved_plugin_loader_state():
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)

        assert config.resolve_entity("assistant").authored_deferred_tool_configs == []
        assert config.resolve_entity("assistant").available_tools == ["scheduler"]
        assert any(
            call.args == ("Plugin tool unavailable because plugin failed to load",)
            and call.kwargs["tool_name"] == "broken_plugin_tool"
            and call.kwargs["config_path"] == "agents.assistant.tools[0]"
            for call in mock_logger.warning.call_args_list
        )


def test_broken_plugin_unavailable_tool_does_not_shadow_builtin_tool(tmp_path: Path) -> None:
    """A skipped plugin declaration must not make a built-in tool unavailable."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_broken_tool_plugin(plugin_root, tool_name="shell")

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
            "      - shell\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
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

    with _preserved_plugin_loader_state():
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)

        assert config.resolve_entity("assistant").available_tools == ["shell", "scheduler"]


def test_broken_plugin_unavailable_tool_does_not_shadow_healthy_plugin_tool(tmp_path: Path) -> None:
    """A later skipped plugin declaration must not make a healthy plugin tool unavailable."""
    good_root = tmp_path / "plugins" / "good"
    broken_root = tmp_path / "plugins" / "broken"
    _write_working_tool_plugin(good_root, plugin_name="good_plugin", tool_name="healthy_plugin_tool")
    _write_broken_tool_plugin(broken_root, tool_name="healthy_plugin_tool")

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
            "      - healthy_plugin_tool\n"
            "plugins:\n"
            "  - ./plugins/good\n"
            "  - ./plugins/broken\n"
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

    with _preserved_plugin_loader_state():
        config = load_config(runtime_paths, tolerate_plugin_load_errors=True)

        assert config.resolve_entity("assistant").available_tools == ["healthy_plugin_tool", "scheduler"]


def test_load_config_still_rejects_unknown_tool_without_broken_plugin_explanation(tmp_path: Path) -> None:
    """Tolerant startup should not convert ordinary unknown tools into warnings."""
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
            "      - typo_plugin_tool\n"
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

    with pytest.raises(ConfigRuntimeValidationError, match="Unknown tool 'typo_plugin_tool'"):
        load_config(runtime_paths, tolerate_plugin_load_errors=True)


def test_load_config_still_rejects_unknown_deferred_tool_without_broken_plugin_explanation(tmp_path: Path) -> None:
    """Tolerant startup should not allow ordinary unknown deferred tools."""
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
            "      - name: typo_plugin_tool\n"
            "        defer: true\n"
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

    with pytest.raises(ConfigRuntimeValidationError, match="Unknown tool 'typo_plugin_tool'"):
        load_config(runtime_paths, tolerate_plugin_load_errors=True)


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
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    mock_logger = MagicMock()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        plugins = load_plugins(config, runtime_paths)
        assert [plugin.name for plugin in plugins] == ["good_plugin"]
        assert "good_plugin_tool" in TOOL_REGISTRY
        assert "good_plugin_tool" in TOOL_METADATA
        assert any(
            call.args == ("Failed to load plugin, skipping",)
            and call.kwargs["path"] == str(bad_root.resolve())
            and "definitely_missing_plugin_dependency" in call.kwargs["error"]
            for call in mock_logger.warning.call_args_list
        )
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_load_plugins_skips_system_exit_during_plugin_module_execution(tmp_path: Path) -> None:
    """A plugin module raising SystemExit at import time should be treated as a broken plugin."""
    good_root = tmp_path / "plugins" / "good"
    bad_root = tmp_path / "plugins" / "bad"
    good_root.mkdir(parents=True)
    bad_root.mkdir(parents=True)
    (good_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good_plugin", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (bad_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad_plugin", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (good_root / "hooks.py").write_text(
        "from mindroom.hooks import hook\n\n@hook('message:received')\nasync def audit(ctx):\n    del ctx\n",
        encoding="utf-8",
    )
    (bad_root / "hooks.py").write_text("raise SystemExit('plugin exit')\n", encoding="utf-8")

    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = Config(plugins=["./plugins/good", "./plugins/bad"])

    with _preserved_plugin_loader_state():
        plugins = load_plugins(config, runtime_paths)

        assert [plugin.name for plugin in plugins] == ["good_plugin"]
        registry = HookRegistry.from_plugins(plugins)
        assert [hook.plugin_name for hook in registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["good_plugin"]


def test_load_plugins_propagates_keyboard_interrupt_during_plugin_module_execution(tmp_path: Path) -> None:
    """Operator interrupts during plugin module execution should still terminate startup."""
    plugin_root = tmp_path / "plugins" / "bad"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad_plugin", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text("raise KeyboardInterrupt('stop')\n", encoding="utf-8")

    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = Config(plugins=["./plugins/bad"])

    with _preserved_plugin_loader_state(), pytest.raises(KeyboardInterrupt):
        load_plugins(config, runtime_paths)


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
        initial = _reload_plugins(config, runtime_paths_for(config))
        assert await initial.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)[0].callback(None) == "before"

        (plugin_root / "helper.py").write_text("VALUE = 'after'\n", encoding="utf-8")
        reloaded = _reload_plugins(config, runtime_paths_for(config))

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


def test_prepare_plugin_reload_keeps_live_import_state_until_apply(tmp_path: Path) -> None:
    """Preparing a plugin reload should not replace live modules or caches."""
    plugin_root = tmp_path / "plugins" / "staged-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "staged-reload", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = (plugin_root / "hooks.py").resolve()
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/staged-reload"]), config_path)
    runtime_paths = runtime_paths_for(config)

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    try:
        _reload_plugins(config, runtime_paths)
        live_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path].module
        assert live_module.VALUE == 1

        hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
        prepared = plugins_module.prepare_plugin_reload(config, runtime_paths)

        assert plugin_module._MODULE_IMPORT_CACHE[hooks_path].module is live_module
        assert sys.modules[live_module.__name__] is live_module
        plugins_module.apply_prepared_plugin_reload(prepared, cancel_existing_tasks=True)
        assert plugin_module._MODULE_IMPORT_CACHE[hooks_path].module.VALUE == 2
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


def test_runtime_config_keeps_its_plugin_skill_generation(tmp_path: Path) -> None:
    """A runtime config must load skills from its immutable plugin generation."""
    plugin_root = tmp_path / "plugins" / "skill-generation"
    skill_root = plugin_root / "skills"
    skill_dir = skill_root / "demo-skill"
    skill_dir.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "skill-generation", "skills": ["skills"]}),
        encoding="utf-8",
    )
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\nname: demo-skill\ndescription: generation one\n---\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    authored = Config(
        agents={"general": {"display_name": "General", "skills": ["demo-skill"]}},
        plugins=["./plugins/skill-generation"],
    )
    base_config = _bind_runtime_paths(authored, config_path)
    runtime_paths = runtime_paths_for(base_config)

    def bind_prepared_state(prepared: plugins_module._PreparedPluginReload) -> RuntimeConfig:
        runtime_config = RuntimeConfig.from_authored(
            authored,
            runtime_paths,
            tool_validation_snapshot=prepared.resolved_tool_state.validation_snapshot,
            plugin_oauth_providers=prepared.resolved_tool_state.plugin_oauth_providers,
            plugin_skill_roots=prepared.resolved_tool_state.plugin_skill_roots,
        )
        metadata_module.bind_resolved_tool_state_cache(prepared.resolved_tool_state, runtime_config)
        return runtime_config

    with _preserved_plugin_loader_state():
        prepared_v1 = plugins_module.prepare_plugin_reload(base_config, runtime_paths)
        config_v1 = bind_prepared_state(prepared_v1)
        plugins_module.apply_prepared_plugin_reload(prepared_v1)

        skill_path.write_text(
            "---\nname: demo-skill\ndescription: generation two\n---\n",
            encoding="utf-8",
        )
        prepared_v2 = plugins_module.prepare_plugin_reload(base_config, runtime_paths)
        plugins_module.apply_prepared_plugin_reload(prepared_v2)

        skills = build_agent_skills(
            "general",
            config_v1,
            runtime_paths,
            env_vars={},
            credential_keys=set(),
        )

    assert skills is not None
    assert skills.get_skill("demo-skill").description == "generation one"


@pytest.mark.asyncio
async def test_plugin_reload_keeps_each_runtime_package_generation_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Staging and committing V2 must not replace lazy imports pinned by config V1."""
    plugin_root = tmp_path / "plugins" / "generation-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "generation-reload", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    helper_path = plugin_root / "helper.py"
    helper_path.write_text("TOOLKIT_NAME = 'generation_v1'\n", encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class GenerationToolkit(Toolkit):\n"
        "    def __init__(self):\n"
        "        from .helper import TOOLKIT_NAME\n"
        "        super().__init__(name=TOOLKIT_NAME, tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='generation_tool',\n"
        "    display_name='Generation Tool',\n"
        "    description='Test isolated runtime plugin generations.',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def generation_tool():\n"
        "    return GenerationToolkit\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    authored_config = Config(plugins=["./plugins/generation-reload"])
    base_config = _bind_runtime_paths(authored_config, config_path)
    runtime_paths = runtime_paths_for(base_config)

    def bind_prepared_state(prepared: plugins_module._PreparedPluginReload) -> RuntimeConfig:
        runtime_config = RuntimeConfig.from_authored(
            authored_config,
            runtime_paths,
            tool_validation_snapshot=prepared.resolved_tool_state.validation_snapshot,
            plugin_oauth_providers=prepared.resolved_tool_state.plugin_oauth_providers,
        )
        metadata_module.bind_resolved_tool_state_cache(prepared.resolved_tool_state, runtime_config)
        return runtime_config

    def toolkit_name(runtime_config: RuntimeConfig) -> str:
        return get_tool_by_name(
            "generation_tool",
            runtime_paths,
            runtime_config=runtime_config,
            disable_sandbox_proxy=True,
            worker_target=None,
        ).name

    with _preserved_plugin_loader_state():
        prepared_v1 = plugins_module.prepare_plugin_reload(base_config, runtime_paths)
        config_v1 = bind_prepared_state(prepared_v1)
        plugins_module.apply_prepared_plugin_reload(prepared_v1)

        helper_path.write_text("TOOLKIT_NAME = 'generation_version_2'\n", encoding="utf-8")
        staging_started = threading.Event()
        continue_staging = threading.Event()
        original_exec_plugin_source = plugins_module._exec_plugin_source

        def pause_candidate_import(module_path: Path, module: ModuleType) -> None:
            if module_path.name == tools_path.name:
                staging_started.set()
                if not continue_staging.wait(timeout=5):
                    msg = "timed out waiting to resume candidate plugin import"
                    raise RuntimeError(msg)
            original_exec_plugin_source(module_path, module)

        monkeypatch.setattr(plugins_module, "_exec_plugin_source", pause_candidate_import)
        prepare_task = asyncio.create_task(
            asyncio.to_thread(plugins_module.prepare_plugin_reload, base_config, runtime_paths),
        )
        try:
            assert await asyncio.to_thread(staging_started.wait, 5)
            assert toolkit_name(config_v1) == "generation_v1"
        finally:
            continue_staging.set()
        prepared_v2 = await prepare_task
        config_v2 = bind_prepared_state(prepared_v2)
        plugins_module.apply_prepared_plugin_reload(prepared_v2)

        assert toolkit_name(config_v1) == "generation_v1"
        assert toolkit_name(config_v2) == "generation_version_2"


def test_plugin_staging_serializes_runtime_validation_packages(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Staged snapshot restoration must not evict concurrent validation packages."""
    plugin_root = tmp_path / "plugins" / "validation-race"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "validation-race", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helper.py").write_text("TOOLKIT_NAME = 'validation_race'\n", encoding="utf-8")
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "class ValidationRaceToolkit(Toolkit):\n"
        "    def __init__(self):\n"
        "        from .helper import TOOLKIT_NAME\n"
        "        super().__init__(name=TOOLKIT_NAME, tools=[])\n"
        "@register_tool_with_metadata(\n"
        "    name='validation_race_tool',\n"
        "    display_name='Validation Race Tool',\n"
        "    description='Test validation package ownership.',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def validation_race_tool():\n"
        "    return ValidationRaceToolkit\n",
        encoding="utf-8",
    )
    runtime_paths = _minimal_runtime_paths(tmp_path)
    empty_config = RuntimeConfig.from_authored(Config(defaults={"tools": []}), runtime_paths)
    authored_plugin_config = Config(defaults={"tools": []}, plugins=["./plugins/validation-race"])
    staging_started = threading.Event()
    continue_staging = threading.Event()
    validation_started = threading.Event()
    validation_finished = threading.Event()
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    original_load_plugins = plugins_module.load_plugins

    def pause_staging(*args: object, **kwargs: object) -> list[plugin_module._Plugin]:
        staging_started.set()
        if not continue_staging.wait(timeout=5):
            msg = "timed out waiting to resume plugin staging"
            raise RuntimeError(msg)
        return original_load_plugins(*args, **kwargs)

    def stage_plugins() -> None:
        try:
            results["prepared"] = plugins_module.prepare_plugin_reload(empty_config, runtime_paths)
        except BaseException as exc:
            errors.append(exc)

    def validate_config() -> None:
        validation_started.set()
        try:
            results["config"] = RuntimeConfig.from_authored(authored_plugin_config, runtime_paths)
        except BaseException as exc:
            errors.append(exc)
        finally:
            validation_finished.set()

    monkeypatch.setattr(plugins_module, "load_plugins", pause_staging)
    with _preserved_plugin_loader_state():
        staging_thread = threading.Thread(target=stage_plugins)
        validation_thread = threading.Thread(target=validate_config)
        staging_thread.start()
        try:
            assert staging_started.wait(timeout=5)
            validation_thread.start()
            assert validation_started.wait(timeout=5)
            assert not validation_finished.wait(timeout=0.2)
        finally:
            continue_staging.set()
            staging_thread.join(timeout=5)
            validation_thread.join(timeout=5)

        assert not staging_thread.is_alive()
        assert not validation_thread.is_alive()
        assert not errors
        runtime_config = results["config"]
        assert isinstance(runtime_config, RuntimeConfig)
        toolkit = get_tool_by_name(
            "validation_race_tool",
            runtime_paths,
            runtime_config=runtime_config,
            disable_sandbox_proxy=True,
            worker_target=None,
        )
        assert toolkit.name == "validation_race"


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_after_schedule", [False, True])
async def test_plugin_import_rejects_background_tasks(
    tmp_path: Path,
    *,
    raise_after_schedule: bool,
) -> None:
    """Plugin imports cannot start tasks before their runtime snapshot is committed."""
    plugin_root = tmp_path / "plugins" / "task-import"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "task-import", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    marker_path = tmp_path / "task-ran"
    failure = "raise RuntimeError('scheduled failure')\n" if raise_after_schedule else ""
    (plugin_root / "hooks.py").write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "async def background():\n"
        f"    Path({str(marker_path)!r}).write_text('ran', encoding='utf-8')\n"
        "TASK = asyncio.create_task(background())\n"
        f"{failure}",
        encoding="utf-8",
    )
    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = RuntimeConfig.from_authored(
        Config(defaults={"tools": []}, plugins=["./plugins/task-import"]),
        runtime_paths,
        tool_validation_snapshot={},
    )

    with _preserved_plugin_loader_state():
        with pytest.raises(plugin_module.PluginValidationError, match="Plugin hooks module execution failed"):
            plugins_module.prepare_plugin_reload(config, runtime_paths)
        await asyncio.sleep(0)

    assert not marker_path.exists()


@pytest.mark.asyncio
async def test_runtime_validation_rejects_plugin_import_background_tasks(tmp_path: Path) -> None:
    """Runtime validation cancels tasks created in its temporary plugin import."""
    plugin_root = tmp_path / "plugins" / "validation-task"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "validation-task", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    marker_path = tmp_path / "validation-task-ran"
    (plugin_root / "hooks.py").write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "async def background():\n"
        f"    Path({str(marker_path)!r}).write_text('ran', encoding='utf-8')\n"
        "TASK = asyncio.create_task(background())\n",
        encoding="utf-8",
    )
    runtime_paths = _minimal_runtime_paths(tmp_path)

    with _preserved_plugin_loader_state():
        with pytest.raises(ConfigRuntimeValidationError, match="created async tasks during import"):
            RuntimeConfig.from_authored(Config(plugins=["./plugins/validation-task"]), runtime_paths)
        await asyncio.sleep(0)

    assert not marker_path.exists()


def test_pinned_api_snapshot_keeps_oauth_plugin_generation(tmp_path: Path) -> None:
    """A request pinned before reload must retain its staged OAuth provider code."""
    plugin_root = tmp_path / "plugins" / "oauth-reload"
    _write_oauth_reload_plugin(plugin_root, "before")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    authored_config = Config(plugins=["./plugins/oauth-reload"])
    base_config = _bind_runtime_paths(authored_config, config_path)
    runtime_paths = runtime_paths_for(base_config)

    def bind_prepared_state(prepared: plugins_module._PreparedPluginReload) -> RuntimeConfig:
        runtime_config = RuntimeConfig.from_authored(
            authored_config,
            runtime_paths,
            tool_validation_snapshot=prepared.resolved_tool_state.validation_snapshot,
            plugin_oauth_providers=prepared.resolved_tool_state.plugin_oauth_providers,
        )
        metadata_module.bind_resolved_tool_state_cache(prepared.resolved_tool_state, runtime_config)
        return runtime_config

    clear_oauth_provider_cache()
    try:
        with _preserved_plugin_loader_state():
            prepared_v1 = plugins_module.prepare_plugin_reload(base_config, runtime_paths)
            config_v1 = bind_prepared_state(prepared_v1)
            plugins_module.apply_prepared_plugin_reload(prepared_v1)
            snapshot_v1 = ApiSnapshot(
                generation=1,
                runtime_paths=runtime_paths,
                config_data=config_v1.authored_model_dump(),
                runtime_config=config_v1,
            )

            _write_oauth_reload_plugin(plugin_root, "after")
            prepared_v2 = plugins_module.prepare_plugin_reload(base_config, runtime_paths)
            config_v2 = bind_prepared_state(prepared_v2)
            plugins_module.apply_prepared_plugin_reload(prepared_v2)
            snapshot_v2 = ApiSnapshot(
                generation=2,
                runtime_paths=runtime_paths,
                config_data=config_v2.authored_model_dump(),
                runtime_config=config_v2,
            )

            providers_v1 = load_oauth_providers_for_snapshot(snapshot_v1)
            providers_v2 = load_oauth_providers_for_snapshot(snapshot_v2)

            assert providers_v1["plugin_oauth_reload"].display_name == "before"
            assert providers_v2["plugin_oauth_reload"].display_name == "after"
    finally:
        clear_oauth_provider_cache()


def test_plugin_reload_snapshot_keeps_only_active_generation_registrations(tmp_path: Path) -> None:
    """Repeated reloads must not retain registration maps from obsolete generations."""
    plugin_root = tmp_path / "plugins" / "registration-reload"
    _write_working_tool_plugin(
        plugin_root,
        plugin_name="registration-reload",
        tool_name="registration_reload_tool",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/registration-reload"]), config_path)
    runtime_paths = runtime_paths_for(config)
    original_snapshot = capture_tool_registry_snapshot()

    try:
        with _preserved_plugin_loader_state():
            generation_counts: list[int] = []
            for _ in range(3):
                _reload_plugins(config, runtime_paths)
                snapshot = capture_tool_registry_snapshot()
                generation_counts.append(
                    sum(
                        "registration_reload__runtime__" in module_name
                        for module_name in snapshot.plugin_tool_metadata_by_module
                    ),
                )

            assert generation_counts == [1, 1, 1]
    finally:
        restore_tool_registry_snapshot(original_snapshot)


def test_load_oauth_providers_isolates_system_exit_from_plugin_callback(tmp_path: Path) -> None:
    """OAuth plugin callbacks should not be able to terminate provider loading."""
    plugin_root = tmp_path / "plugins" / "bad-oauth"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad-oauth", "oauth_module": "oauth_provider.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "oauth_provider.py").write_text(
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    del settings, runtime_paths\n"
        "    raise SystemExit('oauth exit')\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    baseline_config = _bind_runtime_paths(Config(plugins=[]), config_path)
    runtime_paths = runtime_paths_for(baseline_config)

    with _preserved_plugin_loader_state():
        try:
            clear_oauth_provider_cache()
            baseline_providers = load_oauth_providers(baseline_config, runtime_paths)
            config = RuntimeConfig.from_authored(
                Config(plugins=["./plugins/bad-oauth"]),
                runtime_paths,
                tolerate_plugin_load_errors=True,
            )
            clear_oauth_provider_cache()
            providers = load_oauth_providers(config, runtime_paths)
            assert providers == baseline_providers

            clear_oauth_provider_cache()
            with pytest.raises(ConfigRuntimeValidationError, match="Plugin OAuth provider registration failed"):
                RuntimeConfig.from_authored(Config(plugins=["./plugins/bad-oauth"]), runtime_paths)
        finally:
            clear_oauth_provider_cache()


def test_load_oauth_providers_propagates_keyboard_interrupt_from_plugin_callback(tmp_path: Path) -> None:
    """Operator interrupts during plugin OAuth callbacks should still terminate startup."""
    plugin_root = tmp_path / "plugins" / "bad-oauth"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad-oauth", "oauth_module": "oauth_provider.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "oauth_provider.py").write_text(
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    del settings, runtime_paths\n"
        "    raise KeyboardInterrupt('stop')\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = runtime_paths_for(_bind_runtime_paths(Config(), config_path))

    with _preserved_plugin_loader_state():
        try:
            clear_oauth_provider_cache()
            with pytest.raises(KeyboardInterrupt):
                RuntimeConfig.from_authored(Config(plugins=["./plugins/bad-oauth"]), runtime_paths)
        finally:
            clear_oauth_provider_cache()


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
    shared_task = asyncio.create_task(asyncio.Event().wait())
    extra_task = asyncio.create_task(asyncio.Event().wait())
    try:
        _reload_plugins(config, runtime_paths)
        hooks_path = (plugin_root / "hooks.py").resolve()
        hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path].module
        helper_module = sys.modules[
            f"{plugin_module._MODULE_IMPORT_CACHE[hooks_path].module_name.rsplit('.', 1)[0]}.helper"
        ]
        hooks_module._AUTO_POKE_TASK = shared_task
        hooks_module._snooze_tasks = {"shared": shared_task, "extra": extra_task}
        helper_module._AUTO_POKE_TASK = shared_task

        result = _reload_plugins(config, runtime_paths)
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


@pytest.mark.asyncio
async def test_reload_plugins_cancels_tasks_for_removed_plugins(tmp_path: Path) -> None:
    """Removing a loaded plugin should still cancel its module-global tasks."""
    plugin_root = tmp_path / "plugins" / "removed-task-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "removed-task-plugin", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
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
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/removed-task-plugin"]), config_path)
    config_without_plugin = _bind_runtime_paths(Config(plugins=[]), config_path)
    runtime_paths = runtime_paths_for(config_with_plugin)

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        initial = _reload_plugins(config_with_plugin, runtime_paths)
        assert initial.active_plugin_names == ("removed-task-plugin",)

        hooks_path = (plugin_root / "hooks.py").resolve()
        hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path].module
        hooks_module._AUTO_POKE_TASK = task

        removed = _reload_plugins(config_without_plugin, runtime_paths)
        await asyncio.sleep(0)

        assert removed.active_plugin_names == ()
        assert removed.cancelled_task_count == 1
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
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
        initial = _reload_plugins(config, runtime_paths_for(config))
        assert initial.active_plugin_names == ("broken-reload",)

        hooks_path.unlink()

        with pytest.raises(plugin_module.PluginValidationError, match="Plugin hooks module not found"):
            _reload_plugins(config, runtime_paths_for(config))
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


def test_failed_strict_tool_plugin_reload_preserves_previous_live_registry(tmp_path: Path) -> None:
    """A failed strict reload should leave the previous working tool registration usable."""
    plugin_root = tmp_path / "plugins" / "reload-tool"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "reload_tool", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class ReloadTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='reload', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='reload_plugin_tool',\n"
        "    display_name='Reload Plugin Tool',\n"
        "    description='Tool that starts healthy then breaks on reload',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def reload_plugin_tools():\n"
        "    return ReloadTool\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/reload-tool"]), config_path)

    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    try:
        initial = _reload_plugins(config, runtime_paths_for(config))
        assert initial.active_plugin_names == ("reload_tool",)
        assert get_tool_by_name("reload_plugin_tool", runtime_paths_for(config), worker_target=None).name == "reload"

        tools_path.write_text("raise ImportError('reload failure')\n", encoding="utf-8")

        with pytest.raises(plugin_module.PluginValidationError, match="reload failure"):
            _reload_plugins(config, runtime_paths_for(config))

        assert get_tool_by_name("reload_plugin_tool", runtime_paths_for(config), worker_target=None).name == "reload"
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


def test_reload_plugins_skip_broken_plugins_keeps_healthy_plugin_when_explicit_package_spec_breaks(
    tmp_path: Path,
) -> None:
    """Degraded reloads should skip broken explicit package specs and keep healthy plugins active."""
    plugin_root = tmp_path / "plugins" / "good"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "good", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
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
    runtime_paths = _minimal_runtime_paths(tmp_path)
    config = RuntimeConfig.from_authored(
        Config(plugins=["./plugins/good", "python:definitely_missing_package.sub"]),
        runtime_paths,
        tolerate_plugin_load_errors=True,
    )

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    try:
        result = _reload_plugins(config, runtime_paths, skip_broken_plugins=True)

        assert result.active_plugin_names == ("good",)
        assert [hook.plugin_name for hook in result.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["good"]
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)
