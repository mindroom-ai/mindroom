"""Tests for plugin loading and registration."""

from __future__ import annotations

import asyncio
import gc
import importlib
import json
import os
import py_compile
import re
import sys
import uuid
import zipfile
from contextlib import contextmanager
from importlib import machinery
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

import mindroom.tool_system.plugin_imports as plugin_module
import mindroom.tool_system.plugins as plugins_module
import mindroom.tool_system.skills as skills_module
import mindroom.tools  # noqa: F401
from mindroom.config.main import Config, ConfigRuntimeValidationError, load_config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.hooks import EVENT_MESSAGE_RECEIVED
from mindroom.oauth.registry import oauth_provider_registry
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import (
    ConfigPlugins,
    PluginReloadResult,
    candidate_tool_registry,
    config_plugins,
    dispose_plugins,
    get_configured_plugin_roots,
    prepare_plugins,
    publish_plugins,
)
from mindroom.tool_system.registry import ToolMetadataValidationError
from tests.conftest import bind_runtime_paths, cached_plugin_module, publish_test_plugins, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.tool_system.registry import ToolRegistry


def _runtime_paths(config_path: Path) -> RuntimePaths:
    """Return the runtime paths of a config at ``config_path`` whose storage sits beside it."""
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _bind_runtime_paths(config: Config, config_path: Path) -> Config:
    return bind_runtime_paths(config, _runtime_paths(config_path))


def _minimal_runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return _runtime_paths(config_path)


def _write_broken_tool_plugin(plugin_root: Path, tool_name: str = "broken_plugin_tool") -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={tool_name!r},\n"
        "    display_name='Broken Plugin Tool',\n"
        "    file_access=ToolFileAccess.NONE,\n"
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
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={tool_name!r},\n"
        "    display_name='Broken Plugin Tool',\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    description='Tool declared by a plugin that fails before registration',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def broken_plugin_tools():\n"
        "    return BrokenTool\n",
        encoding="utf-8",
    )


def _write_constant_named_broken_tool_plugin(plugin_root: Path, tool_name: str = "constant_named_tool") -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from definitely_missing_plugin_dependency import broken\n"
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        f"TOOL_NAME = {tool_name!r}\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name=TOOL_NAME,\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='Constant Named Tool',\n"
        "    description='Tool registered under a module-level constant name',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def broken_plugin_tools():\n"
        "    return BrokenTool\n",
        encoding="utf-8",
    )


def _write_dynamic_named_broken_tool_plugin(plugin_root: Path, tool_name: str = "dynamic_plugin_tool") -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from definitely_missing_plugin_dependency import broken\n"
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name='_'.join({tool_name.split('_')!r}),\n"
        "    display_name='Dynamic Named Tool',\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    description='Tool whose registered name cannot be recovered statically',\n"
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
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class BrokenTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='broken', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='registered_before_failure',\n"
        "    file_access=ToolFileAccess.NONE,\n"
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
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='Declared After Failure',\n"
        "    description='Tool declared after failure',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def declared_after_failure_tools():\n"
        "    return BrokenTool\n",
        encoding="utf-8",
    )


def _tool_plugin_source(tool_name: str, *, toolkit_name: str = "demo") -> str:
    """Return a tools module registering ``tool_name``, built as an empty toolkit named ``toolkit_name``."""
    return (
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        f"        super().__init__(name={toolkit_name!r}, tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        f"    name={tool_name!r},\n"
        "    file_access=ToolFileAccess.NONE,\n"
        f"    display_name={tool_name!r},\n"
        "    description='A plugin tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n"
    )


def _write_working_tool_plugin(
    plugin_root: Path,
    *,
    plugin_name: str,
    tool_name: str,
    toolkit_name: str = "working",
) -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": plugin_name, "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(_tool_plugin_source(tool_name, toolkit_name=toolkit_name), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    """Move a file's modification time forward, so an edit within the same timestamp tick still counts."""
    mtime = path.stat().st_mtime + 10
    os.utime(path, (mtime, mtime))


@contextmanager
def _forgetting_modules(*prefixes: str) -> Iterator[None]:
    """Forget the modules under ``prefixes`` a block imports, so their next import runs their code again."""
    original_modules = set(sys.modules)
    try:
        yield
    finally:
        for module_name in set(sys.modules) - original_modules:
            if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in prefixes):
                sys.modules.pop(module_name, None)


def _tools(config: Config, runtime_paths: RuntimePaths | None = None) -> ToolRegistry:
    """Return the tools ``config`` can build from the published plugins."""
    return config_plugins(config, runtime_paths or runtime_paths_for(config)).tools


def _publish(config: Config, runtime_paths: RuntimePaths) -> PluginReloadResult:
    """Load ``config``'s plugins and publish them, as an orchestrator commit does."""
    return publish_plugins(prepare_plugins(config, runtime_paths))


def _loaded(config: Config, runtime_paths: RuntimePaths) -> list[Any]:
    """Load and publish ``config``'s plugins and return the ones its view holds, in config order."""
    return list(_load(config, runtime_paths).loaded_plugins)


def _load(config: Config, runtime_paths: RuntimePaths) -> ConfigPlugins:
    """Load and publish ``config``'s plugins as the orchestrator does, and return ``config``'s view of them."""
    _publish(config, runtime_paths)
    return config_plugins(config, runtime_paths)


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        pytest.param(TypeError, "unexpected backend type error", id="type"),
        pytest.param(ValueError, "unexpected backend value error", id="value"),
    ],
)
def test_validate_with_runtime_does_not_mask_unexpected_tool_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_type: type[Exception],
    message: str,
) -> None:
    """Unexpected internal type and value errors escape instead of becoming 422-style config errors."""
    runtime_paths = _minimal_runtime_paths(tmp_path)

    def _raise(_self: Config, _runtime_paths: object, **_kwargs: object) -> None:
        raise error_type(message)

    monkeypatch.setattr(Config, "_validate_authored_tool_entries", _raise)

    with pytest.raises(error_type, match=message):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            },
            runtime_paths,
        )


def test_a_config_collected_while_another_config_s_plugins_are_composed_does_not_block_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config the cyclic collector frees while another config's plugin view is composed lets that call finish.

    The collector runs inside whatever allocation triggers it, so a freed config's cleanup can run on the very thread
    composing the other view.
    """
    runtime_paths = _minimal_runtime_paths(tmp_path)
    compose = ConfigPlugins.of

    def collect_then_compose(generation: object, config: Config, paths: RuntimePaths) -> ConfigPlugins:
        gc.collect()
        return compose(generation, config, paths)

    gc.disable()
    try:
        stale = Config()
        config_plugins(stale, runtime_paths)
        cycle: list[object] = [stale]
        cycle.append(cycle)
        del stale, cycle
        monkeypatch.setattr(ConfigPlugins, "of", collect_then_compose)

        assert config_plugins(Config(), runtime_paths).tools is not None
    finally:
        gc.enable()


def test_plugin_loading_registers_tools_and_skills(tmp_path: Path) -> None:
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
        _tool_plugin_source("demo_plugin"),
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

    plugins = _load(config, runtime_paths_for(config))
    assert [plugin.name for plugin in plugins.loaded_plugins] == ["demo-plugin"]
    assert "demo_plugin" in plugins.tools
    tool = get_tool_by_name("demo_plugin", runtime_paths_for(config), worker_target=None, tools=plugins.tools)
    assert tool.name == "demo"
    assert (plugin_root / "skills").resolve() in plugins.skill_roots


def test_validating_a_candidate_plugin_changes_nothing_live(tmp_path: Path) -> None:
    """Validation resolves a new plugin's tools without publishing them or installing its modules."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("demo_plugin"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    published = plugins_module._PUBLISHED
    module_cache = dict(plugin_module._MODULE_IMPORT_CACHE)
    modules = set(sys.modules)

    candidate, skipped = candidate_tool_registry(config_with_plugin, runtime_paths_for(config_with_plugin))

    assert "demo_plugin" in candidate
    assert skipped == ()
    assert "demo_plugin" not in _tools(config_with_plugin)
    assert plugins_module._PUBLISHED is published
    assert module_cache == plugin_module._MODULE_IMPORT_CACHE
    assert set(sys.modules) == modules


def test_config_validation_reads_a_plugin_manifest_fixed_after_a_rejected_save(tmp_path: Path) -> None:
    """A save rejected because the manifest names a missing tools file must pass once the manifest is fixed."""
    plugin_root = tmp_path / "plugins" / "demo"
    _write_working_tool_plugin(plugin_root, plugin_name="demo", tool_name="demo_tool")
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(json.dumps({"name": "demo", "tools_module": "missing.py"}), encoding="utf-8")
    runtime_paths = _minimal_runtime_paths(tmp_path)
    data = {"plugins": ["./plugins/demo"]}

    with pytest.raises(ConfigRuntimeValidationError, match="Plugin tools module not found"):
        Config.validate_with_runtime(data, runtime_paths)

    manifest_path.write_text(json.dumps({"name": "demo", "tools_module": "tools.py"}), encoding="utf-8")
    _bump_mtime(manifest_path)

    Config.validate_with_runtime(data, runtime_paths)


def test_config_validation_reads_a_plugin_manifest_that_adds_a_tools_module(tmp_path: Path) -> None:
    """Validation must see a tools module that a manifest edit adds to an already validated plugin."""
    plugin_root = tmp_path / "plugins" / "demo"
    _write_working_tool_plugin(plugin_root, plugin_name="demo", tool_name="demo_tool")
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    runtime_paths = _minimal_runtime_paths(tmp_path)
    data = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
        "plugins": ["./plugins/demo"],
    }

    Config.validate_with_runtime(data, runtime_paths)

    manifest_path.write_text(json.dumps({"name": "demo", "tools_module": "tools.py"}), encoding="utf-8")
    _bump_mtime(manifest_path)
    data["agents"]["assistant"]["tools"] = ["demo_tool"]

    config = Config.validate_with_runtime(data, runtime_paths)

    assert config.get_agent("assistant").tool_names == ["demo_tool"]


def test_plugin_loading_from_python_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        _tool_plugin_source("demo_pkg_tool", toolkit_name="demo_pkg"),
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

    plugins = _load(config, runtime_paths_for(config))
    assert [plugin.name for plugin in plugins.loaded_plugins] == ["demo-pkg"]
    assert plugins.loaded_plugins[0].root == plugin_root.resolve()
    assert "demo_pkg_tool" in plugins.tools
    tool = get_tool_by_name("demo_pkg_tool", runtime_paths_for(config), worker_target=None, tools=plugins.tools)
    assert tool.name == "demo_pkg"
    assert (plugin_root / "skills").resolve() in plugins.skill_roots


def test_plugin_loading_from_explicit_python_package_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    plugins = _loaded(config, runtime_paths_for(config))
    assert [plugin.name for plugin in plugins] == ["demo-pkg"]
    assert plugins[0].root == plugin_root.resolve()


def test_explicit_python_plugin_spec_requires_importable_module(tmp_path: Path) -> None:
    """Explicit python: specs should fail closed when the module cannot be resolved."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    with pytest.raises(
        ConfigRuntimeValidationError,
        match="Configured plugin module could not be resolved",
    ):
        _bind_runtime_paths(Config(plugins=["python:missing_demo_pkg"]), config_path)


def test_a_dotted_package_spec_resolves_through_nested_namespace_packages_without_importing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python:outer.inner.demo` finds `demo` inside the PEP 420 namespace packages `outer` and `outer.inner`."""
    outer = f"ns_{uuid.uuid4().hex}"
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / outer / "inner" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(json.dumps({"name": "ns-demo", "skills": []}), encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    plugins = _loaded(Config(plugins=[f"python:{outer}.inner.demo"]), _minimal_runtime_paths(tmp_path))

    assert [plugin.root for plugin in plugins] == [plugin_root.resolve()]
    assert outer not in sys.modules


@pytest.mark.parametrize("middle", ["missing", "module"])
def test_a_dotted_package_spec_whose_middle_part_is_not_a_package_does_not_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    middle: str,
) -> None:
    """Each part before the last must be a package inside the one before; the search never restarts from sys.path."""
    vendor = f"vendor_{uuid.uuid4().hex}"
    demo = f"demo_{uuid.uuid4().hex}"
    site_packages = tmp_path / "site-packages"
    (site_packages / vendor).mkdir(parents=True)
    (site_packages / vendor / "__init__.py").write_text("", encoding="utf-8")
    (site_packages / vendor / "module.py").write_text("", encoding="utf-8")
    # A top-level plugin of the last part's name, which a search restarted from sys.path would find.
    (site_packages / demo).mkdir()
    (site_packages / demo / "mindroom.plugin.json").write_text(json.dumps({"name": "demo", "skills": []}), "utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    spec = f"python:{vendor}.{middle}.{demo}"
    [skipped] = _publish(Config(plugins=[spec]), _minimal_runtime_paths(tmp_path)).skipped

    assert str(skipped.error) == f"Configured plugin module could not be resolved: {spec}"


@pytest.mark.parametrize("suffix", [".", "..demo"])
def test_a_dotted_package_spec_with_an_empty_part_does_not_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    """`python:vendor.` and `python:vendor..demo` name no module, so neither loads the plugin `python:vendor` names."""
    vendor = f"vendor_{uuid.uuid4().hex}"
    site_packages = tmp_path / "site-packages"
    for root, name in ((site_packages / vendor, "vendor"), (site_packages / vendor / "demo", "demo")):
        root.mkdir(parents=True)
        (root / "mindroom.plugin.json").write_text(json.dumps({"name": name, "skills": []}), "utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    spec = f"python:{vendor}{suffix}"
    [skipped] = _publish(Config(plugins=[spec]), _minimal_runtime_paths(tmp_path)).skipped

    assert str(skipped.error) == f"Configured plugin module could not be resolved: {spec}"


def test_configured_plugin_roots_leave_out_a_package_spec_python_cannot_look_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec whose package Python cannot look up, such as a module without `__spec__`, resolves to no root."""
    name = f"nospec_{uuid.uuid4().hex}"
    monkeypatch.setitem(sys.modules, name, ModuleType(name))

    assert get_configured_plugin_roots(Config(plugins=[f"python:{name}.demo"]), _minimal_runtime_paths(tmp_path)) == ()


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


def test_plugin_loading_uses_bound_runtime_paths(tmp_path: Path) -> None:
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

    plugins = _loaded(config, runtime_paths_for(config))

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
def test_plugin_loading_rejects_invalid_manifest_name(tmp_path: Path, plugin_name: str) -> None:
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
def test_plugin_loading_accepts_safe_manifest_names(tmp_path: Path, plugin_name: str) -> None:
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
    plugins = _loaded(config, runtime_paths_for(config))
    assert [plugin.name for plugin in plugins] == [plugin_name]


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
def test_plugin_loading_rejects_malformed_manifests(
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


def test_plugin_loading_rejects_missing_plugin_directory(tmp_path: Path) -> None:
    """Configured plugins must exist on disk instead of being silently skipped."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ValueError, match="Configured plugin path does not exist"):
        _bind_runtime_paths(Config(plugins=["./plugins/missing"]), config_path)


def test_plugin_loading_skips_missing_plugin_directory_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime loading should warn and continue when one plugin path is missing."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    missing_root = (tmp_path / "plugins" / "missing").resolve()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    assert _loaded(Config(plugins=["./plugins/missing"]), runtime_paths) == []
    mock_logger.warning.assert_any_call("Plugin path does not exist, skipping", path=str(missing_root))


def test_plugin_loading_warns_once_for_repeated_missing_plugin_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated runtime loads should not spam the same missing-plugin warning."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    missing_root = (tmp_path / "plugins" / "missing").resolve()
    original_warned_messages = plugin_module._WARNED_PLUGIN_MESSAGES.copy()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        assert _loaded(Config(plugins=["./plugins/missing"]), runtime_paths) == []
        assert _loaded(Config(plugins=["./plugins/missing"]), runtime_paths) == []
        matching_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args == ("Plugin path does not exist, skipping",) and call.kwargs == {"path": str(missing_root)}
        ]
        assert len(matching_calls) == 1
    finally:
        plugin_module._WARNED_PLUGIN_MESSAGES.clear()
        plugin_module._WARNED_PLUGIN_MESSAGES.update(original_warned_messages)


def test_plugin_loading_rejects_missing_tools_module(tmp_path: Path) -> None:
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


def test_validate_with_runtime_does_not_change_published_plugin_skill_roots(tmp_path: Path) -> None:
    """Validating a config that adds a plugin with skills leaves the skills of the published plugins alone."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": None, "skills": ["skills"]}),
        encoding="utf-8",
    )
    (plugin_root / "skills").mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    assert config_plugins(config, runtime_paths_for(config)).skill_roots == ()


def test_plugin_loading_warns_once_for_repeated_non_bundled_plugin_loads(
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
    runtime_paths = _runtime_paths(config_path)
    config = Config(plugins=["./plugins/demo"])

    mock_logger = MagicMock()
    original_warned_messages = plugin_module._WARNED_PLUGIN_MESSAGES.copy()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    try:
        assert [plugin.name for plugin in _loaded(config, runtime_paths)] == ["demo-plugin"]
        assert [plugin.name for plugin in _loaded(config, runtime_paths)] == ["demo-plugin"]
        matching_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args == ("Loading non-bundled plugin",) and call.kwargs == {"path": str(plugin_root.resolve())}
        ]
        assert len(matching_calls) == 1
    finally:
        plugin_module._WARNED_PLUGIN_MESSAGES.clear()
        plugin_module._WARNED_PLUGIN_MESSAGES.update(original_warned_messages)


def test_plugin_loading_revalidates_skill_dirs_when_manifest_cache_is_warm(tmp_path: Path) -> None:
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

    assert [plugin.name for plugin in _loaded(config, runtime_paths_for(config))] == ["demo-plugin"]
    skill_dir.rmdir()
    assert _loaded(config, runtime_paths_for(config)) == []
    assert config_plugins(config, runtime_paths_for(config)).skill_roots == ()


def test_validate_with_runtime_does_not_leak_plugin_tools_after_failure(tmp_path: Path) -> None:
    """A runtime validation that fails publishes none of the plugin tools it loaded."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("leaked_plugin_tool"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    bad_config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "router": {"model": "default"},
            "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
            "plugins": ["./plugins/demo"],
            "defaults": {"tools": ["missing_tool"]},
        },
    )
    with pytest.raises(ValueError, match="Unknown tool 'missing_tool'"):
        _bind_runtime_paths(bad_config, config_path)

    assert plugins_module._PUBLISHED.plugins == {}

    follow_up_config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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


def test_validate_with_runtime_does_not_mutate_live_tool_registry_on_success(tmp_path: Path) -> None:
    """Successful runtime validation does not publish the plugin tools it loaded."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("validated_plugin_tool"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = _runtime_paths(config_path)
    authored_config = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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

    module_cache = dict(plugin_module._MODULE_IMPORT_CACHE)
    validated = Config.validate_with_runtime(authored_config, runtime_paths)
    assert validated.get_agent("assistant").tool_names == ["validated_plugin_tool"]
    assert "validated_plugin_tool" not in config_plugins(validated, runtime_paths).tools
    assert module_cache == plugin_module._MODULE_IMPORT_CACHE


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
    runtime_paths = _runtime_paths(config_path)

    with pytest.raises(ConfigRuntimeValidationError, match=r"hooks\.py"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/broken-hooks"],
            },
            runtime_paths,
        )


def _write_package_helper_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write the importable package plugin ``demo_pkg``, whose tools module imports its helper module by absolute name.

    The helper module registers ``helper_tool``; the plugin's root is returned.
    """
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
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class HelperTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='helper_toolkit', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='helper_tool',\n"
        "    file_access=ToolFileAccess.NONE,\n"
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
    return plugin_root


def test_validate_with_runtime_does_not_mutate_live_registry_for_package_helper_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation does not publish the plugin tools that a helper module of the plugin registers."""
    _write_package_helper_plugin(tmp_path, monkeypatch)

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = _runtime_paths(config_path)

    with _forgetting_modules("demo_pkg"):
        module_cache = dict(plugin_module._MODULE_IMPORT_CACHE)
        validated = Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
        assert "helper_tool" not in config_plugins(validated, runtime_paths).tools
        assert module_cache == plugin_module._MODULE_IMPORT_CACHE
        # Validation leaves no helper it imported, so loading the plugin imports and registers it again.
        assert "demo_pkg.helpers" not in sys.modules
        assert "helper_tool" in _load(validated, runtime_paths).tools


# Loads a config's plugins as the primary publishes them, or as a worker refreshes them.
_PRIMARY_AND_WORKER = pytest.mark.parametrize(
    "load",
    [_load, plugins_module.refresh_plugins],
    ids=["primary", "worker"],
)


@_PRIMARY_AND_WORKER
@pytest.mark.parametrize("first_load_fails", [False, True], ids=["loaded", "failed"])
@pytest.mark.parametrize("helper_import", ["demo_pkg.helpers", ".helpers", "demo_pkg.sub"])
def test_reloading_a_package_plugin_keeps_the_tools_its_helper_module_registers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
    first_load_fails: bool,
    helper_import: str,
) -> None:
    """A tools module edit reloads the helper module it imports, whether the plugin loaded before."""
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    # A subpackage that registers the helper's tool in its own ``__init__.py``.
    (plugin_root / "sub").mkdir()
    (plugin_root / "sub" / "__init__.py").write_text(
        (plugin_root / "helpers.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    healthy_source = f"from {helper_import} import helper_tools\n"
    tools_path.write_text(healthy_source, encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)
    if first_load_fails:
        tools_path.write_text(f"{healthy_source}raise RuntimeError('not yet')\n", encoding="utf-8")

    with _forgetting_modules("demo_pkg"):
        assert ("helper_tool" in load(config, runtime_paths).tools) is not first_load_fails
        helpers_path = plugin_root / ("sub/__init__.py" if helper_import == "demo_pkg.sub" else "helpers.py")
        imported_helpers = [
            module for module in sys.modules.values() if getattr(module, "__file__", None) == str(helpers_path)
        ]
        # A failed load leaves none of the plugin's modules behind.
        assert len(imported_helpers) == (0 if first_load_fails else 1)
        tools_path.write_text(f"{healthy_source}# edited\n", encoding="utf-8")
        _bump_mtime(tools_path)
        assert "helper_tool" in load(config, runtime_paths).tools


def test_a_worker_refresh_whose_edited_tools_module_fails_keeps_the_installed_helper_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker refresh that fails to load the edited plugin reinstates the helper module it had installed."""
    tools_path = _write_package_helper_plugin(tmp_path, monkeypatch) / "tools.py"
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules("demo_pkg"):
        plugins_module.refresh_plugins(config, runtime_paths)
        installed = sys.modules["demo_pkg.helpers"]
        tools_path.write_text(
            f"{tools_path.read_text(encoding='utf-8')}raise RuntimeError('broken')\n",
            encoding="utf-8",
        )
        _bump_mtime(tools_path)
        assert "helper_tool" not in plugins_module.refresh_plugins(config, runtime_paths).tools
        assert sys.modules["demo_pkg.helpers"] is installed


@_PRIMARY_AND_WORKER
def test_a_package_plugins_module_imported_after_its_load_follows_a_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
) -> None:
    """A module the plugin imports by package name only when a tool runs uses the reloaded plugin's classes."""
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    (plugin_root / "errors.py").write_text("class ApiError(Exception):\n    pass\n", encoding="utf-8")
    (plugin_root / "client.py").write_text(
        "from demo_pkg.errors import ApiError\n\n\ndef fetch():\n    raise ApiError('rate limited')\n",
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "from demo_pkg.errors import ApiError\n\n\n"
        "def run():\n"
        "    from demo_pkg.client import fetch\n\n"
        "    try:\n"
        "        fetch()\n"
        "    except ApiError:\n"
        "        return 'handled'\n",
        encoding="utf-8",
    )
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules("demo_pkg"):
        for _ in range(2):
            load(config, runtime_paths)
            assert cached_plugin_module(tools_path).run() == "handled"
            tools_path.write_text(f"{tools_path.read_text(encoding='utf-8')}# edited\n", encoding="utf-8")
            _bump_mtime(tools_path)


def test_a_package_plugins_helper_module_follows_the_published_plugin_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper module a package plugin imports by absolute name is installed and restored with its other modules."""
    _write_package_helper_plugin(tmp_path, monkeypatch)
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules("demo_pkg"):
        _load(config, runtime_paths)
        published = sys.modules["demo_pkg.helpers"]
        generation = prepare_plugins(config, runtime_paths)
        assert sys.modules["demo_pkg.helpers"] is published
        publish_plugins(generation)
        assert sys.modules["demo_pkg.helpers"] is generation.modules["demo_pkg.helpers"] is not published


@_PRIMARY_AND_WORKER
@pytest.mark.parametrize("edited", ["hooks.py", "tools.py"])
@pytest.mark.parametrize("tools_source", ["from .hooks import STATE\n", "from . import hooks\n\nSTATE = hooks.STATE\n"])
def test_a_tools_module_shares_the_hooks_module_it_imports_before_and_after_a_reload(
    tmp_path: Path,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
    edited: str,
    tools_source: str,
) -> None:
    """The plugin's tools and hooks modules hold one copy of state the tools module imports from the hooks module."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text("STATE = {}\n", encoding="utf-8")
    (plugin_root / "tools.py").write_text(tools_source, encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    def shared_state() -> bool:
        load(config, runtime_paths)
        return (
            cached_plugin_module(plugin_root / "tools.py").STATE is cached_plugin_module(plugin_root / "hooks.py").STATE
        )

    assert shared_state()
    edited_path = plugin_root / edited
    edited_path.write_text(edited_path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    _bump_mtime(edited_path)
    assert shared_state()


def _write_nested_package_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    package_source: str = "",
    tools_source: str = "from pack.demo.helpers import STATE\n",
) -> tuple[Path, Config]:
    """Write the package plugin ``pack.demo``, inside the importable package ``pack``, and return its root and config."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "pack" / "demo"
    plugin_root.mkdir(parents=True)
    (site_packages / "pack" / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "__init__.py").write_text(package_source, encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helpers.py").write_text("STATE = {}\n", encoding="utf-8")
    (plugin_root / "errors.py").write_text("class ApiError(Exception):\n    pass\n", encoding="utf-8")
    (plugin_root / "tools.py").write_text(tools_source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))
    return plugin_root, _bind_runtime_paths(Config(plugins=["python:pack.demo"]), tmp_path / "config.yaml")


def test_an_unpublished_load_restores_a_nested_package_plugin_on_its_parent_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load that is not published leaves ``from pack import demo`` and ``pack.demo`` one module again."""
    with _forgetting_modules("pack"):
        _plugin_root, config = _write_nested_package_plugin(tmp_path, monkeypatch)
        runtime_paths = runtime_paths_for(config)
        _load(config, runtime_paths)
        dispose_plugins(prepare_plugins(config, runtime_paths))

        assert sys.modules["pack"].demo is sys.modules["pack.demo"]
        assert sys.modules["pack.demo"].helpers is sys.modules["pack.demo.helpers"]


@_PRIMARY_AND_WORKER
def test_a_reload_imports_a_nested_package_plugin_afresh_through_its_parent_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
) -> None:
    """After a reload, ``from pack import demo`` gives the package that raises the reloaded exception class."""
    with _forgetting_modules("pack"):
        plugin_root, config = _write_nested_package_plugin(
            tmp_path,
            monkeypatch,
            package_source="from pack.demo.errors import ApiError\n\n\ndef fail():\n    raise ApiError\n",
            tools_source=(
                "from pack import demo\n"
                "from pack.demo.errors import ApiError\n\n\n"
                "def run():\n"
                "    try:\n"
                "        demo.fail()\n"
                "    except ApiError:\n"
                "        return 'handled'\n"
            ),
        )
        runtime_paths = runtime_paths_for(config)
        tools_path = plugin_root / "tools.py"
        for _ in range(2):
            load(config, runtime_paths)
            assert cached_plugin_module(tools_path).run() == "handled"
            tools_path.write_text(f"{tools_path.read_text(encoding='utf-8')}# edited\n", encoding="utf-8")
            _bump_mtime(tools_path)


@_PRIMARY_AND_WORKER
def test_a_subpackage_keeps_the_name_its_package_rebinds(
    tmp_path: Path,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
) -> None:
    """A subpackage that re-exports ``search`` from its ``search`` module still offers the function once published."""
    plugin_root = tmp_path / "plugins" / "demo"
    (plugin_root / "api").mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "api" / "__init__.py").write_text("from .search import search\n", encoding="utf-8")
    (plugin_root / "api" / "search.py").write_text(
        "def search(query):\n    return f'found {query}'\n",
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text("from . import api\n\n\ndef run():\n    return api.search('x')\n", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")

    load(config, runtime_paths_for(config))

    assert cached_plugin_module(tools_path).run() == "found x"


def test_validation_executes_a_module_another_plugin_module_imported_once(tmp_path: Path) -> None:
    """Candidate validation reuses the hooks module the tools module imported, as loading the plugin does."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    runs = tmp_path / "hooks-runs.txt"
    (plugin_root / "hooks.py").write_text(
        f"with open({str(runs)!r}, 'a') as runs:\n    runs.write('run\\n')\n",
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text("from . import hooks\n", encoding="utf-8")

    _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")

    assert runs.read_text(encoding="utf-8").splitlines() == ["run"]


@_PRIMARY_AND_WORKER
def test_a_reload_runs_a_sibling_imported_module_from_its_source_not_a_stale_bytecode_cache(
    tmp_path: Path,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
) -> None:
    """A same-size edit that keeps the hooks module's mtime runs at the next reload, though its bytecode cache stays valid."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VERSION = 1\n", encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text("from .hooks import VERSION\n", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)
    load(config, runtime_paths)
    assert cached_plugin_module(hooks_path).VERSION == 1

    # The cache an import of the first version writes, then a same-size edit within its mtime.
    mtime = hooks_path.stat().st_mtime
    py_compile.compile(str(hooks_path), invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    hooks_path.write_text("VERSION = 2\n", encoding="utf-8")
    os.utime(hooks_path, (mtime, mtime))
    tools_path.write_text("from .hooks import VERSION\n# edited\n", encoding="utf-8")
    _bump_mtime(tools_path)
    load(config, runtime_paths)

    assert cached_plugin_module(hooks_path).VERSION == 2
    assert cached_plugin_module(tools_path).VERSION == 2


def _edit(path: Path) -> None:
    """Append a comment to ``path`` and move its mtime on, so the next load counts it as changed."""
    path.write_text(f"{path.read_text(encoding='utf-8')}# edited\n", encoding="utf-8")
    _bump_mtime(path)


def _write_package_hooks_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools_source: str) -> Path:
    """Write the package plugin ``demo_pkg`` with ``tools_source`` as its tools module and ``STATE`` in its hooks."""
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-pkg", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text("STATE = {}\n", encoding="utf-8")
    (plugin_root / "tools.py").write_text(tools_source, encoding="utf-8")
    return plugin_root


@_PRIMARY_AND_WORKER
@pytest.mark.parametrize(
    "tools_source",
    [
        "from demo_pkg.hooks import STATE\n\n\ndef state():\n    return STATE\n",
        "def state():\n    from demo_pkg.hooks import STATE\n\n    return STATE\n",
    ],
    ids=["at-import", "when-a-tool-runs"],
)
def test_a_manifest_module_imported_by_package_name_is_the_module_mindroom_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
    tools_source: str,
) -> None:
    """`from demo_pkg.hooks import STATE` reads the hooks module MindRoom runs, before and after a reload."""
    plugin_root = _write_package_hooks_plugin(tmp_path, monkeypatch, tools_source)
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules("demo_pkg"):
        loaded_hooks = []
        for _ in range(2):
            load(config, runtime_paths)
            hooks = cached_plugin_module(plugin_root / "hooks.py")
            assert cached_plugin_module(plugin_root / "tools.py").state() is hooks.STATE
            assert sys.modules["demo_pkg.hooks"] is hooks
            loaded_hooks.append(hooks)
            _edit(plugin_root / "tools.py")
        # The reload executed the hooks module again.
        assert loaded_hooks[0] is not loaded_hooks[1]


@_PRIMARY_AND_WORKER
def test_a_plugin_that_puts_its_root_on_sys_path_shares_the_modules_it_imports_by_top_level_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
) -> None:
    """A tools module that adds its directory to ``sys.path`` and imports the hooks module by name shares its state."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    hooks_name = f"hooks_{uuid.uuid4().hex}"
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "hooks_module": f"{hooks_name}.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / f"{hooks_name}.py").write_text("STATE = {}\n", encoding="utf-8")
    (plugin_root / "tools.py").write_text(
        "import sys\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n\n"
        f"from {hooks_name} import STATE\n",
        encoding="utf-8",
    )
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules(hooks_name):
        loaded_hooks = []
        for _ in range(2):
            load(config, runtime_paths)
            hooks = cached_plugin_module(plugin_root / f"{hooks_name}.py")
            assert cached_plugin_module(plugin_root / "tools.py").STATE is hooks.STATE
            loaded_hooks.append(hooks)
            _edit(plugin_root / "tools.py")
        # The reload executed the hooks module again.
        assert loaded_hooks[0] is not loaded_hooks[1]


def _same_size_edit_in_place(path: Path, source: str) -> None:
    """Cache ``path``'s bytecode, then write ``source`` of the same size, keeping the mtime that cache records."""
    mtime = path.stat().st_mtime
    py_compile.compile(str(path), invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    path.write_text(source, encoding="utf-8")
    os.utime(path, (mtime, mtime))


@_PRIMARY_AND_WORKER
@pytest.mark.parametrize("looked_up_before", [False, True], ids=["new", "looked-up-before"])
def test_a_reload_runs_a_helper_in_a_subpackage_from_its_source_not_a_stale_bytecode_cache(
    tmp_path: Path,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
    looked_up_before: bool,
) -> None:
    """A same-size edit of a helper in a subpackage that keeps its mtime runs at the next reload."""
    plugin_root = tmp_path / "plugins" / "demo"
    (plugin_root / "lib").mkdir(parents=True)
    if looked_up_before:
        # Python caches the finder of a directory it searched before any config named the plugin.
        machinery.PathFinder.find_spec("util", [str(plugin_root.resolve() / "lib")])
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    helper_path = plugin_root / "lib" / "util.py"
    helper_path.write_text("VERSION = 1\n", encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text("from .lib.util import VERSION\n", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)
    load(config, runtime_paths)
    assert cached_plugin_module(tools_path).VERSION == 1

    _same_size_edit_in_place(helper_path, "VERSION = 2\n")
    _edit(tools_path)
    load(config, runtime_paths)

    assert cached_plugin_module(tools_path).VERSION == 2


@_PRIMARY_AND_WORKER
def test_a_reload_runs_a_helper_imported_through_a_linked_sys_path_entry_from_its_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: Callable[[Config, RuntimePaths], ConfigPlugins],
) -> None:
    """A package plugin Python imports through a symlinked directory still runs a helper's same-size edit."""
    plugin_root = tmp_path / "site-packages" / "demo_pkg"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-pkg", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    helper_path = plugin_root / "helpers.py"
    helper_path.write_text("VERSION = 1\n", encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text("from demo_pkg.helpers import VERSION\n", encoding="utf-8")
    linked_site_packages = tmp_path / "linked-site-packages"
    linked_site_packages.symlink_to(plugin_root.parent)
    monkeypatch.syspath_prepend(str(linked_site_packages))
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    # Python gives the module the file name it imports it from.
    imported_tools_path = linked_site_packages / "demo_pkg" / "tools.py"

    with _forgetting_modules("demo_pkg"):
        load(config, runtime_paths)
        assert cached_plugin_module(imported_tools_path).VERSION == 1

        _same_size_edit_in_place(helper_path, "VERSION = 2\n")
        _edit(tools_path)
        load(config, runtime_paths)

        assert cached_plugin_module(imported_tools_path).VERSION == 2


def test_validation_counts_the_helper_tools_of_a_published_package_plugin_whose_tools_module_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validating a config after the plugin's tools module changes still finds the tool its helper module registers."""
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules("demo_pkg"):
        _load(config, runtime_paths)
        installed_helpers = sys.modules["demo_pkg.helpers"]
        _edit(plugin_root / "tools.py")

        tools, skipped = candidate_tool_registry(config, runtime_paths)

        assert "helper_tool" in tools
        assert skipped == ()
        assert sys.modules["demo_pkg.helpers"] is installed_helpers


def test_a_package_plugin_whose_tools_module_is_its_package_registers_its_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest naming the package's ``__init__.py`` as its tools module loads that package and its tools."""
    name = f"initpkg_{uuid.uuid4().hex}"
    plugin_root = tmp_path / "site-packages" / name
    _write_working_tool_plugin(plugin_root, plugin_name="init-pkg", tool_name="init_pkg_tool")
    (plugin_root / "tools.py").rename(plugin_root / "__init__.py")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "init-pkg", "tools_module": "__init__.py", "skills": []}),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(plugin_root.parent))

    plugins = _load(Config(plugins=[name]), _minimal_runtime_paths(tmp_path))

    assert "init_pkg_tool" in plugins.tools
    assert cached_plugin_module(plugin_root.resolve() / "__init__.py") is sys.modules[name]


@pytest.mark.parametrize("holder", ["sys-path", "sys-modules", "no-spec"])
def test_a_plugin_whose_package_name_another_module_holds_loads_its_own_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holder: str,
) -> None:
    """A plugin whose package name Python gives another module, or cannot look up, still runs its own tools module."""
    name = f"shadow_{uuid.uuid4().hex}"
    plugin_root = tmp_path / "site-packages" / name
    _write_working_tool_plugin(plugin_root, plugin_name="shadowed", tool_name="shadowed_tool")
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(plugin_root.parent))
    if holder == "sys-path":
        other = tmp_path / "earlier" / name
        other.mkdir(parents=True)
        (other / "__init__.py").write_text("", encoding="utf-8")
        (other / "tools.py").write_text("raise RuntimeError('another package')\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(other.parent))
    elif holder == "sys-modules":
        tools = ModuleType(f"{name}.tools")
        tools.__file__ = str(tmp_path / "elsewhere" / "tools.py")
        monkeypatch.setitem(sys.modules, tools.__name__, tools)
    else:
        monkeypatch.setitem(sys.modules, name, ModuleType(name))

    holders = {module_name: module for module_name, module in sys.modules.items() if module_name.startswith(name)}

    plugins = _load(Config(plugins=[str(plugin_root)]), _minimal_runtime_paths(tmp_path))

    assert "shadowed_tool" in plugins.tools
    assert cached_plugin_module(plugin_root.resolve() / "tools.py").demo_plugin_tools().__name__ == "DemoTool"
    # The module that holds the name stays where it is.
    assert all(sys.modules.get(module_name) is module for module_name, module in holders.items())


def test_a_plugin_imports_a_zip_archive_inside_its_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tools module that puts a zip archive from its own directory on ``sys.path`` imports from it."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    vendored = f"vendored_{uuid.uuid4().hex}"
    plugin_root = tmp_path / "plugins" / "demo"
    _write_working_tool_plugin(plugin_root, plugin_name="demo", tool_name="zip_tool")
    with zipfile.ZipFile(plugin_root / "vendor.zip", "w") as archive:
        archive.writestr(f"{vendored}.py", "VALUE = 'from the archive'\n")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "import sys\n"
        "from pathlib import Path\n\n"
        "sys.path.append(str(Path(__file__).with_name('vendor.zip')))\n\n"
        f"from {vendored} import VALUE\n",
        encoding="utf-8",
    )

    with _forgetting_modules(vendored):
        _load(Config(plugins=["./plugins/demo"]), _minimal_runtime_paths(tmp_path))

        assert cached_plugin_module(tools_path.resolve()).VALUE == "from the archive"


@pytest.mark.usefixtures("restored_builtin_tools")
def test_a_tools_module_imported_by_its_package_name_before_the_plugin_first_loads_registers_its_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tools module something imported by its package name while the plugin's manifest was broken registers later."""
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text("{", encoding="utf-8")
    config = Config(plugins=["demo_pkg"])
    runtime_paths = _minimal_runtime_paths(tmp_path)

    with _forgetting_modules("demo_pkg"):
        assert "helper_tool" not in _load(config, runtime_paths).tools
        importlib.import_module("demo_pkg.tools")
        manifest_path.write_text(manifest, encoding="utf-8")
        _bump_mtime(manifest_path)

        assert "helper_tool" in _load(config, runtime_paths).tools


@pytest.mark.asyncio
async def test_a_worker_refresh_ends_the_task_of_a_module_its_plugin_imported_after_loading(tmp_path: Path) -> None:
    """A module a tool imports only when it runs is replaced on a reload with its task ended, so one task runs."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "client.py").write_text(
        "import asyncio\n\nTASK = asyncio.get_running_loop().create_task(asyncio.Event().wait())\n",
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text("def start():\n    from . import client\n\n    return client.TASK\n", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    plugins_module.refresh_plugins(config, runtime_paths)
    first = cached_plugin_module(tools_path).start()
    tools_path.write_text(f"{tools_path.read_text(encoding='utf-8')}# edited\n", encoding="utf-8")
    _bump_mtime(tools_path)
    plugins_module.refresh_plugins(config, runtime_paths)
    second = cached_plugin_module(tools_path).start()
    await asyncio.sleep(0)

    try:
        assert first.cancelled()
        assert not second.done()
    finally:
        second.cancel()


def test_a_worker_serves_a_module_saved_while_its_plugin_loaded(tmp_path: Path) -> None:
    """A hooks module saved after the tools module imported it, while the load runs, is served at the next refresh."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VERSION = 1\n", encoding="utf-8")
    (plugin_root / "tools.py").write_text("", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)
    # The first load saves the hooks module, moving its mtime on, after the tools module imported it.
    (plugin_root / "tools.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "from .hooks import VERSION\n\n"
        "if VERSION == 1:\n"
        "    _hooks = Path(__file__).with_name('hooks.py')\n"
        "    _mtime = _hooks.stat().st_mtime + 10\n"
        "    _hooks.write_text('VERSION = 2\\n')\n"
        "    os.utime(_hooks, (_mtime, _mtime))\n",
        encoding="utf-8",
    )

    plugins_module.refresh_plugins(config, runtime_paths)
    plugins_module.refresh_plugins(config, runtime_paths)

    assert cached_plugin_module(hooks_path).VERSION == 2


def test_plugin_loading_removes_tools_for_successfully_removed_plugins(tmp_path: Path) -> None:
    """Removing a previously loaded plugin should also remove its tool registrations."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("removed_plugin_tool"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    config_without_plugin = _bind_runtime_paths(Config(plugins=[]), config_path)

    assert [plugin.name for plugin in _loaded(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
        "demo_plugin",
    ]
    assert "removed_plugin_tool" in _tools(config_with_plugin)

    assert _loaded(config_without_plugin, runtime_paths_for(config_without_plugin)) == []
    assert "removed_plugin_tool" not in _tools(config_without_plugin)
    # The plugin's tool lists among neither tools nor unavailable ones of a config that dropped it.
    assert "removed_plugin_tool" not in _tools(config_without_plugin).unavailable
    assert "removed_plugin_tool" not in _tools(config_with_plugin)

    follow_up_config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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


def test_plugin_loading_re_registers_tools_when_plugin_is_re_enabled(tmp_path: Path) -> None:
    """Re-enabling an unchanged plugin in the same process should restore its tools."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("toggled_plugin_tool"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_with_plugin = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    config_without_plugin = _bind_runtime_paths(Config(plugins=[]), config_path)

    assert [plugin.name for plugin in _loaded(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
        "demo_plugin",
    ]
    assert "toggled_plugin_tool" in _tools(config_with_plugin)

    assert _loaded(config_without_plugin, runtime_paths_for(config_without_plugin)) == []
    assert "toggled_plugin_tool" not in _tools(config_without_plugin)

    assert [plugin.name for plugin in _loaded(config_with_plugin, runtime_paths_for(config_with_plugin))] == [
        "demo_plugin",
    ]
    assert "toggled_plugin_tool" in _tools(config_with_plugin)

    follow_up_config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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


def test_plugin_loading_preserves_metadata_only_built_in_tools(tmp_path: Path) -> None:
    """A config without plugins still offers the metadata-only built-in tools."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config_without_plugins = _bind_runtime_paths(Config(plugins=[]), config_path)

    assert _loaded(config_without_plugins, runtime_paths_for(config_without_plugins)) == []
    for tool_name in ("memory", "delegate", "self_config", "compact_context"):
        assert tool_name in _tools(config_without_plugins)


def test_plugin_loading_removes_stale_tools_when_enabled_plugin_changes_exports(tmp_path: Path) -> None:
    """Reloading an enabled plugin should drop tool names it no longer registers."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        _tool_plugin_source("old_tool"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    assert [plugin.name for plugin in _loaded(config, runtime_paths_for(config))] == ["demo_plugin"]
    assert "old_tool" in _tools(config)

    tools_path.write_text(
        _tool_plugin_source("new_tool"),
        encoding="utf-8",
    )
    stat_result = tools_path.stat()
    os.utime(tools_path, (stat_result.st_atime, stat_result.st_mtime + 1))

    assert [plugin.name for plugin in _loaded(config, runtime_paths_for(config))] == ["demo_plugin"]
    assert "old_tool" not in _tools(config)
    assert "new_tool" in _tools(config)


def test_plugin_loading_rejects_built_in_tool_name_collisions(tmp_path: Path) -> None:
    """Plugin tools must not shadow built-in tool registrations."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("calculator"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ConfigRuntimeValidationError, match="conflicts with built-in tool 'calculator'"):
        _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)


def test_a_plugin_edit_taking_a_configured_mcp_server_s_tool_name_fails_the_load_as_a_whole(tmp_path: Path) -> None:
    """Preparing the plugins of a config whose MCP server one plugin tool now names refuses, so nothing publishes."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(_tool_plugin_source("crm_notes"), encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(
        Config(
            plugins=["./plugins/demo"],
            mcp_servers={"crm": {"transport": "stdio", "command": "crm-mcp"}},
        ),
        config_path,
    )
    _publish(config, runtime_paths_for(config))
    tools_path.write_text(_tool_plugin_source("mcp_crm"), encoding="utf-8")
    os.utime(tools_path, ns=(1, 1))

    with pytest.raises(ToolMetadataValidationError, match="MCP tool 'mcp_crm' conflicts"):
        prepare_plugins(config, runtime_paths_for(config))

    assert "crm_notes" in _tools(config)
    assert "mcp_crm" in _tools(config)


@pytest.mark.parametrize(
    ("modules", "oauth_source", "error"),
    [
        pytest.param(
            {"oauth_module": "oauth.py"},
            "import missing_oauth_dependency\n",
            "No module named 'missing_oauth_dependency'",
            id="import",
        ),
        pytest.param(
            {"oauth_module": "oauth.py"},
            "PROVIDERS = []\n",
            "must define callable register_oauth_providers",
            id="callback",
        ),
        pytest.param(
            {"tools_module": "oauth.py", "oauth_module": "oauth.py"},
            "PROVIDERS = []\n",
            "must define callable register_oauth_providers",
            id="callback-in-the-tools-module",
        ),
        pytest.param(
            {"hooks_module": "oauth.py", "oauth_module": "oauth.py"},
            "PROVIDERS = []\n",
            "must define callable register_oauth_providers",
            id="callback-in-the-hooks-module",
        ),
    ],
)
def test_config_validation_rejects_a_plugin_whose_oauth_module_does_not_load(
    tmp_path: Path,
    modules: dict[str, str],
    oauth_source: str,
    error: str,
) -> None:
    """Validation executes a plugin's OAuth module as loading it does, so a plugin loading would skip is rejected.

    That holds when the OAuth module is also the plugin's tools or hooks module.
    """
    plugin_root = tmp_path / "plugins" / "oauth-demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "oauth-demo", **modules, "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "oauth.py").write_text(oauth_source, encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    with pytest.raises(ConfigRuntimeValidationError, match=error):
        _bind_runtime_paths(Config(plugins=["./plugins/oauth-demo"]), config_path)


def test_plugin_loading_rejects_plugin_tool_name_collisions(tmp_path: Path) -> None:
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
            "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
            "\n"
            "class DemoTool(Toolkit):\n"
            "    def __init__(self) -> None:\n"
            "        super().__init__(name='demo', tools=[])\n"
            "\n"
            "@register_tool_with_metadata(\n"
            "    name='shared_tool',\n"
            "    file_access=ToolFileAccess.NONE,\n"
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


def test_plugin_loading_rejects_duplicate_tool_names_within_one_plugin(tmp_path: Path) -> None:
    """One plugin module must not register the same tool name twice."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
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
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='First Duplicate',\n"
        "    description='Should fail',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def first_plugin_tool():\n"
        "    return FirstTool\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='dup_tool',\n"
        "    file_access=ToolFileAccess.NONE,\n"
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


def test_plugin_loading_preserves_tools_when_manifest_name_changes(tmp_path: Path) -> None:
    """Changing only the manifest plugin name should force a logical module reload."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(
        json.dumps({"name": "demo_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        _tool_plugin_source("renamed_tool"),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)

    assert [plugin.name for plugin in _loaded(config, runtime_paths_for(config))] == ["demo_plugin"]
    assert "renamed_tool" in _tools(config)

    manifest_path.write_text(
        json.dumps({"name": "renamed_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    stat_result = manifest_path.stat()
    os.utime(manifest_path, (stat_result.st_atime, stat_result.st_mtime + 1))

    assert [plugin.name for plugin in _loaded(config, runtime_paths_for(config))] == ["renamed_plugin"]
    assert "renamed_tool" in _tools(config)

    follow_up_config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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


def test_plugin_loading_follows_a_manifest_that_moves_the_tools_module(tmp_path: Path) -> None:
    """A load after a manifest edit that moves the tools module to another file must keep the plugin's tools."""
    plugin_root = tmp_path / "plugins" / "demo"
    _write_working_tool_plugin(plugin_root, plugin_name="demo", tool_name="moved_tool")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    runtime_paths = runtime_paths_for(config)

    assert [plugin.name for plugin in _loaded(config, runtime_paths)] == ["demo"]

    (plugin_root / "tools.py").rename(plugin_root / "moved_tools.py")
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(json.dumps({"name": "demo", "tools_module": "moved_tools.py"}), encoding="utf-8")
    _bump_mtime(manifest_path)

    assert [plugin.name for plugin in _loaded(config, runtime_paths)] == ["demo"]
    assert _tools(config).factory("moved_tool").__module__.endswith(".moved_tools")


def test_plugin_loading_reads_a_manifest_edited_after_a_discarded_staged_reload(tmp_path: Path) -> None:
    """A manifest edit after a staged plugin reload that was never applied must reach the next load."""
    plugin_root = tmp_path / "plugins" / "demo"
    _write_working_tool_plugin(plugin_root, plugin_name="demo", tool_name="staged_tool")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/demo"]), config_path)
    runtime_paths = runtime_paths_for(config)

    dispose_plugins(prepare_plugins(config, runtime_paths))

    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text(json.dumps({"name": "renamed", "tools_module": "tools.py"}), encoding="utf-8")
    _bump_mtime(manifest_path)

    assert [plugin.name for plugin in _loaded(config, runtime_paths)] == ["renamed"]


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
        _tool_plugin_source("good_plugin_tool"),
        encoding="utf-8",
    )
    (bad_root / "tools.py").write_text("from definitely_missing_plugin_dependency import broken\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)
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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == (
        "shell",
        "scheduler",
    )
    assert any(
        call.args == ("Plugin tool unavailable because plugin failed to load",)
        and call.kwargs["tool_name"] == "broken_plugin_tool"
        and call.kwargs["config_path"] == "agents.assistant.tools[1]"
        for call in mock_logger.warning.call_args_list
    )


@pytest.mark.asyncio
async def test_a_repaired_plugin_s_tool_returns_to_the_live_config_after_an_explicit_reload(tmp_path: Path) -> None:
    """Startup skips a broken plugin and the tool it declares; once its file is fixed, ``!reload-plugins`` restores it.

    No config edit follows the repair, so the live config stays the object startup validated.
    """
    plugin_root = tmp_path / "plugins" / "broken"
    _write_broken_tool_plugin(plugin_root)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-6-astra\n"
        "agents:\n"
        "  assistant:\n"
        "    display_name: Assistant\n"
        "    tools:\n"
        "      - broken_plugin_tool\n"
        "plugins:\n"
        "  - ./plugins/broken\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)
    assert (
        "broken_plugin_tool" not in resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names
    )
    tools_path = plugin_root / "tools.py"
    repaired = tools_path.read_text(encoding="utf-8").replace(
        "raise ImportError('missing optional plugin dependency')\n",
        "",
    )
    tools_path.write_text(repaired, encoding="utf-8")
    _bump_mtime(tools_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths, config=config)
    orchestrator.running = True

    result = await orchestrator.reload_plugins_now(source="command")

    assert result.active_plugin_names == ("broken_plugin",)
    candidates = resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).candidates()
    assert "broken_plugin_tool" in [entry.name for entry in candidates]


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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == ("scheduler",)
    assert any(
        call.args == ("Plugin tool unavailable because plugin failed to load",)
        and call.kwargs["tool_name"] == "broken_plugin_tool"
        and call.kwargs["config_path"] == "agents.assistant.tools[0]"
        for call in mock_logger.warning.call_args_list
    )


def test_load_config_tolerates_broken_plugin_tool_named_by_module_constant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken-plugin tool names assigned via module-level constants should still be recovered."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_constant_named_broken_tool_plugin(plugin_root)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    tools:\n"
            "      - shell\n"
            "      - constant_named_tool\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == (
        "shell",
        "scheduler",
    )
    assert any(
        call.args == ("Plugin tool unavailable because plugin failed to load",)
        and call.kwargs["tool_name"] == "constant_named_tool"
        and call.kwargs["config_path"] == "agents.assistant.tools[1]"
        for call in mock_logger.warning.call_args_list
    )


def test_load_config_disables_unknown_tool_when_plugin_tool_namespace_is_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown tools must not block tolerant startup while a failed plugin could explain them."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_dynamic_named_broken_tool_plugin(plugin_root)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    tools:\n"
            "      - shell\n"
            "      - dynamic_plugin_tool:\n"
            "          some_option: ignored\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == (
        "shell",
        "scheduler",
    )
    assert any(
        call.args
        == (
            "Unknown tool may belong to a plugin whose tool names could not be resolved; "
            "disabling it for this run (verify the tool name is not a typo)",
        )
        and call.kwargs["tool_name"] == "dynamic_plugin_tool"
        and call.kwargs["config_path"] == "agents.assistant.tools[1]"
        and call.kwargs["unresolved_plugin_sources"] == ["broken_plugin"]
        for call in mock_logger.warning.call_args_list
    )


def test_load_config_tolerates_unresolved_listen_tool_like_a_tools_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listen tool a failed plugin could explain warns and loads, as an unknown ``tools:`` entry does."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_dynamic_named_broken_tool_plugin(plugin_root)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    rooms: [lobby]\n"
            "    tools: [memory, dynamic_plugin_tool]\n"
            "    listen:\n"
            "      rooms: [lobby]\n"
            "      tools: [memory, dynamic_plugin_tool.run]\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
        ),
        encoding="utf-8",
    )
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    load_config(_runtime_paths(config_path), tolerate_plugin_load_errors=True)

    assert any(
        call.kwargs.get("config_path") == "agents.assistant.listen.tools[1]"
        and call.kwargs["tool_name"] == "dynamic_plugin_tool"
        for call in mock_logger.warning.call_args_list
    )


def test_load_config_disables_unknown_tool_when_plugin_fails_before_manifest_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved plugin manifest must leave its possible tool namespace unresolved."""
    plugin_root = tmp_path / "plugins" / "broken"
    plugin_root.mkdir(parents=True)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    tools:\n"
            "      - shell\n"
            "      - manifestless_plugin_tool\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == (
        "shell",
        "scheduler",
    )
    assert any(
        call.args
        == (
            "Unknown tool may belong to a plugin whose tool names could not be resolved; "
            "disabling it for this run (verify the tool name is not a typo)",
        )
        and call.kwargs["tool_name"] == "manifestless_plugin_tool"
        and call.kwargs["config_path"] == "agents.assistant.tools[1]"
        and call.kwargs["unresolved_plugin_sources"] == ["./plugins/broken"]
        for call in mock_logger.warning.call_args_list
    )


def test_failed_hooks_only_plugin_does_not_hide_unknown_tool_typo(tmp_path: Path) -> None:
    """A hooks-only failure cannot explain an unknown authored tool name."""
    plugin_root = tmp_path / "plugins" / "broken"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_hooks", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
        "from definitely_missing_plugin_dependency import broken\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    tools:\n"
            "      - typo_plugin_tool\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(config_path)

    with pytest.raises(ConfigRuntimeValidationError, match="Unknown tool 'typo_plugin_tool'"):
        load_config(runtime_paths, tolerate_plugin_load_errors=True)


def test_load_config_treats_plugin_system_exit_as_load_error(tmp_path: Path) -> None:
    """Plugin SystemExit should become strict validation failure or tolerant unavailability."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_pre_registration_broken_tool_plugin(plugin_root, tool_name="system_exit_tool")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        tools_path.read_text(encoding="utf-8").replace(
            "from definitely_missing_plugin_dependency import broken",
            "raise SystemExit('plugin exit')",
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  assistant:\n"
            "    display_name: Assistant\n"
            "    role: test\n"
            "    tools: [system_exit_tool]\n"
            "plugins:\n"
            "  - ./plugins/broken\n"
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(config_path)

    with pytest.raises(ConfigRuntimeValidationError, match="plugin exit"):
        load_config(runtime_paths)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    # The tolerant publication skips the exiting plugin, and its tool stays authored but unavailable.
    [skipped] = _publish(config, runtime_paths).skipped

    assert "plugin exit" in str(skipped.error)
    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == ("scheduler",)


def test_load_config_propagates_plugin_keyboard_interrupt(tmp_path: Path) -> None:
    """Operator interrupts during plugin validation should still terminate startup."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_pre_registration_broken_tool_plugin(plugin_root)
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        tools_path.read_text(encoding="utf-8").replace(
            "from definitely_missing_plugin_dependency import broken",
            "raise KeyboardInterrupt('stop')",
        ),
        encoding="utf-8",
    )
    runtime_paths = _minimal_runtime_paths(tmp_path)

    with pytest.raises(KeyboardInterrupt, match="stop"):
        Config.validate_with_runtime(
            {"plugins": ["./plugins/broken"]},
            runtime_paths,
            tolerate_plugin_load_errors=True,
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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)
    config = Config(plugins=["./plugins/broken"])

    _publish(config, runtime_paths)
    runtime_tools = config_plugins(config, runtime_paths).tools
    validation_snapshot = candidate_tool_registry(config, runtime_paths)[0].validation_snapshot

    assert "broken_plugin_tool" not in runtime_tools
    assert "broken_plugin_tool" not in runtime_tools.listable()
    assert runtime_tools.validation_snapshot["broken_plugin_tool"].unavailable_due_to_plugin_load_error is True
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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == ("scheduler",)
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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.config.main.logger", mock_logger)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    agent_tools = resolve_agent_tools(config, "assistant", _tools(config, runtime_paths))
    # The deferred selection stays authored, but no turn attaches a tool that cannot load.
    assert [entry.name for entry in agent_tools.facts.authored if entry.defer] == ["broken_plugin_tool"]
    assert "broken_plugin_tool" not in [entry.name for entry in agent_tools.candidates()]
    assert agent_tools.tool_names == ("scheduler",)
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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == (
        "shell",
        "scheduler",
    )


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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)

    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    _publish(config, runtime_paths)

    assert resolve_agent_tools(config, "assistant", _tools(config, runtime_paths)).tool_names == (
        "healthy_plugin_tool",
        "scheduler",
    )


def test_load_config_still_rejects_unknown_tool_without_broken_plugin_explanation(tmp_path: Path) -> None:
    """Tolerant startup should not convert ordinary unknown tools into warnings."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)

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
            "    id: gpt-6-astra\n"
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
    runtime_paths = _runtime_paths(config_path)

    with pytest.raises(ConfigRuntimeValidationError, match="Unknown tool 'typo_plugin_tool'"):
        load_config(runtime_paths, tolerate_plugin_load_errors=True)


def test_plugin_loading_skips_later_broken_plugin_and_keeps_earlier_tools(
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
        _tool_plugin_source("good_plugin_tool"),
        encoding="utf-8",
    )
    (bad_root / "tools.py").write_text("from definitely_missing_plugin_dependency import broken\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = _runtime_paths(config_path)
    config = Config(plugins=["./plugins/good", "./plugins/bad"])

    mock_logger = MagicMock()

    monkeypatch.setattr(plugin_module, "logger", mock_logger)

    plugins = _loaded(config, runtime_paths)
    assert [plugin.name for plugin in plugins] == ["good_plugin"]
    assert "good_plugin_tool" in _tools(config, runtime_paths)
    assert any(
        call.args == ("Failed to load plugin, skipping",)
        and call.kwargs["path"] == str(bad_root.resolve())
        and "definitely_missing_plugin_dependency" in call.kwargs["error"]
        for call in mock_logger.warning.call_args_list
    )


def test_plugin_loading_skips_system_exit_during_plugin_module_execution(tmp_path: Path) -> None:
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

    plugins = _load(config, runtime_paths)

    assert [plugin.name for plugin in plugins.loaded_plugins] == ["good_plugin"]
    registry = plugins.hooks
    assert [hook.plugin_name for hook in registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["good_plugin"]


def test_plugin_loading_records_system_exit_during_hook_discovery_as_a_validation_error(tmp_path: Path) -> None:
    """A SystemExit raised while discovering a loaded module's hooks skips the plugin with a validation error."""
    plugin_root = tmp_path / "plugins" / "bad"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "bad_plugin", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
        "class _ExitOnLookup:\n"
        "    def __getattr__(self, name):\n"
        "        raise SystemExit('lookup exit')\n"
        "\n"
        "\n"
        "LAZY = _ExitOnLookup()\n",
        encoding="utf-8",
    )

    [skipped] = _publish(Config(plugins=["./plugins/bad"]), _minimal_runtime_paths(tmp_path)).skipped

    assert isinstance(skipped.error, plugin_module.PluginValidationError)
    assert str(skipped.error) == f"Plugin materialization failed for {plugin_root.resolve()}: lookup exit"


def test_plugin_loading_propagates_keyboard_interrupt_during_plugin_module_execution(tmp_path: Path) -> None:
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

    with pytest.raises(KeyboardInterrupt):
        _loaded(config, runtime_paths)


def test_plugin_loading_rejects_duplicate_manifest_names_before_materialization(tmp_path: Path) -> None:
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

    with pytest.raises(ValueError, match="Duplicate plugin manifest names configured"):
        _bind_runtime_paths(Config(plugins=["./plugins/first", "./plugins/second"]), config_path)

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


def test_plugin_loading_discovers_hooks_from_tools_module_when_hooks_module_missing(tmp_path: Path) -> None:
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

    registry = _load(config, runtime_paths_for(config)).hooks

    assert [hook.hook_name for hook in registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["audit"]


def test_plugin_loading_discovers_hooks_from_dedicated_hooks_module(tmp_path: Path) -> None:
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

    registry = _load(config, runtime_paths_for(config)).hooks

    assert [hook.hook_name for hook in registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["from-hooks-module"]


def _nested_module_plugins(tmp_path: Path, capability: str) -> tuple[Config, RuntimePaths, Path]:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    _write_working_tool_plugin(inner, plugin_name="inner", tool_name="nested_root_tool")
    (outer / "mindroom.plugin.json").write_text(
        json.dumps({"name": "outer", "tools_module": "inner/tools.py"}),
        encoding="utf-8",
    )
    (inner / "mindroom.plugin.json").write_text(
        json.dumps({"name": "inner", f"{capability}_module": "tools.py"}),
        encoding="utf-8",
    )
    module_path = inner / "tools.py"
    module_path.write_text(
        module_path.read_text(encoding="utf-8")
        + "\ndef register_oauth_providers(settings, runtime_paths):\n    return ()\n",
        encoding="utf-8",
    )
    return Config(plugins=[str(outer), str(inner)]), _minimal_runtime_paths(tmp_path), module_path


@pytest.mark.parametrize("capability", ["hooks", "oauth"])
def test_nested_plugin_roots_preserve_each_module_owner(tmp_path: Path, capability: str) -> None:
    """Loading a shared file through another root must retain the active tool owner."""
    config, runtime_paths, _module_path = _nested_module_plugins(tmp_path, capability)
    plugins = publish_test_plugins(config, runtime_paths)
    if capability == "oauth":
        oauth_provider_registry(plugins, skip_broken_plugins=False)
    assert (
        get_tool_by_name("nested_root_tool", runtime_paths, worker_target=None, tools=plugins.tools).name == "working"
    )
    assert plugins.tools.factory("nested_root_tool").__module__ in sys.modules


@pytest.mark.parametrize("capability", ["hooks", "oauth"])
def test_failed_nested_plugin_module_reload_preserves_each_owner_s_installed_module(
    tmp_path: Path,
    capability: str,
) -> None:
    """A worker refresh whose shared file now fails keeps each owner's installed module and drops the tool."""
    config, runtime_paths, module_path = _nested_module_plugins(tmp_path, capability)
    module_path = module_path.resolve()
    module_names = (
        plugin_module._module_name("outer", module_path.parent.parent, module_path),
        plugin_module._module_name("inner", module_path.parent, module_path),
    )
    assert "nested_root_tool" in plugins_module.refresh_plugins(config, runtime_paths).tools
    installed = [sys.modules[module_name] for module_name in module_names]

    healthy_source = module_path.read_text(encoding="utf-8")
    module_path.write_text(healthy_source + "\nraise RuntimeError('reload failed')\n", encoding="utf-8")
    _bump_mtime(module_path)
    refreshed = plugins_module.refresh_plugins(config, runtime_paths)

    assert "nested_root_tool" not in refreshed.tools
    assert [sys.modules[module_name] for module_name in module_names] == installed

    module_path.write_text(healthy_source, encoding="utf-8")
    _bump_mtime(module_path)
    assert "nested_root_tool" in plugins_module.refresh_plugins(config, runtime_paths).tools


def test_nested_plugin_roots_reuse_each_owners_module_across_loads(tmp_path: Path) -> None:
    """A worker loading one file under two nested plugin roots must not run it again on every request."""
    config, runtime_paths, module_path = _nested_module_plugins(tmp_path, "hooks")
    module_path = module_path.resolve()
    module_names = (
        plugin_module._module_name("outer", module_path.parent.parent, module_path),
        plugin_module._module_name("inner", module_path.parent, module_path),
    )

    plugins_module.refresh_plugins(config, runtime_paths)
    first_modules = [sys.modules[module_name] for module_name in module_names]

    plugins_module.refresh_plugins(config, runtime_paths)

    assert all(sys.modules[name] is module for name, module in zip(module_names, first_modules, strict=True))


def test_plugin_loading_reuses_same_module_when_tools_and_hooks_share_file(tmp_path: Path) -> None:
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
    runtime_paths = _runtime_paths(config_path)
    config = Config(plugins=[{"path": "./plugins/same-file"}])

    plugins = _loaded(config, runtime_paths)

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

    initial = _load(config, runtime_paths_for(config)).hooks
    assert await initial.hooks_for(EVENT_MESSAGE_RECEIVED)[0].callback(None) == "before"

    (plugin_root / "helper.py").write_text("VALUE = 'after'\n", encoding="utf-8")
    reloaded = _load(config, runtime_paths_for(config)).hooks

    assert await reloaded.hooks_for(EVENT_MESSAGE_RECEIVED)[0].callback(None) == "after"


def test_a_plugin_publication_refreshes_oauth_providers(tmp_path: Path) -> None:
    """Plugin reloads should refresh OAuth providers loaded through the registry."""
    plugin_root = tmp_path / "plugins" / "oauth-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "oauth-reload", "oauth_module": "oauth_provider.py", "skills": []}),
        encoding="utf-8",
    )
    display_path = plugin_root / "display.txt"
    display_path.write_text("before\n", encoding="utf-8")
    (plugin_root / "oauth_provider.py").write_text(
        "from pathlib import Path\n"
        "from mindroom.oauth import OAuthProvider\n"
        "\n"
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    del settings, runtime_paths\n"
        "    display_name = Path(__file__).with_name('display.txt').read_text(encoding='utf-8').strip()\n"
        "    return [OAuthProvider(\n"
        "        id='plugin_oauth_reload',\n"
        "        display_name=display_name,\n"
        "        authorization_url='https://auth.example.test/authorize',\n"
        "        token_url='https://auth.example.test/token',\n"
        "        scopes=('plugin.read',),\n"
        "        credential_service='plugin_reload_oauth',\n"
        "        client_config_services=('plugin_oauth_reload_oauth_client',),\n"
        "    )]\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/oauth-reload"]), config_path)
    runtime_paths = runtime_paths_for(config)

    _publish(config, runtime_paths)
    initial = config_plugins(config, runtime_paths).oauth_providers
    assert initial["plugin_oauth_reload"].display_name == "before"

    display_path.write_text("after\n", encoding="utf-8")
    _publish(config, runtime_paths)
    reloaded = config_plugins(config, runtime_paths).oauth_providers

    assert reloaded["plugin_oauth_reload"].display_name == "after"


def test_oauth_providers_isolate_system_exit_from_plugin_callback(tmp_path: Path) -> None:
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
    config = _bind_runtime_paths(Config(plugins=["./plugins/bad-oauth"]), config_path)
    runtime_paths = runtime_paths_for(config)

    _publish(config, runtime_paths)
    baseline_providers = config_plugins(baseline_config, runtime_paths).oauth_providers
    providers = config_plugins(config, runtime_paths).oauth_providers
    assert providers == baseline_providers
    with pytest.raises(plugin_module.PluginValidationError, match="Plugin OAuth provider registration failed"):
        oauth_provider_registry(config_plugins(config, runtime_paths), skip_broken_plugins=False)


def test_oauth_providers_propagate_keyboard_interrupt_from_plugin_callback(tmp_path: Path) -> None:
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
    config = _bind_runtime_paths(Config(plugins=["./plugins/bad-oauth"]), config_path)
    runtime_paths = runtime_paths_for(config)

    _publish(config, runtime_paths)
    with pytest.raises(KeyboardInterrupt):
        _ = config_plugins(config, runtime_paths).oauth_providers


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

    shared_task = asyncio.create_task(asyncio.Event().wait())
    extra_task = asyncio.create_task(asyncio.Event().wait())
    try:
        _publish(config, runtime_paths)
        hooks_path = (plugin_root / "hooks.py").resolve()
        hooks_module = cached_plugin_module(hooks_path)
        helper_module = sys.modules[f"{hooks_module.__name__.rsplit('.', 1)[0]}.helper"]
        hooks_module._AUTO_POKE_TASK = shared_task
        hooks_module._snooze_tasks = {"shared": shared_task, "extra": extra_task}
        helper_module._AUTO_POKE_TASK = shared_task

        result = _publish(config, runtime_paths)
        await asyncio.sleep(0)

        assert result.cancelled_task_count == 2
        assert shared_task.cancelled()
        assert extra_task.cancelled()
    finally:
        await asyncio.gather(shared_task, extra_task, return_exceptions=True)


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

    task = asyncio.create_task(asyncio.Event().wait())
    try:
        initial = _publish(config_with_plugin, runtime_paths)
        assert initial.active_plugin_names == ("removed-task-plugin",)

        hooks_path = (plugin_root / "hooks.py").resolve()
        hooks_module = cached_plugin_module(hooks_path)
        hooks_module._AUTO_POKE_TASK = task

        removed = _publish(config_without_plugin, runtime_paths)
        await asyncio.sleep(0)

        assert removed.active_plugin_names == ()
        assert removed.cancelled_task_count == 1
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _write_task_hooks_plugin(plugin_root: Path, *, plugin_name: str) -> Path:
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": plugin_name, "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = (plugin_root / "hooks.py").resolve()
    hooks_path.write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "VERSION = 1\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n",
        encoding="utf-8",
    )
    return hooks_path


def _set_hooks_version(hooks_path: Path, version: int) -> None:
    """Edit the hooks file to a new version whose modification time is strictly later than the previous version's."""
    previous_mtime = hooks_path.stat().st_mtime
    source = hooks_path.read_text(encoding="utf-8")
    hooks_path.write_text(re.sub(r"VERSION = \d+", f"VERSION = {version}", source), encoding="utf-8")
    # The write resets the time to now, which can equal a previous bump within one timestamp tick.
    mtime = previous_mtime + 10
    os.utime(hooks_path, (mtime, mtime))


@pytest.mark.asyncio
async def test_an_edit_waits_for_its_reload_which_cancels_the_edited_module_s_task(tmp_path: Path) -> None:
    """An edited plugin file changes nothing a config uses until a reload, which cancels the replaced module's task."""
    hooks_path = _write_task_hooks_plugin(tmp_path / "plugins" / "edited-task-plugin", plugin_name="edited-task-plugin")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/edited-task-plugin"]), config_path)
    runtime_paths = runtime_paths_for(config)

    task = asyncio.create_task(asyncio.Event().wait())
    try:
        _publish(config, runtime_paths)
        first_module = cached_plugin_module(hooks_path)
        first_module._AUTO_POKE_TASK = task
        _set_hooks_version(hooks_path, 2)

        assert config_plugins(config, runtime_paths).generation.modules[first_module.__name__] is first_module
        assert cached_plugin_module(hooks_path) is first_module

        result = _publish(config, runtime_paths)
        await asyncio.sleep(0)

        assert result.cancelled_task_count == 1
        assert task.cancelled()
        assert cached_plugin_module(hooks_path).VERSION == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_worker_refresh_of_an_edited_plugin_cancels_the_replaced_module_s_task(tmp_path: Path) -> None:
    """A worker request that loads an edited plugin file ends the tasks the module it replaces holds."""
    hooks_path = _write_task_hooks_plugin(tmp_path / "plugins" / "refreshed-task", plugin_name="refreshed-task")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/refreshed-task"]), config_path)
    runtime_paths = runtime_paths_for(config)

    task = asyncio.create_task(asyncio.Event().wait())
    try:
        plugins_module.refresh_plugins(config, runtime_paths)
        cached_plugin_module(hooks_path)._AUTO_POKE_TASK = task
        _set_hooks_version(hooks_path, 2)

        plugins_module.refresh_plugins(config, runtime_paths)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert cached_plugin_module(hooks_path).VERSION == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_worker_refresh_refused_as_a_whole_keeps_the_published_modules_and_ends_new_tasks(
    tmp_path: Path,
) -> None:
    """A worker refresh refused because two plugins register one tool restores the published modules."""
    probe = ModuleType(f"mindroom_test_probe_{uuid.uuid4().hex}")
    probe.tasks = []
    sys.modules[probe.__name__] = probe
    for name, starts_task in (("first", False), ("second", True)):
        plugin_root = tmp_path / "plugins" / name
        plugin_root.mkdir(parents=True)
        (plugin_root / "mindroom.plugin.json").write_text(
            json.dumps({"name": f"{name}_plugin", "tools_module": "tools.py", "skills": []}),
            encoding="utf-8",
        )
        (plugin_root / "tools.py").write_text(
            "import asyncio\n"
            f"import {probe.__name__} as probe\n"
            "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\n"
            "from mindroom.tool_system.registration import register_tool_with_metadata\n"
            "\n"
            + (
                "_POLLER = asyncio.get_running_loop().create_task(asyncio.Event().wait())\nprobe.tasks.append(_POLLER)\n"
                if starts_task
                else ""
            )
            + "\n"
            "@register_tool_with_metadata(\n"
            "    name='shared_tool',\n"
            "    file_access=ToolFileAccess.NONE,\n"
            "    display_name='Shared',\n"
            "    description='Conflicts',\n"
            "    category=ToolCategory.DEVELOPMENT,\n"
            ")\n"
            "def shared_tools():\n"
            "    return object\n",
            encoding="utf-8",
        )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    first = _bind_runtime_paths(Config(plugins=["./plugins/first"]), config_path)
    runtime_paths = runtime_paths_for(first)

    try:
        plugins_module.refresh_plugins(first, runtime_paths)
        published = plugins_module._canonical_plugin_modules()

        with pytest.raises(ToolMetadataValidationError, match="conflicts between plugins"):
            plugins_module.refresh_plugins(Config(plugins=["./plugins/first", "./plugins/second"]), runtime_paths)
        await asyncio.sleep(0)

        [task] = probe.tasks
        assert task.cancelled()
        assert plugins_module._canonical_plugin_modules() == published
        assert "shared_tool" in plugins_module.refresh_plugins(first, runtime_paths).tools
    finally:
        for task in probe.tasks:
            task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)
        sys.modules.pop(probe.__name__, None)


def _write_import_task_plugin(plugin_root: Path) -> None:
    """Write one plugin whose hooks module starts a task at import and keeps it in a module global."""
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "import-task", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text(
        "import asyncio\n"
        "from mindroom.plugin_api import hook\n"
        "\n"
        "_BACKGROUND_TASK = asyncio.get_running_loop().create_task(asyncio.Event().wait())\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("package", [False, True], ids=["directory", "package"])
async def test_a_plugin_module_that_fails_after_starting_a_task_leaves_no_task_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package: bool,
) -> None:
    """Validating or loading a plugin whose module raises after starting a task ends that task too."""
    probe = ModuleType(f"mindroom_test_probe_{uuid.uuid4().hex}")
    probe.tasks = []
    sys.modules[probe.__name__] = probe
    if package:
        # Its hooks module loads under the package's name.
        entry = f"fails_pkg_{uuid.uuid4().hex}"
        plugin_root = tmp_path / "site-packages" / entry
        plugin_root.mkdir(parents=True)
        (plugin_root / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.syspath_prepend(str(plugin_root.parent))
    else:
        entry = "./plugins/fails-after-task"
        plugin_root = tmp_path / "plugins" / "fails-after-task"
        plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "fails-after-task", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=[entry]), config_path)
    hooks_path.write_text(
        "import asyncio\n"
        f"import {probe.__name__} as probe\n"
        "\n"
        "_POLLER = asyncio.get_running_loop().create_task(asyncio.Event().wait())\n"
        "probe.tasks.append(_POLLER)\n"
        "raise RuntimeError('setup failed after the poller started')\n",
        encoding="utf-8",
    )
    os.utime(hooks_path, ns=(1, 1))

    generation = None
    try:
        with pytest.raises(ConfigRuntimeValidationError, match="setup failed after the poller started"):
            _bind_runtime_paths(Config(plugins=[entry]), config_path)
        generation = prepare_plugins(config, runtime_paths_for(config))
        [skipped] = generation.skipped
        assert "setup failed after the poller started" in str(skipped.error)
        await asyncio.sleep(0)
        validated, loaded = probe.tasks
        assert validated.cancelled()
        assert loaded.cancelled()
    finally:
        if generation is not None:
            dispose_plugins(generation)
        for task in probe.tasks:
            task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_staged_plugin_reload_cancels_only_the_live_generation_tasks_on_commit(tmp_path: Path) -> None:
    """Staging leaves the live plugin tasks running; a discarded stage ends its own; publishing ends the live ones."""
    plugin_root = tmp_path / "plugins" / "import-task"
    _write_import_task_plugin(plugin_root)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/import-task"]), config_path)
    edited_config = _bind_runtime_paths(
        Config(plugins=[{"path": "./plugins/import-task", "settings": {"interval": 5}}]),
        config_path,
    )
    runtime_paths = runtime_paths_for(config)
    hooks_path = (plugin_root / "hooks.py").resolve()

    tasks: list[asyncio.Task[None]] = []
    try:
        _publish(config, runtime_paths)
        module_name = cached_plugin_module(hooks_path).__name__
        live_task = cached_plugin_module(hooks_path)._BACKGROUND_TASK
        tasks.append(live_task)

        # A config reload that fails after staging never publishes, so the live plugin must keep its task.
        discarded = prepare_plugins(edited_config, runtime_paths)
        discarded_task = discarded.module_cache[module_name].module._BACKGROUND_TASK
        tasks.append(discarded_task)
        dispose_plugins(discarded)
        await asyncio.sleep(0)
        assert not live_task.done()
        assert discarded_task.cancelled()

        prepared = prepare_plugins(edited_config, runtime_paths)
        staged_task = prepared.module_cache[module_name].module._BACKGROUND_TASK
        tasks.append(staged_task)
        result = publish_plugins(prepared)
        await asyncio.sleep(0)

        assert cached_plugin_module(hooks_path)._BACKGROUND_TASK is staged_task
        assert result.cancelled_task_count == 1
        assert live_task.cancelled()
        assert not staged_task.done()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_a_reload_skips_a_plugin_that_became_invalid(tmp_path: Path) -> None:
    """An explicit reload skips and reports a configured plugin that broke, and publishes the rest."""
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

    initial = _publish(config, runtime_paths_for(config))
    assert initial.active_plugin_names == ("broken-reload",)

    hooks_path.unlink()
    result = _publish(config, runtime_paths_for(config))

    assert result.active_plugin_names == ()
    [skipped] = result.skipped
    assert skipped.path == "./plugins/broken-reload"
    assert "Plugin hooks module not found" in str(skipped.error)


def test_a_registry_captured_before_a_reload_keeps_the_tool_the_reload_skips(tmp_path: Path) -> None:
    """A reload skips a plugin whose tools module broke; registries captured before it still build its tool."""
    plugin_root = tmp_path / "plugins" / "reload-tool"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "reload_tool", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        _tool_plugin_source("reload_plugin_tool", toolkit_name="reload"),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/reload-tool"]), config_path)
    runtime_paths = runtime_paths_for(config)

    initial = _publish(config, runtime_paths)
    assert initial.active_plugin_names == ("reload_tool",)
    captured = _tools(config)
    assert get_tool_by_name("reload_plugin_tool", runtime_paths, worker_target=None, tools=captured).name == "reload"

    tools_path.write_text("raise ImportError('reload failure')\n", encoding="utf-8")
    _bump_mtime(tools_path)
    result = _publish(config, runtime_paths)

    assert "reload failure" in str(result.skipped[0].error)
    assert "reload_plugin_tool" not in _tools(config)
    assert get_tool_by_name("reload_plugin_tool", runtime_paths, worker_target=None, tools=captured).name == "reload"


def test_reload_plugins_skip_broken_plugins_keeps_healthy_plugin_when_explicit_package_spec_breaks(
    tmp_path: Path,
) -> None:
    """Reloads skip broken explicit package specs and keep healthy plugins active."""
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
    config = Config(plugins=["./plugins/good", "python:definitely_missing_package.sub"])

    result = _publish(config, runtime_paths)
    hooks = config_plugins(config, runtime_paths).hooks

    assert result.active_plugin_names == ("good",)
    assert [plugin.path for plugin in result.skipped] == ["python:definitely_missing_package.sub"]
    assert [hook.plugin_name for hook in hooks.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["good"]


def _write_skill_plugin(plugin_root: Path, *, description: str = "Demo skill") -> Path:
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "skill-plugin", "skills": ["skills"]}),
        encoding="utf-8",
    )
    skill_dir = plugin_root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        f"---\nname: demo-skill\ndescription: {description}\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    return skill_path


def test_repeated_plugin_loading_with_unchanged_roots_keeps_skill_cache(tmp_path: Path) -> None:
    """Warm per-agent plugin loads must not rescan unchanged plugin skill roots."""
    plugin_root = tmp_path / "plugins" / "skill-plugin"
    _write_skill_plugin(plugin_root)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/skill-plugin"]), config_path)
    skills_root = (plugin_root / "skills").resolve()

    _loaded(config, runtime_paths_for(config))
    first_load = skills_module._load_root_skills(skills_root)
    assert [skill.name for skill in first_load] == ["demo-skill"]

    _loaded(config, runtime_paths_for(config))

    assert skills_module._load_root_skills(skills_root) is first_load
    skills_module.clear_skill_cache()


def test_reload_plugins_picks_up_skill_file_changes(tmp_path: Path) -> None:
    """A plugin reload with unchanged roots must still surface edited skill files."""
    plugin_root = tmp_path / "plugins" / "skill-plugin"
    skill_path = _write_skill_plugin(plugin_root, description="Demo v1")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/skill-plugin"]), config_path)
    skills_root = (plugin_root / "skills").resolve()

    _loaded(config, runtime_paths_for(config))
    first_load = skills_module._load_root_skills(skills_root)
    assert first_load[0].description == "Demo v1"

    old_mtime = skill_path.stat().st_mtime_ns
    _write_skill_plugin(plugin_root, description="Demo v2")
    os.utime(skill_path, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))
    _publish(config, runtime_paths_for(config))

    refreshed = skills_module._load_root_skills(skills_root)
    assert refreshed[0].description == "Demo v2"
    skills_module.clear_skill_cache()


def test_a_config_without_a_plugin_offers_none_of_its_skills(tmp_path: Path) -> None:
    """Dropping a plugin drops its skill roots from every config that no longer lists it."""
    plugin_root = tmp_path / "plugins" / "skill-plugin"
    _write_skill_plugin(plugin_root)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/skill-plugin"]), config_path)
    config_without_plugin = _bind_runtime_paths(Config(plugins=[]), config_path)

    assert _load(config, runtime_paths_for(config)).skill_roots == ((plugin_root / "skills").resolve(),)
    assert config_plugins(config_without_plugin, runtime_paths_for(config_without_plugin)).skill_roots == ()
