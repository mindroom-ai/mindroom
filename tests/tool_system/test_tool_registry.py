"""The tools one config can build stay that config's, whatever other configs and plugin publications a process sees."""

from __future__ import annotations

import importlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.tool_system import plugin_imports
from mindroom.tool_system import plugins as plugins_module
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import candidate_tool_registry, config_plugins, refresh_plugins
from tests.conftest import runtime_paths_for
from tests.test_plugins import (
    _bind_runtime_paths,
    _bump_mtime,
    _forgetting_modules,
    _publish,
    _tool_plugin_source,
    _write_broken_tool_plugin,
    _write_package_helper_plugin,
    _write_working_tool_plugin,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.plugins import ConfigPlugins


def test_a_config_without_a_plugin_leaves_another_config_s_plugin_tools_buildable(tmp_path: Path) -> None:
    """While the API already serves a config that removed plugin ``p``, a bot still on the old config builds ``p``'s tool.

    A config reload publishes the new config to the API before running replies drain, so the MCP gateway and the
    dashboard resolve tools for the new config while bots keep building for the old one.
    """
    _write_working_tool_plugin(tmp_path / "plugins" / "p", plugin_name="p", tool_name="p_tool")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    old = _bind_runtime_paths(
        Config(plugins=["./plugins/p"], mcp_servers={"srv": {"transport": "stdio", "command": "srv"}}),
        config_path,
    )
    new = _bind_runtime_paths(Config(plugins=[]), config_path)
    runtime_paths = runtime_paths_for(old)

    _publish(old, runtime_paths)
    new_tools = config_plugins(new, runtime_paths).tools
    old_tools = config_plugins(old, runtime_paths).tools

    toolkit = get_tool_by_name("p_tool", runtime_paths, tools=old_tools, runtime_config=old, worker_target=None)

    assert toolkit.name == "working"
    assert "p_tool" not in new_tools
    assert new_tools.mcp_server_id("mcp_srv") is None
    assert old_tools.mcp_server_id("mcp_srv") == "srv"


def _write_named_tool_plugin(plugin_root: Path, toolkit_name: str) -> None:
    """Write plugin ``p`` whose tool ``p_tool`` builds a toolkit named ``toolkit_name``."""
    if not plugin_root.exists():
        _write_working_tool_plugin(plugin_root, plugin_name="p", tool_name="p_tool")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(_tool_plugin_source("p_tool", toolkit_name=toolkit_name), encoding="utf-8")
    _bump_mtime(tools_path)


def _built_name(plugins: ConfigPlugins, config: Config) -> str:
    return get_tool_by_name(
        "p_tool",
        plugins.runtime_paths,
        tools=plugins.tools,
        runtime_config=config,
        worker_target=None,
    ).name


def test_a_config_object_sees_each_plugin_publication_and_a_capture_keeps_its_own(tmp_path: Path) -> None:
    """A config object a no-op save kept serves a later plugin-only reload; an earlier capture keeps its generation."""
    plugin_root = tmp_path / "plugins" / "p"
    _write_named_tool_plugin(plugin_root, "first")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/p"]), config_path)
    runtime_paths = runtime_paths_for(config)

    _publish(config, runtime_paths)
    before = config_plugins(config, runtime_paths)
    _write_named_tool_plugin(plugin_root, "second")
    _publish(config, runtime_paths)
    after = config_plugins(config, runtime_paths)

    assert _built_name(after, config) == "second"
    assert _built_name(before, config) == "first"


def test_mcp_only_edits_around_a_plugin_reload_build_the_reloaded_plugin(tmp_path: Path) -> None:
    """Configs that changed only MCP servers before or after a plugin reload all build the reloaded plugin's tool."""
    plugin_root = tmp_path / "plugins" / "p"
    _write_named_tool_plugin(plugin_root, "first")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    def config_with(server: str) -> Config:
        servers = {server: {"transport": "stdio", "command": server}}
        return _bind_runtime_paths(Config(plugins=["./plugins/p"], mcp_servers=servers), config_path)

    original = config_with("one")
    runtime_paths = runtime_paths_for(original)
    _publish(original, runtime_paths)
    edited_before = config_with("two")
    assert _built_name(config_plugins(edited_before, runtime_paths), edited_before) == "first"
    _write_named_tool_plugin(plugin_root, "second")
    _publish(edited_before, runtime_paths)
    edited_after = config_with("three")

    for config, server in ((original, "one"), (edited_before, "two"), (edited_after, "three")):
        plugins = config_plugins(config, runtime_paths)
        assert _built_name(plugins, config) == "second"
        assert plugins.tools.mcp_server_id(f"mcp_{server}") == server


def test_an_oauth_provider_lookup_runs_no_plugin_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider lookups call the loaded plugin's callback with each config's settings and never import plugin code."""
    plugin_root = tmp_path / "plugins" / "oauth"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "oauth", "oauth_module": "oauth_provider.py", "skills": []}),
        encoding="utf-8",
    )
    imports_path = tmp_path / "imports.log"
    (plugin_root / "oauth_provider.py").write_text(
        "from pathlib import Path\n"
        "from mindroom.oauth import OAuthProvider\n"
        f"with Path({str(imports_path)!r}).open('a') as log:\n"
        "    log.write('import\\n')\n"
        "\n"
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    del runtime_paths\n"
        "    return [OAuthProvider(\n"
        "        id='plugin_oauth',\n"
        "        display_name=settings.get('label', 'Plugin'),\n"
        "        authorization_url='https://auth.example.test/authorize',\n"
        "        token_url='https://auth.example.test/token',\n"
        "        scopes=('read',),\n"
        "        credential_service='plugin_oauth',\n"
        "        client_config_services=('plugin_oauth_client',),\n"
        "    )]\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")

    def config_with(label: str) -> Config:
        return _bind_runtime_paths(
            Config(plugins=[{"path": "./plugins/oauth", "settings": {"label": label}}]),
            config_path,
        )

    first = config_with("First")
    runtime_paths = runtime_paths_for(first)
    _publish(first, runtime_paths)

    def no_plugin_loading(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An OAuth provider lookup loaded plugin code")

    monkeypatch.setattr(plugins_module, "_load_plugin_module", no_plugin_loading)
    monkeypatch.setattr(plugins_module, "_load_plugins", no_plugin_loading)
    second = config_with("Second")
    labels = [
        config_plugins(config, runtime_paths).oauth_providers["plugin_oauth"].display_name for config in (first, second)
    ]

    assert labels == ["First", "Second"]
    # The first config's validation and its publication executed the module; no lookup did.
    assert imports_path.read_text(encoding="utf-8").splitlines() == ["import", "import"]


def test_a_rejected_plugin_validates_once_its_source_is_repaired(tmp_path: Path) -> None:
    """Saving the identical config again after repairing the plugin it rejected validates against the repaired source."""
    plugin_root = tmp_path / "plugins" / "broken"
    _write_broken_tool_plugin(plugin_root, tool_name="repaired_tool")
    runtime_paths = runtime_paths_for(_bind_runtime_paths(Config(), tmp_path / "config.yaml"))
    data = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "plugins": ["./plugins/broken"],
        "agents": {"code": {"display_name": "Code", "tools": ["repaired_tool"]}},
    }

    with pytest.raises(ConfigRuntimeValidationError, match="missing optional plugin dependency"):
        Config.validate_with_runtime(data, runtime_paths)
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        tools_path.read_text().replace("raise ImportError", "# raise ImportError"),
        encoding="utf-8",
    )

    config = Config.validate_with_runtime(data, runtime_paths)

    assert config.agents["code"].tool_names == ["repaired_tool"]


def test_validating_a_candidate_never_disturbs_live_modules_another_thread_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a changed plugin validates, a live sibling another thread imports is the live one and stays installed."""
    plugin_root = tmp_path / "plugins" / "p"
    _write_working_tool_plugin(plugin_root, plugin_name="p", tool_name="p_tool")
    (plugin_root / "helpers.py").write_text("SOURCE = 'live'\n", encoding="utf-8")
    (plugin_root / "lazy.py").write_text("SOURCE = 'live'\n", encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "import sys\nfrom . import helpers\nsys.modules['mindroom_test_candidate_probe'].entered()\n"
        + tools_path.read_text(),
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()
    probe = ModuleType("mindroom_test_candidate_probe")
    probe.entered = lambda: None
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/p"]), config_path)
    runtime_paths = runtime_paths_for(config)
    root = plugin_root.resolve()
    helpers_name = plugin_imports._module_name("p", root, root / "helpers.py")
    lazy_name = plugin_imports._module_name("p", root, root / "lazy.py")

    def block() -> None:
        entered.set()
        assert release.wait(10)

    _publish(config, runtime_paths)
    live_helpers = sys.modules[helpers_name]
    (plugin_root / "helpers.py").write_text("SOURCE = 'candidate'\n", encoding="utf-8")
    _bump_mtime(tools_path)
    probe.entered = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        validation = pool.submit(candidate_tool_registry, config, runtime_paths)
        assert entered.wait(10)
        live_lazy = importlib.import_module(lazy_name)
        seen = sys.modules[helpers_name]
        release.set()
        tools, skipped = validation.result(timeout=10)

    assert seen is live_helpers
    assert live_helpers.SOURCE == "live"
    assert sys.modules[lazy_name] is live_lazy
    assert sys.modules[helpers_name] is live_helpers
    assert not skipped
    assert "p_tool" in tools


class _SignalingLock:
    """Wrap the plugin lock, setting ``waiting`` whenever a thread must wait for another thread to release it."""

    def __init__(self, lock: threading.RLock, waiting: threading.Event) -> None:
        self._lock = lock
        self._waiting = waiting

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._waiting.set()
            self._lock.acquire()

    def __exit__(self, *_exc_info: object) -> None:
        self._lock.release()


@pytest.mark.parametrize(("first", "second"), [("check", "load"), ("load", "check")])
def test_a_candidate_check_and_a_live_load_of_a_package_plugin_never_interleave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
) -> None:
    """Whichever starts first, the live load keeps the helper module the plugin's tools module imports by absolute name.

    The check reports the tools that helper module registers.
    """
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(
        "import sys\nsys.modules['mindroom_test_candidate_probe'].entered()\n" + tools_path.read_text(),
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()
    # Set once the second operation either finished or waits for the first one to finish.
    settled = threading.Event()

    def block_the_first_execution() -> None:
        if not entered.is_set():
            entered.set()
            assert release.wait(10)

    probe = ModuleType("mindroom_test_candidate_probe")
    probe.entered = block_the_first_execution
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    monkeypatch.setattr(plugins_module, "_PLUGIN_LOCK", _SignalingLock(plugins_module._PLUGIN_LOCK, settled))
    operations = {"check": candidate_tool_registry, "load": refresh_plugins}

    def run_second() -> object:
        result = operations[second](config, runtime_paths)
        settled.set()
        return result

    with _forgetting_modules("demo_pkg"), ThreadPoolExecutor(max_workers=2) as pool:
        first_run = pool.submit(operations[first], config, runtime_paths)
        assert entered.wait(10)
        second_run = pool.submit(run_second)
        assert settled.wait(10)
        release.set()
        results = {first: first_run.result(timeout=10), second: second_run.result(timeout=10)}
        installed_helpers = sys.modules.get("demo_pkg.helpers")

    tools, skipped = results["check"]
    assert installed_helpers is plugins_module._PUBLISHED.modules["demo_pkg.helpers"]
    assert not skipped
    assert "helper_tool" in tools


def _helpers_source(*tool_names: str) -> str:
    """Return a helper module registering ``tool_names``, whose ``helper_tools`` a tools module imports."""
    return "".join(_tool_plugin_source(name) for name in tool_names) + "helper_tools = demo_plugin_tools\n"


def _validate_package_plugin_tools(tmp_path: Path, tool_names: list[str]) -> Config:
    """Validate a config whose agent uses ``tool_names`` from the package plugin ``demo_pkg``, as a dashboard save does."""
    data = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "plugins": ["demo_pkg"],
        "agents": {"code": {"display_name": "Code", "tools": tool_names}},
    }
    return Config.validate_with_runtime(
        data,
        runtime_paths_for(_bind_runtime_paths(Config(), tmp_path / "config.yaml")),
    )


def _edit_helpers(plugin_root: Path, *tool_names: str) -> None:
    """Rewrite the plugin's helper module to register ``tool_names``, and edit its tools module, as a plugin edit does."""
    (plugin_root / "helpers.py").write_text(_helpers_source(*tool_names), encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text(f"{tools_path.read_text(encoding='utf-8')}# edited\n", encoding="utf-8")
    _bump_mtime(tools_path)


@pytest.mark.parametrize(
    ("files", "tools_source"),
    [
        ({}, "from demo_pkg.helpers import helper_tools\n"),
        ({}, "from demo_pkg import helpers\n"),
        ({}, "import demo_pkg.helpers\n"),
        ({"__init__.py": "from demo_pkg.helpers import helper_tools\n"}, "from demo_pkg import helper_tools\n"),
        ({"relay.py": "import demo_pkg.helpers\n"}, "from . import relay\n"),
        ({"__init__.py": None}, "from demo_pkg.helpers import helper_tools\n"),
        (
            {
                "data.txt": "data",
                "__init__.py": "from importlib.resources import files\n"
                "DATA = files(__package__).joinpath('data.txt').read_text()\n"
                "from demo_pkg.helpers import helper_tools\n",
            },
            "from demo_pkg import helper_tools\n",
        ),
    ],
    ids=[
        "module",
        "package-fromlist",
        "package-attribute",
        "package-reexport",
        "sibling",
        "namespace-package",
        "package-data",
    ],
)
def test_an_edited_package_plugin_validates_with_the_tool_its_helper_module_adds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str | None],
    tools_source: str,
) -> None:
    """Before the edited plugin reloads, validation sees each tool its helper module registers, and publishes none."""
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    for name, source in files.items():
        if source is None:
            (plugin_root / name).unlink()
        else:
            (plugin_root / name).write_text(source, encoding="utf-8")
    (plugin_root / "tools.py").write_text(tools_source, encoding="utf-8")

    # Binding the config imports a namespace package, so the block starts before it.
    with _forgetting_modules("demo_pkg"):
        config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
        runtime_paths = runtime_paths_for(config)
        _publish(config, runtime_paths)
        live_helpers = sys.modules["demo_pkg.helpers"]
        _edit_helpers(plugin_root, "helper_tool", "added_tool")

        validated = _validate_package_plugin_tools(tmp_path, ["helper_tool", "added_tool"])

        assert validated.agents["code"].tool_names == ["helper_tool", "added_tool"]
        assert sys.modules["demo_pkg.helpers"] is live_helpers
        assert "added_tool" not in config_plugins(config, runtime_paths).tools


def test_an_edited_package_plugin_validates_without_the_tool_its_helper_module_drops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the edited plugin reloads, validation refuses a tool its helper module no longer registers.

    Repeated checks leave one import finder and no candidate package behind.
    """
    plugin_root = _write_package_helper_plugin(tmp_path, monkeypatch)
    (plugin_root / "helpers.py").write_text(_helpers_source("helper_tool", "dropped_tool"), encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["demo_pkg"]), tmp_path / "config.yaml")
    runtime_paths = runtime_paths_for(config)

    with _forgetting_modules("demo_pkg"):
        _publish(config, runtime_paths)
        assert "dropped_tool" in config_plugins(config, runtime_paths).tools
        _edit_helpers(plugin_root, "helper_tool")

        validated = _validate_package_plugin_tools(tmp_path, ["helper_tool"])
        with pytest.raises(ConfigRuntimeValidationError, match="Unknown tool 'dropped_tool'"):
            _validate_package_plugin_tools(tmp_path, ["dropped_tool"])

    assert validated.agents["code"].tool_names == ["helper_tool"]
    assert sys.meta_path.count(plugins_module._CANDIDATE_MODULE_FINDER) == 1
    assert not plugins_module._CANDIDATE_PACKAGES


def test_a_nested_package_plugin_importing_its_helper_through_the_outer_package_still_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``import pack.demo.helpers`` in the edited plugin ``python:pack.demo`` binds the outer package, as it does live."""
    site_packages = tmp_path / "site-packages"
    plugin_root = site_packages / "pack" / "demo"
    plugin_root.mkdir(parents=True)
    (site_packages / "pack" / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "helpers.py").write_text(_helpers_source("helper_tool"), encoding="utf-8")
    tools_path = plugin_root / "tools.py"
    tools_path.write_text("import pack.demo.helpers\nhelper_tools = pack.demo.helpers.helper_tools\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site_packages))

    with _forgetting_modules("pack"):
        config = _bind_runtime_paths(Config(plugins=["python:pack.demo"]), tmp_path / "config.yaml")
        runtime_paths = runtime_paths_for(config)
        _publish(config, runtime_paths)
        tools_path.write_text(f"{tools_path.read_text(encoding='utf-8')}# edited\n", encoding="utf-8")
        _bump_mtime(tools_path)

        _, skipped = candidate_tool_registry(config, runtime_paths)

    assert not skipped
