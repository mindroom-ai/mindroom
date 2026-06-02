"""Tests for per-tool dynamic loading."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest

from mindroom.agents import (
    _build_dynamic_tooling_instruction_block,
    _build_dynamic_tooling_state_suffix,
    get_agent_toolkit_names,
)
from mindroom.config.main import Config
from mindroom.config.models import EffectiveToolConfig, ToolConfigEntry
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.response_runner import _agent_has_matrix_messaging_tool
from mindroom.tool_system import dynamic_toolkits as dynamic_toolkits_module
from mindroom.tool_system.dynamic_toolkits import (
    get_loaded_tools_for_session,
    save_loaded_tools_for_session,
    visible_tool_surface,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _base_config_data() -> dict[str, object]:
    return {
        "defaults": {"tools": []},
        "agents": {
            "code": {
                "display_name": "Code",
                "role": "Write code",
            },
        },
        "models": {
            "default": {
                "provider": "openai",
                "id": "gpt-4o-mini",
            },
        },
    }


@pytest.fixture(autouse=True)
def _clear_loaded_tools_state() -> Generator[None, None, None]:
    dynamic_toolkits_module._loaded_tools.clear()
    yield
    dynamic_toolkits_module._loaded_tools.clear()


def _validated_config(tmp_path: Path, raw: dict[str, object]) -> Config:
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(raw, runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    return config


def _tool_payload(result: str) -> dict[str, object]:
    return json.loads(result)


def _runtime_tool_configs(
    *,
    agent_name: str,
    config: Config,
    loaded_tools: list[str],
    enable_dynamic_tools_manager: bool,
) -> list[EffectiveToolConfig]:
    return list(
        visible_tool_surface(
            agent_name=agent_name,
            config=config,
            loaded_tools=loaded_tools,
            enable_dynamic_tools_manager=enable_dynamic_tools_manager,
        ).runtime_tool_configs,
    )


def test_config_accepts_inline_deferred_tool_flags(tmp_path: Path) -> None:
    """Inline tool entries should carry dynamic loading flags and preserve overrides."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        "shell",
        {"coding": {"defer": True, "initial": True, "restrict_to_base_dir": False}},
        {"name": "searxng", "defer": True, "overrides": {"fixed_max_results": 10}},
    ]

    config = _validated_config(tmp_path, raw)

    entries = config.agents["code"].tools
    assert [(entry.name, entry.defer, entry.initial) for entry in entries] == [
        ("shell", False, False),
        ("coding", True, True),
        ("searxng", True, False),
    ]
    assert entries[1].overrides == {"restrict_to_base_dir": False}
    assert entries[2].overrides == {"fixed_max_results": 10}
    assert config.authored_model_dump()["agents"]["code"]["tools"] == [
        "shell",
        {"coding": {"restrict_to_base_dir": False, "defer": True, "initial": True}},
        {"searxng": {"fixed_max_results": 10, "defer": True}},
    ]


def test_config_rejects_invalid_lazy_flag_locations(tmp_path: Path) -> None:
    """Lazy flags should be valid only on per-agent deferred tool entries."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"shell": {"initial": True}}]  # type: ignore[index]
    with pytest.raises(ValueError, match="initial=true requires defer=true"):
        _validated_config(tmp_path, raw)

    raw = _base_config_data()
    raw["defaults"] = {"tools": [{"shell": {"defer": True}}]}
    with pytest.raises(ValueError, match=r"defaults\.tools does not support defer or initial flags: shell"):
        _validated_config(tmp_path, raw)

    raw = _base_config_data()
    raw["defaults"] = {"tools": [{"shell": {"defer": False}}]}
    with pytest.raises(ValueError, match=r"defaults\.tools does not support defer or initial flags: shell"):
        _validated_config(tmp_path, raw)

    raw = _base_config_data()
    raw["defaults"] = {"tools": [{"shell": {"initial": False}}]}
    with pytest.raises(ValueError, match=r"defaults\.tools does not support defer or initial flags: shell"):
        _validated_config(tmp_path, raw)


@pytest.mark.parametrize("lazy_flag", ["defer", "initial"])
def test_config_rejects_lazy_flags_inside_named_tool_overrides(tmp_path: Path, lazy_flag: str) -> None:
    """Lazy-loading control flags belong at tool-entry level, not inside overrides."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"name": "shell", "overrides": {lazy_flag: True}}]  # type: ignore[index]

    with pytest.raises(
        ValueError,
        match=rf"Tool control flags must be declared at the tool-entry level, not inside overrides: {lazy_flag}",
    ):
        _validated_config(tmp_path, raw)


@pytest.mark.parametrize("tool_name", ["delegate", "dynamic_tools", "self_config"])
def test_config_rejects_deferred_control_plane_tools(tmp_path: Path, tool_name: str) -> None:
    """Control-plane tools are injected by runtime policy and cannot be lazy-loading units."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{tool_name: {"defer": True}}]  # type: ignore[index]
    with pytest.raises(
        ValueError,
        match=(
            rf"agents\.code\.tools: '{tool_name}' is a control-plane tool and cannot be deferred; "
            r"defer/initial are only valid on runtime tools\."
        ),
    ):
        _validated_config(tmp_path, raw)


@pytest.mark.parametrize(
    "lazy_flags",
    [
        {"defer": True},
        {"initial": True},
        {"defer": "true"},
        {"defer": 1},
        {"defer": False},
        {"initial": False},
    ],
)
def test_config_rejects_deferred_tool_presets(tmp_path: Path, lazy_flags: dict[str, object]) -> None:
    """Presets are bundles and must not be dynamic-loading units."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"openclaw_compat": lazy_flags}]  # type: ignore[index]

    with pytest.raises(
        ValueError,
        match=(
            r"agents\.code\.tools: 'openclaw_compat' is a preset and cannot be deferred; "
            r"defer/initial are only valid on individual tools\."
        ),
    ):
        _validated_config(tmp_path, raw)


def test_config_rejects_default_tool_preset_lazy_flag_presence(tmp_path: Path) -> None:
    """Preset lazy-flag presence should be rejected in defaults too."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": [{"openclaw_compat": {"defer": False}}]}

    with pytest.raises(
        ValueError,
        match=(
            r"defaults\.tools: 'openclaw_compat' is a preset and cannot be deferred; "
            r"defer/initial are only valid on individual tools\."
        ),
    ):
        _validated_config(tmp_path, raw)


def test_config_rejects_constructed_deferred_tool_preset(tmp_path: Path) -> None:
    """Post-coercion validation should reject already-normalized preset entries."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [ToolConfigEntry(name="openclaw_compat", defer=True)]  # type: ignore[index]

    with pytest.raises(
        ValueError,
        match=(
            r"agents\.code\.tools: 'openclaw_compat' is a preset and cannot be deferred; "
            r"defer/initial are only valid on individual tools\."
        ),
    ):
        _validated_config(tmp_path, raw)


def test_effective_tool_configs_keep_defer_initial_and_authored_order(tmp_path: Path) -> None:
    """Effective configs should keep lazy flags after default and agent merges."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": ["shell", "sleep"]}
    raw["agents"]["code"]["tools"] = [{"shell": {"defer": True, "initial": True}}]  # type: ignore[index]

    config = _validated_config(tmp_path, raw)

    entries = config.get_agent_tool_configs("code")
    assert [(entry.name, entry.defer, entry.initial, entry.authored_order) for entry in entries] == [
        ("shell", True, True, 0),
        ("sleep", False, False, 1),
    ]


def test_loaded_state_is_agent_and_session_scoped_for_matrix_threads(tmp_path: Path) -> None:
    """Agents and Matrix threads should not share loaded dynamic tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"shell": {"defer": True}}]  # type: ignore[index]
    raw["agents"]["worker"] = {
        "display_name": "Worker",
        "role": "Also writes code",
        "tools": [{"shell": {"defer": True}}],
    }
    config = _validated_config(tmp_path, raw)

    save_loaded_tools_for_session(
        agent_name="code",
        session_id="!room:example.org:$event-a",
        loaded_tools=["shell"],
    )

    assert get_loaded_tools_for_session(
        agent_name="code",
        config=config,
        session_id="!room:example.org:$event-a",
    ) == ["shell"]
    assert (
        get_loaded_tools_for_session(
            agent_name="code",
            config=config,
            session_id="!room:example.org:$event-b",
        )
        == []
    )
    assert (
        get_loaded_tools_for_session(
            agent_name="worker",
            config=config,
            session_id="!room:example.org:$event-a",
        )
        == []
    )
    assert dynamic_toolkits_module._loaded_tools[("code", "!room:example.org:$event-a")] == ["shell"]


def test_loaded_state_is_isolated_by_scope_and_persists_for_same_agent_scope(tmp_path: Path) -> None:
    """One agent should keep dynamic tool state isolated per normalized scope."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"shell": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    save_loaded_tools_for_session(agent_name="code", session_id="thread-a", loaded_tools=["shell"])

    assert get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a") == ["shell"]
    assert get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-b") == []


def test_initial_deferred_tools_seed_loaded_state_and_expand_implied_runtime_tools(tmp_path: Path) -> None:
    """Initial deferred tools should seed loaded state and expand implied runtime tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"matrix_message": {"defer": True, "initial": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    loaded = get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a")
    runtime_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=loaded,
            enable_dynamic_tools_manager=False,
        )
    ]

    assert loaded == ["matrix_message"]
    assert runtime_names == ["matrix_message", "attachments", "matrix_room"]
    assert ("code", "thread-a") not in dynamic_toolkits_module._loaded_tools


def test_new_initial_deferred_tool_is_loaded_for_existing_session_after_reload(tmp_path: Path) -> None:
    """Existing loaded state should union current config initial tools after hot reload."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"shell": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)
    assert get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a") == []
    assert ("code", "thread-a") not in dynamic_toolkits_module._loaded_tools

    reloaded_raw = _base_config_data()
    reloaded_raw["agents"]["code"]["tools"] = [{"shell": {"defer": True, "initial": True}}]  # type: ignore[index]
    reloaded_config = _validated_config(tmp_path, reloaded_raw)
    manager = DynamicToolsToolkit(agent_name="code", config=reloaded_config, session_id="thread-a")

    assert get_loaded_tools_for_session(agent_name="code", config=reloaded_config, session_id="thread-a") == ["shell"]
    assert ("code", "thread-a") not in dynamic_toolkits_module._loaded_tools
    assert _tool_payload(manager.load_tool("shell"))["status"] == "already_loaded"
    assert _tool_payload(manager.unload_tool("shell"))["status"] == "sticky"
    assert get_loaded_tools_for_session(agent_name="code", config=reloaded_config, session_id="thread-a") == ["shell"]
    assert ("code", "thread-a") not in dynamic_toolkits_module._loaded_tools


def test_get_agent_toolkit_names_matches_sessionless_dynamic_tool_manager_visibility(tmp_path: Path) -> None:
    """Sessionless toolkit-name introspection should not advertise unavailable mutation tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"shell": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    assert "dynamic_tools" not in get_agent_toolkit_names("code", config)
    assert "dynamic_tools" in get_agent_toolkit_names("code", config, session_id="thread-a")


def test_sessionless_initial_deferred_tools_are_runtime_visible_without_manager(tmp_path: Path) -> None:
    """Sessionless agent construction should expose initial tools without enabling mutation tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"sleep": {"defer": True, "initial": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    loaded = get_loaded_tools_for_session(agent_name="code", config=config, session_id=None)
    runtime_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=loaded,
            enable_dynamic_tools_manager=False,
        )
    ]

    assert loaded == ["sleep"]
    assert runtime_names == ["sleep"]


def test_eager_preset_expansion_preserves_default_concrete_tool_overrides(tmp_path: Path) -> None:
    """Concrete default tool overrides should own preset-expanded children."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": [{"shell": {"shell_path_prepend": "/run/wrappers/bin"}}]}
    raw["agents"]["code"]["tools"] = ["openclaw_compat"]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    runtime_configs = _runtime_tool_configs(
        agent_name="code",
        config=config,
        loaded_tools=[],
        enable_dynamic_tools_manager=False,
    )
    overrides_by_name = {entry.name: entry.tool_config_overrides for entry in runtime_configs}

    assert overrides_by_name["shell"] == {"shell_path_prepend": "/run/wrappers/bin"}
    assert overrides_by_name["coding"] == {}


def test_loaded_deferred_concrete_tool_preserves_override_over_preset_child(tmp_path: Path) -> None:
    """Concrete deferred tool entries should own preset-expanded children once loaded."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        "openclaw_compat",
        {"shell": {"defer": True, "extra_env_passthrough": "FOO_*"}},
    ]
    config = _validated_config(tmp_path, raw)

    unloaded_configs = _runtime_tool_configs(
        agent_name="code",
        config=config,
        loaded_tools=[],
        enable_dynamic_tools_manager=False,
    )
    save_loaded_tools_for_session(agent_name="code", session_id="thread-a", loaded_tools=["shell"])
    loaded_configs = _runtime_tool_configs(
        agent_name="code",
        config=config,
        loaded_tools=get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a"),
        enable_dynamic_tools_manager=False,
    )
    unloaded_names = [entry.name for entry in unloaded_configs]
    loaded_overrides_by_name = {entry.name: entry.tool_config_overrides for entry in loaded_configs}

    assert "openclaw_compat" in unloaded_names
    assert "shell" not in unloaded_names
    assert loaded_overrides_by_name["shell"] == {"extra_env_passthrough": "FOO_*"}


def test_loaded_parent_keeps_implied_child_when_child_is_separately_deferred(tmp_path: Path) -> None:
    """A loaded deferred parent should expose its expanded children even if one child is separately unloaded."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"matrix_message": {"defer": True}},
        {"attachments": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)

    unloaded_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=[],
            enable_dynamic_tools_manager=False,
        )
    ]
    loaded_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=["matrix_message"],
            enable_dynamic_tools_manager=False,
        )
    ]

    assert unloaded_names == []
    assert loaded_names == ["matrix_message", "attachments", "matrix_room"]


@pytest.mark.parametrize(
    "tools",
    [
        ["openclaw_compat", {"matrix_message": {"defer": True}}],
        [{"matrix_message": {"defer": True}}, "openclaw_compat"],
    ],
)
def test_deferred_concrete_tool_gates_implied_children_over_preset_in_any_order(
    tmp_path: Path,
    tools: list[object],
) -> None:
    """A deferred concrete tool should own its full implied expansion over eager preset children."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = tools  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    unloaded_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=[],
            enable_dynamic_tools_manager=False,
        )
    ]
    loaded_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=["matrix_message"],
            enable_dynamic_tools_manager=False,
        )
    ]

    assert "matrix_message" not in unloaded_names
    assert "attachments" not in unloaded_names
    assert "matrix_room" not in unloaded_names
    assert "matrix_message" in loaded_names
    assert "attachments" in loaded_names
    assert "matrix_room" in loaded_names


def test_runtime_selection_hides_unloaded_deferred_tools(tmp_path: Path) -> None:
    """Runtime tool selection should expose only eager and loaded deferred tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", {"shell": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    unloaded_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=[],
            enable_dynamic_tools_manager=True,
        )
    ]
    loaded_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_tools=["shell"],
            enable_dynamic_tools_manager=True,
        )
    ]

    assert unloaded_names == ["sleep", "dynamic_tools"]
    assert loaded_names == ["sleep", "shell", "dynamic_tools"]


def test_dynamic_tools_manager_loads_unloads_searches_and_respects_sticky_initial(tmp_path: Path) -> None:
    """The manager should list, search, load, unload, and protect sticky tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"shell": {"defer": True, "initial": True}},
        {"sleep": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")

    listed = _tool_payload(manager.list_tools())
    assert listed["loaded_tools"] == ["shell"]
    assert listed["tools"] == [
        {
            "description": "Execute shell commands and scripts",
            "loaded": True,
            "name": "shell",
            "sticky": True,
        },
        {
            "description": "Sleep utility for introducing delays and pauses in execution",
            "loaded": False,
            "name": "sleep",
            "sticky": False,
        },
    ]

    search_payload = _tool_payload(manager.tool_search("sleep"))
    assert [match["name"] for match in search_payload["matches"]] == ["sleep"]

    assert _tool_payload(manager.load_tool("sleep"))["status"] == "loaded"
    assert _tool_payload(manager.load_tool("sleep"))["status"] == "already_loaded"
    assert _tool_payload(manager.unload_tool("shell"))["status"] == "sticky"
    assert _tool_payload(manager.unload_tool("sleep"))["status"] == "unloaded"
    assert ("code", "thread-a") not in dynamic_toolkits_module._loaded_tools
    assert _tool_payload(manager.unload_tool("sleep"))["status"] == "not_loaded"


def test_dynamic_tools_manager_catalog_responses_use_one_loaded_state_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manager catalog payloads should not mix loaded state from multiple reads."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"shell": {"defer": True}},
        {"sleep": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")
    loaded_snapshots = iter([["shell"], ["sleep"]])

    monkeypatch.setattr(manager, "_loaded_tools", lambda: next(loaded_snapshots))

    payload = _tool_payload(manager.list_tools())

    assert payload["loaded_tools"] == ["shell"]
    assert {entry["name"]: entry["loaded"] for entry in payload["tools"]} == {
        "shell": True,
        "sleep": False,
    }

    loaded_snapshots = iter([["shell"], ["sleep"]])
    monkeypatch.setattr(manager, "_loaded_tools", lambda: next(loaded_snapshots))

    search_payload = _tool_payload(manager.tool_search("sleep"))

    assert search_payload["loaded_tools"] == ["shell"]
    assert search_payload["matches"] == [
        {
            "description": "Sleep utility for introducing delays and pauses in execution",
            "loaded": False,
            "name": "sleep",
            "sticky": False,
        },
    ]


def test_dynamic_tools_manager_concurrent_loads_do_not_drop_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent load_tool calls should merge with current state under the state lock."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"shell": {"defer": True}},
        {"sleep": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")
    barrier = Barrier(2)
    original_loaded_tools = manager._loaded_tools

    def blocked_loaded_tools() -> list[str]:
        loaded_tools = original_loaded_tools()
        barrier.wait(timeout=5)
        return loaded_tools

    monkeypatch.setattr(manager, "_loaded_tools", blocked_loaded_tools)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(manager.load_tool, ("shell", "sleep")))

    assert {_tool_payload(result)["status"] for result in results} == {"loaded"}
    assert get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a") == [
        "shell",
        "sleep",
    ]


def test_dynamic_tools_manager_concurrent_load_collision_uses_latest_state(tmp_path: Path) -> None:
    """Concurrent load_tool calls should validate combined provider-visible state under the lock."""
    raw = _base_config_data()
    raw["mcp_servers"] = {
        "demo": {
            "transport": "stdio",
            "command": "npx",
        },
    }
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"mcp_demo": {"defer": True}},
        {"shell": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")

    class _FakeMCPManager:
        def mcp_tool_unavailable_messages_for_loaded_tools(
            self,
            _agent_name: str,
            _loaded_tools: list[str],
        ) -> list[str]:
            return []

        def function_name_collision_messages_for_loaded_tools(
            self,
            _agent_name: str,
            loaded_tools: list[str],
        ) -> list[str]:
            if {"mcp_demo", "shell"} <= set(loaded_tools):
                return ["MCP/local collision"]
            return []

    bind_mcp_server_manager(_FakeMCPManager())  # type: ignore[arg-type]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [_tool_payload(result) for result in executor.map(manager.load_tool, ("mcp_demo", "shell"))]
    finally:
        bind_mcp_server_manager(None)

    assert {payload["status"] for payload in results} == {"loaded", "function_name_collision"}
    assert get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a") in (
        ["mcp_demo"],
        ["shell"],
    )


def test_scope_incompatible_deferred_tools_reject_at_config_and_runtime(tmp_path: Path) -> None:
    """Scope-incompatible deferred tools should be rejected before schema exposure."""
    raw = _base_config_data()
    raw["agents"]["code"].update(  # type: ignore[union-attr]
        {
            "worker_scope": "user",
            "tools": [{"homeassistant": {"defer": True}}],
        },
    )
    with pytest.raises(
        ValueError,
        match=r"code -> deferred tool 'homeassistant' -> homeassistant \(worker_scope=user\)",
    ) as exc_info:
        _validated_config(tmp_path, raw)
    error_message = str(exc_info.value)
    assert "code -> deferred tool 'homeassistant' -> homeassistant (worker_scope=user)" in error_message
    assert "code -> homeassistant (worker_scope=user)" not in error_message

    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"homeassistant": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)
    config.agents["code"].worker_scope = "user"
    manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")

    payload = _tool_payload(manager.load_tool("homeassistant"))

    assert payload["status"] == "scope_incompatible"
    assert payload["unsupported_tools"] == ["homeassistant"]
    assert get_loaded_tools_for_session(agent_name="code", config=config, session_id="thread-a") == []


def test_matrix_metadata_injection_is_loaded_state_aware(tmp_path: Path) -> None:
    """Matrix prompt metadata should appear only when matrix_message is loaded."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"matrix_message": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    assert not _agent_has_matrix_messaging_tool(config, "code", "thread-a")

    save_loaded_tools_for_session(agent_name="code", session_id="thread-a", loaded_tools=["matrix_message"])

    assert _agent_has_matrix_messaging_tool(config, "code", "thread-a")


def test_dynamic_prompt_splits_static_catalog_from_volatile_loaded_state(tmp_path: Path) -> None:
    """Prompt catalog text should remain stable while loaded-state suffix changes."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"shell": {"defer": True, "initial": True}},
        {"sleep": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)

    static_before = _build_dynamic_tooling_instruction_block(
        config,
        "code",
        enable_dynamic_tools_manager=True,
    )
    suffix_before = _build_dynamic_tooling_state_suffix(
        config,
        "code",
        loaded_tools=("shell",),
        enable_dynamic_tools_manager=True,
    )
    suffix_after = _build_dynamic_tooling_state_suffix(
        config,
        "code",
        loaded_tools=("shell", "sleep"),
        enable_dynamic_tools_manager=True,
    )
    static_after = _build_dynamic_tooling_instruction_block(
        config,
        "code",
        enable_dynamic_tools_manager=True,
    )

    assert static_before == static_after
    assert "shell - Execute shell commands" in static_before
    assert suffix_before != suffix_after
    assert (
        suffix_after == "Dynamic tools currently loaded for this session: shell, sleep\n"
        "Sticky initial dynamic tools that cannot be unloaded: shell"
    )
