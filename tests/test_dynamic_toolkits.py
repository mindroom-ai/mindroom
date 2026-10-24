"""Tests for deferred tool configuration and visible tool surfaces."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom import agents as agents_module
from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.agents import (
    _context_hidden_toolkits,
    create_agent,
)
from mindroom.config.main import Config
from mindroom.config.models import EffectiveToolConfig, ToolConfigEntry
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import update_awareness
from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit
from mindroom.desktop.credentials import (
    delete_desktop_credentials,
    save_desktop_credentials,
)
from mindroom.history.prompt_tokens import build_static_token_estimator
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.response_runner import _agent_has_matrix_messaging_tool
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.dynamic_toolkits import (
    DeferredToolkit,
    defer_toolkit,
    visible_tool_surface,
)
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.agent_tool_helpers import call_entrypoint, offered_function, source_toolkit
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.tool_system.tool_function import AdaptedToolkit


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            **env,
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


def _validated_config(tmp_path: Path, raw: dict[str, object], **env: str) -> Config:
    runtime_paths = _runtime_paths(tmp_path, **env)
    config = Config.validate_with_runtime(raw, runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    return config


def _tool_payload(result: str) -> dict[str, object]:
    return json.loads(result)


def _build_prompt(build: AgentBuild) -> str:
    return str(prompt_for_turn(build).content)


def _toolkit_names(build: AgentBuild) -> set[str]:
    return {toolkit.name for toolkit in build.spec.toolkits}


def _toolkit(build: AgentBuild, name: str) -> AdaptedToolkit:
    return next(toolkit for toolkit in build.spec.toolkits if toolkit.name == name)


def _function_names(build: AgentBuild) -> set[str]:
    return {function.name for toolkit in build.spec.toolkits for function in toolkit.functions}


def _native_deferred_names(build: AgentBuild) -> frozenset[str]:
    """The functions native tool search sends marked deferred; portable search sends none of them."""
    deferred = build.request_template.deferred_tools
    return frozenset() if deferred.portable else deferred.names


def _portable_deferred_names(build: AgentBuild) -> frozenset[str]:
    """The functions portable tool search keeps off the request behind tool_search and call_tool."""
    deferred = build.request_template.deferred_tools
    return deferred.names if deferred.portable else frozenset()


async def _search(build: AgentBuild, query: str) -> dict[str, object]:
    return _tool_payload(cast("str", await call_entrypoint(offered_function(build, "tool_search"), query=query)))


def _private_identity(requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!shared:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="shared-session",
    )


def test_openai_compatible_context_hides_desktop() -> None:
    """Non-Matrix callers cannot access the Matrix-bound Desktop tool."""
    identity = ToolExecutionIdentity(
        channel="openai_compat",
        agent_name="code",
        requester_id="api-user",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="api-session",
    )

    assert "desktop" in _context_hidden_toolkits(identity)


def _install_update_awareness_status(monkeypatch: pytest.MonkeyPatch) -> str:
    status = update_awareness._MindRoomReleaseStatus(
        current_version="1.0.0",
        latest_version="1.0.0",
        update_available=False,
        release_check_succeeded=True,
    )
    monkeypatch.setattr(update_awareness, "_mindroom_release_status", lambda _runtime_paths: status)
    return "<mindroom_update_awareness>"


def _runtime_tool_configs(
    *,
    agent_name: str,
    config: Config,
    deferred_tools: list[str],
    enable_dynamic_tools_manager: bool,
) -> list[EffectiveToolConfig]:
    return list(
        visible_tool_surface(
            agent_name=agent_name,
            config=config,
            deferred_tools=deferred_tools,
            enable_dynamic_tools_manager=enable_dynamic_tools_manager,
        ).runtime_tool_configs,
    )


def test_config_accepts_inline_deferred_tool_flags(tmp_path: Path) -> None:
    """Inline tool entries should carry deferred tool flags and preserve overrides."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        "shell",
        {"file": {"defer": True, "initial": True, "enable_delete_file": True}},
        {"name": "searxng", "defer": True, "overrides": {"fixed_max_results": 10}},
    ]

    config = _validated_config(tmp_path, raw)

    entries = config.agents["code"].tools
    assert [(entry.name, entry.defer, entry.initial) for entry in entries] == [
        ("shell", False, False),
        ("file", True, True),
        ("searxng", True, False),
    ]
    assert entries[1].overrides == {"enable_delete_file": True}
    assert entries[2].overrides == {"fixed_max_results": 10}
    assert config.authored_model_dump()["agents"]["code"]["tools"] == [
        "shell",
        {"file": {"enable_delete_file": True, "defer": True, "initial": True}},
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


@pytest.mark.parametrize("tool_name", ["delegate", "dynamic_tools", "invite_router", "self_config", "skill_manage"])
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
    """Presets are bundles and must not be deferred units."""
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

    entries = config.resolve_entity("code").tool_configs
    assert [(entry.name, entry.defer, entry.initial, entry.authored_order) for entry in entries] == [
        ("shell", True, True, 0),
        ("sleep", False, False, 1),
    ]


def test_initial_deferred_tools_attach_under_a_restricted_selection_with_implied_tools(tmp_path: Path) -> None:
    """Initial deferred tools attach even when a selection names no deferred tool, with their implied tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"matrix_message": {"defer": True, "initial": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    surface = visible_tool_surface(agent_name="code", config=config, deferred_tools=[])

    assert [entry.name for entry in surface.runtime_tool_configs] == ["matrix_message", "attachments", "matrix_room"]


def test_unrestricted_selection_attaches_every_deferred_tool(tmp_path: Path) -> None:
    """Without a selection every authored deferred tool is attached, and the search toolkit only on request."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", {"shell": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    plain = visible_tool_surface(agent_name="code", config=config)
    searchable = visible_tool_surface(agent_name="code", config=config, enable_dynamic_tools_manager=True)

    assert [entry.name for entry in plain.runtime_tool_configs] == ["sleep", "shell"]
    assert [entry.name for entry in searchable.runtime_tool_configs] == ["sleep", "shell", "dynamic_tools"]


def test_eager_preset_expansion_preserves_default_concrete_tool_overrides(tmp_path: Path) -> None:
    """Concrete default tool overrides should own preset-expanded children."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": [{"shell": {"shell_path_prepend": "/run/wrappers/bin"}}]}
    raw["agents"]["code"]["tools"] = ["openclaw_compat"]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    runtime_configs = _runtime_tool_configs(
        agent_name="code",
        config=config,
        deferred_tools=[],
        enable_dynamic_tools_manager=False,
    )
    overrides_by_name = {entry.name: entry.tool_config_overrides for entry in runtime_configs}

    assert overrides_by_name["shell"] == {"shell_path_prepend": "/run/wrappers/bin"}
    assert overrides_by_name["coding"] == {}


def test_attached_deferred_concrete_tool_preserves_override_over_preset_child(tmp_path: Path) -> None:
    """Concrete deferred tool entries should own preset-expanded children once attached."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        "openclaw_compat",
        {"shell": {"defer": True, "extra_env_passthrough": "FOO_*"}},
    ]
    config = _validated_config(tmp_path, raw)

    unattached_configs = _runtime_tool_configs(
        agent_name="code",
        config=config,
        deferred_tools=[],
        enable_dynamic_tools_manager=False,
    )
    attached_configs = _runtime_tool_configs(
        agent_name="code",
        config=config,
        deferred_tools=["shell"],
        enable_dynamic_tools_manager=False,
    )
    unattached_names = [entry.name for entry in unattached_configs]
    attached_overrides_by_name = {entry.name: entry.tool_config_overrides for entry in attached_configs}

    assert "openclaw_compat" in unattached_names
    assert "shell" not in unattached_names
    assert attached_overrides_by_name["shell"] == {"extra_env_passthrough": "FOO_*"}


def test_attached_parent_keeps_implied_child_when_child_is_separately_deferred(tmp_path: Path) -> None:
    """An attached deferred parent should expose its expanded children even if one child is separately unattached."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"matrix_message": {"defer": True}},
        {"attachments": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)

    unattached_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            deferred_tools=[],
            enable_dynamic_tools_manager=False,
        )
    ]
    attached_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            deferred_tools=["matrix_message"],
            enable_dynamic_tools_manager=False,
        )
    ]

    assert unattached_names == []
    assert attached_names == ["matrix_message", "attachments", "matrix_room"]


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

    unattached_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            deferred_tools=[],
            enable_dynamic_tools_manager=False,
        )
    ]
    attached_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            deferred_tools=["matrix_message"],
            enable_dynamic_tools_manager=False,
        )
    ]

    assert "matrix_message" not in unattached_names
    assert "attachments" not in unattached_names
    assert "matrix_room" not in unattached_names
    assert "matrix_message" in attached_names
    assert "attachments" in attached_names
    assert "matrix_room" in attached_names


def test_runtime_selection_hides_unattached_deferred_tools(tmp_path: Path) -> None:
    """Runtime tool selection should expose only eager and attached deferred tools."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", {"shell": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    unattached_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            deferred_tools=[],
            enable_dynamic_tools_manager=True,
        )
    ]
    attached_names = [
        entry.name
        for entry in _runtime_tool_configs(
            agent_name="code",
            config=config,
            deferred_tools=["shell"],
            enable_dynamic_tools_manager=True,
        )
    ]

    assert unattached_names == ["sleep", "dynamic_tools"]
    assert attached_names == ["sleep", "shell", "dynamic_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_deferred_desktop_uses_only_requester_agent_credentials(tmp_path: Path, private: bool) -> None:
    """Normal and private agents reload only the active requester's Desktop identity."""
    raw = _base_config_data()
    if private:
        raw["agents"]["code"]["private"] = {"per": "user_agent"}  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        "calculator",
        {"desktop": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    alice_identity = _private_identity("@alice:example.org")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    shared_identity = {
        "device_user_id": "@shared-desktop:example.org",
        "device_id": "SHARED",
        "device_ed25519": "shared-fingerprint",
    }
    credentials_manager.shared_manager().save_credentials("desktop", shared_identity)

    alice_agent = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=alice_identity,
    )

    assert "calculator" in _toolkit_names(alice_agent)
    assert "desktop" in _portable_deferred_names(alice_agent)
    alice_desktop = cast("Any", source_toolkit(offered_function(alice_agent, "desktop")))
    result = await alice_desktop.desktop("status")
    assert _tool_payload(result.content)["status"] == "setup_required"

    save_desktop_credentials(
        credentials_manager,
        {
            "device_user_id": "@alice-desktop:example.org",
            "device_id": "ALICE",
            "device_ed25519": "alice-fingerprint",
        },
        requester_id="@alice:example.org",
        agent_name="code",
    )

    paired = alice_desktop._current_configuration()
    assert paired.target is not None
    assert paired.target.user_id == "@alice-desktop:example.org"

    save_desktop_credentials(
        credentials_manager,
        {
            "device_user_id": "@alice-rotated:example.org",
            "device_id": "ROTATED",
            "device_ed25519": "rotated-fingerprint",
        },
        requester_id="@alice:example.org",
        agent_name="code",
    )
    rotated = alice_desktop._current_configuration()
    assert rotated.target is not None
    assert rotated.target.user_id == "@alice-rotated:example.org"

    delete_desktop_credentials(
        credentials_manager,
        requester_id="@alice:example.org",
        agent_name="code",
    )
    assert alice_desktop._current_configuration().target is None


@pytest.mark.asyncio
async def test_native_tool_search_keeps_unconfigured_desktop_safe(tmp_path: Path) -> None:
    """Native deferred Desktop keeps one stable schema and fails closed until paired."""
    raw = _base_config_data()
    raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
    raw["agents"]["code"].update(  # type: ignore[union-attr,index]
        {
            "model": "claude",
            "private": {"per": "user_agent"},
            "tools": [{"desktop": {"defer": True}}],
        },
    )
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    identity = _private_identity("@alice:example.org")

    unpaired = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=identity,
    )
    unpaired_desktop = cast("Any", source_toolkit(offered_function(unpaired, "desktop")))
    result = await unpaired_desktop.desktop("status")
    assert _tool_payload(result.content)["status"] == "setup_required"
    assert "desktop" in _native_deferred_names(unpaired)

    save_desktop_credentials(
        get_runtime_credentials_manager(runtime_paths),
        {
            "device_user_id": "@alice-desktop:example.org",
            "device_id": "ALICE",
            "device_ed25519": "alice-fingerprint",
        },
        requester_id="@alice:example.org",
        agent_name="code",
    )
    paired = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=identity,
    )
    paired_desktop = cast("Any", source_toolkit(offered_function(paired, "desktop")))
    paired_configuration = paired_desktop._current_configuration()
    assert paired_configuration.target is not None
    assert paired_configuration.target.user_id == "@alice-desktop:example.org"
    assert "desktop" in _native_deferred_names(paired)


def _egress_broker_env(*, broker_on: bool) -> dict[str, str]:
    return {"MINDROOM_EGRESS_BROKER_PORT": "8768"} if broker_on else {}


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_on", [False, True])
async def test_deferred_egress_credentials_is_discoverable_only_while_the_broker_is_on(
    tmp_path: Path,
    *,
    broker_on: bool,
) -> None:
    """With the broker off the deferred egress tool is neither attached nor searchable."""
    env = _egress_broker_env(broker_on=broker_on)
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"egress_credentials": {"defer": True}},
        {"sleep": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw, **env)

    agent = create_agent("code", config, _runtime_paths(tmp_path, **env), execution_identity=None)

    searched = await _search(agent, "egress")
    assert ({match["toolkit"] for match in searched["matches"]} == {"egress_credentials"}) is broker_on
    assert ("egress_credentials" in _toolkit_names(agent)) is broker_on


def test_a_lone_deferred_egress_credentials_tool_gives_no_dynamic_tools_while_the_broker_is_off(
    tmp_path: Path,
) -> None:
    """An agent whose only deferred tool is unavailable gets no empty search toolkit."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"egress_credentials": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert "dynamic_tools" not in _toolkit_names(agent)
    assert _portable_deferred_names(agent) == frozenset()


@pytest.mark.parametrize("native_tool_search", [False, True])
@pytest.mark.parametrize("broker_on", [False, True])
def test_deferred_prompt_names_egress_credentials_only_while_the_broker_is_on(
    tmp_path: Path,
    *,
    native_tool_search: bool,
    broker_on: bool,
) -> None:
    """The deferred-tool prompt, manager-backed or native, leaves out the egress tool with the broker off."""
    env = _egress_broker_env(broker_on=broker_on)
    raw = _base_config_data()
    if native_tool_search:
        raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
        raw["agents"]["code"]["model"] = "claude"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"egress_credentials": {"defer": True}},
        {"sleep": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw, **env)

    agent = create_agent(
        "code",
        config,
        _runtime_paths(tmp_path, **env),
        execution_identity=None,
    )

    prompt = _build_prompt(agent)
    assert ("egress_credentials" in prompt) is broker_on
    assert "sleep" in prompt


@pytest.mark.parametrize("broker_on", [False, True])
def test_an_eager_egress_credentials_tool_is_attached_only_while_the_broker_is_on(
    tmp_path: Path,
    *,
    broker_on: bool,
) -> None:
    """The broker switch also decides the eager entry."""
    env = _egress_broker_env(broker_on=broker_on)
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["egress_credentials", "sleep"]  # type: ignore[index]
    config = _validated_config(tmp_path, raw, **env)

    agent = create_agent(
        "code",
        config,
        _runtime_paths(tmp_path, **env),
        execution_identity=None,
    )

    assert ("egress_credentials" in _toolkit_names(agent)) is broker_on


def test_deferred_scope_validation_builds_authored_tools_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope validation must not rebuild the whole tool list for every deferred entry."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {name: {"defer": True}} for name in ["homeassistant", "shell", "file", "calculator", "sleep"]
    ]
    config = _validated_config(tmp_path, raw)
    config.agents["code"].worker_scope = "user"
    original = Config._agent_authored_deferred_tool_configs
    builds = 0

    def count_builds(self: Config, agent_name: str) -> list[EffectiveToolConfig]:
        nonlocal builds
        builds += 1
        return original(self, agent_name)

    monkeypatch.setattr(Config, "_agent_authored_deferred_tool_configs", count_builds)

    assert config._agent_scope_incompatible_deferred_tools("code") == {"homeassistant": ["homeassistant"]}
    assert builds == 1


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

    surface = visible_tool_surface(agent_name="code", config=config)

    assert "homeassistant" not in {entry.name for entry in surface.runtime_tool_configs}


@pytest.mark.parametrize("defer", [False, True])
def test_matrix_metadata_injection_follows_configured_matrix_message(tmp_path: Path, *, defer: bool) -> None:
    """Matrix prompt metadata appears whenever matrix_message is configured, deferred or not."""
    raw = _base_config_data()
    assert not _agent_has_matrix_messaging_tool(_validated_config(tmp_path, raw), "code")

    raw["agents"]["code"]["tools"] = [{"matrix_message": {"defer": defer}}]  # type: ignore[index]

    assert _agent_has_matrix_messaging_tool(_validated_config(tmp_path, raw), "code")


def test_openclaw_compat_implies_matrix_messaging_tool(tmp_path: Path) -> None:
    """openclaw_compat should imply matrix_message availability without explicit config."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["openclaw_compat"]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    assert _agent_has_matrix_messaging_tool(config, "code")


def test_native_tool_search_attaches_deferred_toolkits_and_skips_portable_tool_search(tmp_path: Path) -> None:
    """Claude-native tool search attaches all deferred toolkits and drops the manager machinery."""
    raw = _base_config_data()
    raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "claude"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"sleep": {"defer": True}},
        {"calculator": {"defer": True, "initial": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    function_names = _function_names(agent)
    assert "sleep" in function_names
    assert "add" in function_names
    # Only defer&&!initial tools go on the wire deferred; initial stays plain.
    assert _native_deferred_names(agent) == frozenset({"sleep"})
    assert "tool_search" not in function_names
    assert not any(block.startswith("## Deferred Tools\n") for block in agent.prompt.instructions)


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("anthropic", "claude-opus-5"),
        ("openai", "gpt-6-astra"),
    ],
)
def test_native_tool_search_prompt_lists_deferred_capability_domains(
    tmp_path: Path,
    provider: str,
    model_id: str,
) -> None:
    """Native tool search should tell the model which capability domains it can discover."""
    raw = _base_config_data()
    raw["models"]["native"] = {"provider": provider, "id": model_id}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "native"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"sleep": {"defer": True}},
        {"calculator": {"defer": True, "initial": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    discovery_block = next(
        block for block in agent.prompt.instructions if block.startswith("## Deferred Tool Discovery")
    )
    assert discovery_block in _build_prompt(agent)
    assert "Deferred capability domains available through native tool search: sleep." in discovery_block
    assert "search the deferred tool catalog before concluding that the capability is unavailable" in discovery_block
    assert "calculator" not in discovery_block


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("anthropic", "claude-opus-5"),
        ("openai", "gpt-6-astra"),
    ],
)
@pytest.mark.parametrize("collide_all", [False, True], ids=["partial-collision", "full-collision"])
def test_native_tool_search_does_not_defer_eager_local_collision_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_id: str,
    *,
    collide_all: bool,
) -> None:
    """Final MCP collision projection must preserve the surviving local tool's eager ownership."""
    raw = _base_config_data()
    raw["models"]["native"] = {"provider": provider, "id": model_id}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "native"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = ["sleep", {"mcp_demo": {"defer": True}}]  # type: ignore[index]
    raw["mcp_servers"] = {
        "demo": {
            "transport": "streamable-http",
            "url": "https://mcp.example.test/mcp",
            "auth": {
                "type": "oauth",
                "discovery": "manual",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
            },
        },
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)

    def fake_build_agent_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        if tool_name == "mcp_demo":
            return MindRoomMCPToolkit(
                server_id="demo",
                manager=None,
                catalog=None,
                server_config=config.mcp_servers["demo"],
                runtime_paths=runtime_paths,
                credentials_manager=get_runtime_credentials_manager(runtime_paths),
            )
        local = Toolkit(name="local", auto_register=False)
        colliding_names = (
            {"demo_call_tool", "demo_connection_status", "demo_list_tools"} if collide_all else {"demo_list_tools"}
        )
        for function_name in colliding_names:
            local.functions[function_name] = Function(
                name=function_name,
                entrypoint=lambda: "local",
            )
        return local

    monkeypatch.setattr("mindroom.agents.build_agent_toolkit", fake_build_agent_toolkit)

    agent = create_agent("code", config, runtime_paths, execution_identity=None)

    expected_deferred_names = frozenset() if collide_all else frozenset({"demo_call_tool", "demo_connection_status"})
    assert _native_deferred_names(agent) == expected_deferred_names
    assert "demo_list_tools" in {function.name for function in _toolkit(agent, "local").functions}
    discovery_blocks = [block for block in agent.prompt.instructions if block.startswith("## Deferred Tool Discovery")]
    assert bool(discovery_blocks) is not collide_all
    if discovery_blocks:
        assert "mcp_demo" in discovery_blocks[0]


@pytest.mark.asyncio
async def test_tool_search_lists_only_the_deferred_mcp_functions_the_run_offers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deferred MCP function a local function's name hides leaves the run and the search, not the MCP toolkit."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", {"mcp_demo": {"defer": True}}]  # type: ignore[index]
    raw["mcp_servers"] = {
        "demo": {
            "transport": "streamable-http",
            "url": "https://mcp.example.test/mcp",
            "auth": {
                "type": "oauth",
                "discovery": "manual",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
            },
        },
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    built: dict[str, Toolkit] = {}
    build_agent_toolkit = agents_module.build_agent_toolkit

    def fake_build_agent_toolkit(tool_name: str, **kwargs: object) -> Toolkit | None:
        if tool_name == "mcp_demo":
            built[tool_name] = MindRoomMCPToolkit(
                server_id="demo",
                manager=None,
                catalog=None,
                server_config=config.mcp_servers["demo"],
                runtime_paths=runtime_paths,
                credentials_manager=get_runtime_credentials_manager(runtime_paths),
            )
            return built[tool_name]
        if tool_name == "sleep":
            local = Toolkit(name="local", auto_register=False)
            local.functions["demo_list_tools"] = Function(name="demo_list_tools", entrypoint=lambda: "local")
            return local
        return build_agent_toolkit(tool_name, **kwargs)

    monkeypatch.setattr("mindroom.agents.build_agent_toolkit", fake_build_agent_toolkit)

    agent = create_agent("code", config, runtime_paths, execution_identity=None)

    assert _portable_deferred_names(agent) == frozenset({"demo_call_tool", "demo_connection_status"})
    listed = (await _search(agent, ""))["functions"]
    assert sorted(item["name"] for item in listed) == ["demo_call_tool", "demo_connection_status"]
    assert "demo_list_tools" in built["mcp_demo"].async_functions


def test_native_tool_search_omits_fully_deferred_toolkit_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native-search toolkit should not describe functions that are all deferred."""
    instruction_marker = _install_update_awareness_status(monkeypatch)
    raw = _base_config_data()
    raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "claude"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [{"update_awareness": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert _toolkit(agent, "update_awareness").instructions is None
    assert instruction_marker not in _build_prompt(agent)
    assert _native_deferred_names(agent) == frozenset({"get_mindroom_update_status"})


def test_fully_deferred_toolkit_omits_function_instructions() -> None:
    """A deferred toolkit should not leak instructions attached to its functions."""

    def deferred_tool() -> str:
        return "deferred"

    function = Function(
        name="deferred_tool",
        entrypoint=deferred_tool,
        instructions="DEFERRED_FUNCTION_INSTRUCTIONS",
        add_instructions=True,
    )
    toolkit = Toolkit(name="deferred", tools=[function])
    defer_toolkit("deferred", "", toolkit)

    assert toolkit.functions["deferred_tool"].add_instructions is False
    # Adapting refuses a function that still asks for prompt instructions; the deferred toolkit adds none.
    assert adapt_toolkit(toolkit).instructions is None


def test_instruction_suppression_uses_deferred_toolkit_identity() -> None:
    """A name collision must not suppress instructions from an active toolkit."""

    def active_tool() -> str:
        return "active"

    def deferred_tool() -> str:
        return "deferred"

    active_toolkit_marker = "ACTIVE_TOOLKIT_INSTRUCTIONS"
    active_toolkit = Toolkit(
        name="active",
        tools=[Function(name="shared_tool", entrypoint=active_tool)],
        instructions=active_toolkit_marker,
        add_instructions=True,
    )
    deferred_function = Function(
        name="shared_tool",
        entrypoint=deferred_tool,
        instructions="DEFERRED_FUNCTION_INSTRUCTIONS",
        add_instructions=True,
    )
    deferred_toolkit = Toolkit(
        name="deferred",
        tools=[deferred_function],
        instructions="DEFERRED_TOOLKIT_INSTRUCTIONS",
        add_instructions=True,
    )
    defer_toolkit("deferred", "", deferred_toolkit)

    assert active_toolkit.add_instructions is True
    assert active_toolkit.functions["shared_tool"].add_instructions is True
    assert deferred_toolkit.add_instructions is False
    assert deferred_toolkit.functions["shared_tool"].add_instructions is False
    assert adapt_toolkit(active_toolkit).instructions == active_toolkit_marker
    assert adapt_toolkit(deferred_toolkit).instructions is None


def test_native_tool_search_keeps_initial_toolkit_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An initial deferred toolkit should keep its instructions inline."""
    instruction_marker = _install_update_awareness_status(monkeypatch)
    raw = _base_config_data()
    raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "claude"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"update_awareness": {"defer": True, "initial": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)
    instructions = _toolkit(agent, "update_awareness").instructions

    assert instructions is not None
    assert instruction_marker in instructions
    assert instruction_marker in _build_prompt(agent)
    assert _native_deferred_names(agent) == frozenset()


def test_native_tool_search_drops_toolkit_emptied_by_include_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final assembly should discard a toolkit with no provider-visible functions."""
    instruction_marker = _install_update_awareness_status(monkeypatch)
    raw = _base_config_data()
    raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "claude"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"update_awareness": {"defer": True, "include_tools": []}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert "update_awareness" not in _toolkit_names(agent)
    assert instruction_marker not in _build_prompt(agent)
    assert _native_deferred_names(agent) == frozenset()


@pytest.mark.asyncio
async def test_portable_tool_search_returns_rendered_toolkit_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deferred toolkit's instructions leave the system prompt and arrive with its search results."""
    instruction_marker = _install_update_awareness_status(monkeypatch)
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"update_awareness": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert instruction_marker not in _build_prompt(agent)
    payload = await _search(agent, "update_awareness")
    assert instruction_marker in payload["instructions"]["update_awareness"]


@pytest.mark.parametrize(
    ("tool_entry", "excluded"),
    [
        ("chat_ui", ("show_canvas", "read_canvas_state")),
        ({"chat_ui": {"enable_show_canvas": True}}, ()),
        ({"chat_ui": {"enable_show_canvas": True, "exclude_tools": ["open_settings"]}}, ("open_settings",)),
    ],
)
def test_chat_ui_instructions_map_only_the_enabled_functions(
    tmp_path: Path,
    tool_entry: object,
    excluded: tuple[str, ...],
) -> None:
    """The agent's prompt names what each chat_ui function works on, and nothing it cannot call.

    Canvases are opt-in, so a plain chat_ui entry neither has show_canvas nor describes it.
    """
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [tool_entry]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)
    prompt = _build_prompt(agent)

    assert "chat_ui shows parts of MindRoom Chat to the user." in prompt
    for name in TOOL_METADATA["chat_ui"].function_names:
        assert (f"\n- {name}(" in prompt) is (name not in excluded)
    assert "open_panel(panel='computer') shows the Computer panel" in prompt
    assert "open_panel(panel='members') shows the Members panel" in prompt


@pytest.mark.parametrize(
    ("options", "named"),
    [
        ({"enable_show_canvas": True}, False),
        ({"enable_canvas_libraries": True}, False),
        ({"enable_show_canvas": True, "enable_canvas_libraries": True}, True),
        ({"enable_show_canvas": True, "enable_canvas_libraries": True, "exclude_tools": ["show_canvas"]}, False),
    ],
)
def test_chat_ui_names_the_canvas_library_source_only_for_an_agent_with_canvases(
    tmp_path: Path,
    options: dict[str, object],
    named: bool,
) -> None:
    """Pages that load libraries break where Chat blocks them, so the source is opt-in like canvases."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"chat_ui": options}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)
    prompt = _build_prompt(agent)

    assert ("https://cdn.jsdelivr.net/npm/" in prompt) is named


def test_excluding_a_disabled_chat_ui_function_keeps_the_toolkit(tmp_path: Path) -> None:
    """An operator may exclude show_canvas defensively; that must not drop the other chat_ui functions."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"chat_ui": {"exclude_tools": ["show_canvas"]}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert sorted(function.name for function in _toolkit(agent, "chat_ui").functions) == [
        "open_panel",
        "open_settings",
        "show_computer",
    ]


@pytest.mark.parametrize(
    ("options", "available"),
    [
        ({"include_tools": ["show_computer"]}, "show_computer"),
        ({"enable_show_canvas": True, "include_tools": ["show_canvas"]}, "show_canvas"),
    ],
)
def test_chat_ui_instructions_never_point_at_a_missing_function(
    tmp_path: Path,
    options: dict[str, object],
    available: str,
) -> None:
    """With one function left, neither the map nor the function's own description names another."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"chat_ui": options}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)
    instructions = _toolkit(agent, "chat_ui").instructions
    description = offered_function(agent, available).description

    assert instructions is not None
    assert description is not None
    for name in set(TOOL_METADATA["chat_ui"].function_names) - {available}:
        assert f"{name}(" not in instructions
        assert f"{name}(" not in description
    assert f"- {available}(" in _build_prompt(agent)


@pytest.mark.parametrize(("provider", "model_id"), [("codex", "gpt-6-astra"), ("openai", "gpt-6-astra")])
def test_openai_native_tool_search_attaches_deferred_toolkits_and_skips_portable_tool_search(
    tmp_path: Path,
    provider: str,
    model_id: str,
) -> None:
    """OpenAI-native tool search attaches all deferred toolkits and drops the manager machinery."""
    raw = _base_config_data()
    raw["models"]["native_gpt"] = {"provider": provider, "id": model_id}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "native_gpt"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"sleep": {"defer": True}},
        {"calculator": {"defer": True, "initial": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    function_names = _function_names(agent)
    assert "sleep" in function_names
    assert "add" in function_names
    # Only defer&&!initial tools go on the wire deferred; initial stays plain.
    assert _native_deferred_names(agent) == frozenset({"sleep"})
    assert "tool_search" not in function_names
    assert not any(block.startswith("## Deferred Tools\n") for block in agent.prompt.instructions)


@pytest.mark.parametrize(
    ("api", "base_url"),
    [("chat_completions", None), ("responses", "http://localhost:9292/v1")],
)
def test_explicit_openai_api_uses_portable_tool_search_when_native_is_unavailable(
    tmp_path: Path,
    api: str,
    base_url: str | None,
) -> None:
    """Chat and Responses proxies must not lose deferred tools to hosted-only search."""
    raw = _base_config_data()
    raw["models"]["gpt"] = {  # type: ignore[index]
        "provider": "openai",
        "id": "gpt-6-astra",
        "api": api,
        "extra_kwargs": {"base_url": base_url},
    }
    raw["agents"]["code"]["model"] = "gpt"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [{"sleep": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert _portable_deferred_names(agent) == frozenset({"sleep"})


def test_codex_deferred_browser_uses_non_reserved_function_name(tmp_path: Path) -> None:
    """The deferred browser function must not collide with Codex's reserved browser namespace."""
    raw = _base_config_data()
    raw["models"]["codex"] = {"provider": "codex", "id": "gpt-6-astra"}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "codex"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [{"browser": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    function_names = _function_names(agent)
    assert "browser_control" in function_names
    assert "browser" not in function_names
    assert _native_deferred_names(agent) == frozenset({"browser_control"})


def test_immutable_tool_schema_eagerly_materializes_every_deferred_tool(tmp_path: Path) -> None:
    """Voice-style immutable schemas send every deferred tool eagerly instead of through tool search."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"sleep": {"defer": True}},
        {"calculator": {"defer": True, "initial": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent(
        "code",
        config,
        _runtime_paths(tmp_path),
        execution_identity=None,
        eager_deferred_tools=True,
    )

    function_names = _function_names(agent)
    assert "sleep" in function_names
    assert "add" in function_names
    assert agent.request_template.deferred_tools.names == frozenset()
    assert "tool_search" not in function_names
    assert not any(block.startswith("## Deferred Tools\n") for block in agent.prompt.instructions)


def test_eager_tool_filter_drops_fully_filtered_deferred_toolkit(tmp_path: Path) -> None:
    """Eager deferred tools cannot advertise a toolkit with no callable functions."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [{"sleep": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent(
        "code",
        config,
        _runtime_paths(tmp_path),
        execution_identity=None,
        eager_deferred_tools=True,
        tool_function_filter=lambda _function: False,
    )

    function_names = _function_names(agent)
    assert "sleep" not in function_names
    assert "tool_search" not in function_names
    assert not any(block.startswith("## Deferred Tools\n") for block in agent.prompt.instructions)


@pytest.mark.parametrize(
    ("provider", "model_id", "extra_kwargs"),
    [
        ("openai", "gpt-4o-mini", None),
        ("openai", "gpt-6-astra", {"base_url": "http://localhost:9292/v1"}),
        ("codex", "gpt-4.1", None),
        ("anthropic", "claude-opus-4-1", None),
    ],
)
def test_unsupported_models_use_portable_tool_search(
    tmp_path: Path,
    provider: str,
    model_id: str,
    extra_kwargs: dict[str, object] | None,
) -> None:
    """Models without native search register deferred tools but send only tool_search and call_tool for them."""
    raw = _base_config_data()
    raw["models"]["default"] = {"provider": provider, "id": model_id, "extra_kwargs": extra_kwargs}  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"sleep": {"defer": True}},
        {"calculator": {"defer": True, "initial": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    function_names = _function_names(agent)
    assert {"sleep", "add", "tool_search", "call_tool"} <= function_names
    assert "load_tool" not in function_names
    # initial tools stay ordinary eager tools, so only sleep is hidden behind call_tool.
    assert _portable_deferred_names(agent) == frozenset({"sleep"})
    catalog = next(block for block in agent.prompt.instructions if block.startswith("## Deferred Tools\n"))
    assert f"sleep - {TOOL_METADATA['sleep'].description}" in catalog
    assert "calculator" not in catalog
    assert "call_tool(name, arguments)" in catalog
    assert catalog in _build_prompt(agent)


@pytest.mark.asyncio
async def test_browser_catalog_points_at_the_deferred_chat_ui_panel(tmp_path: Path) -> None:
    """The deferred catalog keeps the browser's pointer to the Computer panel when both tools are deferred."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = [  # type: ignore[index]
        {"browser": {"defer": True}},
        {"chat_ui": {"defer": True}},
    ]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    catalog = next(block for block in agent.prompt.instructions if block.startswith("## Deferred Tools\n"))
    assert "chat_ui.open_panel(panel='computer')" in catalog
    assert "Computer panel" in catalog
    assert (await _search(agent, "browser"))["matches"][0]["toolkit"] == "browser"


class _CrmTools(Toolkit):
    """Deferred-toolkit stand-in with two documented functions and toolkit instructions."""

    def __init__(self) -> None:
        super().__init__(
            name="crm",
            tools=[self.crm_lookup, self.crm_notes],
            instructions="Prefer exact customer IDs.",
            add_instructions=True,
        )

    def crm_lookup(self, customer_id: int) -> str:
        """Look up one customer record by ID."""
        return f"customer {customer_id}"

    def crm_notes(self, customer_id: int, limit: int = 5) -> str:
        """Read recent support notes about an account lookup."""
        return f"{limit} notes for {customer_id}"


class _WeatherTools(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="weather", tools=[self.forecast])

    def forecast(self, city: str) -> str:
        """Return the forecast for a city."""
        return city


def _deferred(toolkit: Toolkit, description: str, domain_name: str | None = None) -> DeferredToolkit:
    """Defer ``toolkit`` and adapt it, as agent construction does, offering every function of it."""
    deferral = defer_toolkit(domain_name or toolkit.name, description, toolkit)
    return DeferredToolkit(deferral, adapt_toolkit(toolkit).functions)


def _search_toolkit(*deferred: DeferredToolkit) -> DynamicToolsToolkit:
    return DynamicToolsToolkit(deferred_toolkits=lambda: deferred)


def test_tool_search_returns_definitions_and_toolkit_instructions() -> None:
    """Search results carry each function's full definition and its toolkit's rendered instructions."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"))

    payload = _tool_payload(toolkit.tool_search("crm_lookup"))

    match = payload["matches"][0]
    assert match["name"] == "crm_lookup"
    assert match["toolkit"] == "crm"
    assert match["description"] == "Look up one customer record by ID."
    assert set(match["parameters"]["properties"]) == {"customer_id"}
    assert payload["instructions"] == {"crm": "Prefer exact customer IDs."}
    assert "call_tool(name, arguments)" in payload["usage"]


def test_tool_search_ranks_name_hits_over_description_hits() -> None:
    """A function-name hit ranks above a description-only hit."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"))

    payload = _tool_payload(toolkit.tool_search("lookup"))

    assert [match["name"] for match in payload["matches"]] == ["crm_lookup", "crm_notes"]


def test_tool_search_matches_toolkit_names_and_omits_unrelated_toolkits() -> None:
    """A toolkit-name query returns that toolkit's functions and no instructions from unmatched toolkits."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"), _deferred(_WeatherTools(), "Forecasts"))

    payload = _tool_payload(toolkit.tool_search("weather"))

    assert [match["name"] for match in payload["matches"]] == ["forecast"]
    assert "instructions" not in payload


def test_tool_search_clamps_max_results() -> None:
    """max_results is clamped to at least one and at most the search limit."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"))

    assert len(_tool_payload(toolkit.tool_search("crm", max_results=0))["matches"]) == 1
    assert len(_tool_payload(toolkit.tool_search("crm", max_results=99))["matches"]) == 2


def test_tool_search_says_when_more_functions_matched_than_it_returned() -> None:
    """A cut-off result names how many functions matched so the model can widen or narrow its search."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"))

    truncated = _tool_payload(toolkit.tool_search("crm", max_results=1))
    complete = _tool_payload(toolkit.tool_search("crm", max_results=2))

    assert len(truncated["matches"]) == 1
    assert "2 functions matched" in truncated["message"]
    assert "max_results" in truncated["message"]
    assert "message" not in complete


def test_tool_search_at_the_result_limit_suggests_only_narrower_keywords() -> None:
    """A cut-off result at the result limit does not suggest raising max_results, which cannot return more."""

    def bulk_function(index: int) -> Function:
        return Function(name=f"bulk_{index}", description="Bulk operation.", entrypoint=lambda: "ok")

    bulk = Toolkit(name="bulk")
    for index in range(25):
        bulk.functions[f"bulk_{index}"] = bulk_function(index)
    toolkit = _search_toolkit(_deferred(bulk, "Bulk operations"))

    below_limit = _tool_payload(toolkit.tool_search("bulk", max_results=19))
    at_limit = _tool_payload(toolkit.tool_search("bulk", max_results=99))

    assert "max_results" in below_limit["message"]
    assert len(at_limit["matches"]) == 20
    assert "25 functions matched" in at_limit["message"]
    assert "max_results" not in at_limit["message"]


def test_tool_search_without_a_query_lists_every_deferred_function() -> None:
    """An empty query lists each deferred function's name, toolkit, and description, without definitions."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"), _deferred(_WeatherTools(), "Forecasts"))

    payload = _tool_payload(toolkit.tool_search())

    assert payload["functions"] == [
        {"name": "crm_lookup", "toolkit": "crm", "description": "Look up one customer record by ID."},
        {"name": "crm_notes", "toolkit": "crm", "description": "Read recent support notes about an account lookup."},
        {"name": "forecast", "toolkit": "weather", "description": "Return the forecast for a city."},
    ]
    assert "matches" not in payload
    assert "message" not in payload
    assert "tool_search" in payload["usage"]


def _bulk_search_toolkit(count: int, description: str) -> DynamicToolsToolkit:
    def bulk_function(index: int) -> Function:
        return Function(name=f"bulk_{index}", description=description, entrypoint=lambda: "ok")

    bulk = Toolkit(name="bulk")
    for index in range(count):
        bulk.functions[f"bulk_{index}"] = bulk_function(index)
    return _search_toolkit(_deferred(bulk, "Bulk operations"))


def test_tool_search_listing_leaves_out_descriptions_before_functions() -> None:
    """A listing too large with descriptions lists every function by name and toolkit instead."""
    result = _bulk_search_toolkit(200, "x" * 200).tool_search()
    payload = _tool_payload(result)

    assert len(result.encode()) <= 16 * 1024
    assert payload["functions"][:2] == [{"name": "bulk_0", "toolkit": "bulk"}, {"name": "bulk_1", "toolkit": "bulk"}]
    assert len(payload["functions"]) == 200
    assert "Descriptions were left out" in payload["message"]


def test_tool_search_listing_stops_at_the_size_limit() -> None:
    """A listing too large even without descriptions names how many functions it left out."""
    result = _bulk_search_toolkit(1000, "Bulk operation.").tool_search("")
    payload = _tool_payload(result)

    assert len(result.encode()) <= 16 * 1024
    assert 0 < len(payload["functions"]) < 1000
    assert "description" not in payload["functions"][0]
    assert f"{len(payload['functions'])} of 1000" in payload["message"]


def test_tool_search_lists_functions_for_a_query_without_words() -> None:
    """A query with no searchable words, such as a wildcard, lists the deferred functions."""
    toolkit = _search_toolkit(_deferred(_WeatherTools(), "Forecasts"))

    assert _tool_payload(toolkit.tool_search("*"))["functions"] == [
        {"name": "forecast", "toolkit": "weather", "description": "Return the forecast for a city."},
    ]


def test_tool_search_without_matches_suggests_other_keywords() -> None:
    """An empty result tells the model how to search again."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"))

    payload = _tool_payload(toolkit.tool_search("spreadsheet"))

    assert payload["matches"] == []
    assert "toolkit name" in payload["message"]
    assert "tool_search() without a query" in payload["message"]


def test_tool_search_shows_the_deferred_functions_the_agent_offers_when_it_searches() -> None:
    """Search reads the deferred toolkits when it runs, after the agent left out functions such as MCP collisions."""
    full = _deferred(_CrmTools(), "Customer records")
    offered = [full]
    toolkit = DynamicToolsToolkit(deferred_toolkits=lambda: offered)
    offered[0] = DeferredToolkit(full.deferral, tuple(f for f in full.functions if f.name != "crm_lookup"))

    payload = _tool_payload(toolkit.tool_search("crm"))

    assert [match["name"] for match in payload["matches"]] == ["crm_notes"]


def test_call_tool_fallback_returns_retry_guidance() -> None:
    """An unresolved call_tool call returns guidance instead of running anything."""
    toolkit = _search_toolkit(_deferred(_CrmTools(), "Customer records"))

    payload = _tool_payload(toolkit.call_tool("crm_lookpu", {"customer_id": 7}))

    assert payload["status"] == "error"
    assert "'crm_lookpu' was not called" in payload["message"]
    assert "tool_search" in payload["message"]


def test_call_tool_schema_accepts_arbitrary_argument_values() -> None:
    """call_tool's arguments must allow any JSON values, or schema-enforcing servers cannot emit real calls."""
    function = _search_toolkit().functions["call_tool"].model_copy(deep=True)
    function.process_entrypoint(strict=False)

    arguments = function.parameters["properties"]["arguments"]
    object_schema = next(option for option in arguments.get("anyOf", [arguments]) if option.get("type") == "object")
    assert object_schema.get("additionalProperties", True) is True
    assert "properties" not in object_schema


def test_call_tool_schema_converts_to_a_plain_gemini_object() -> None:
    """Gemini conversion must not invent placeholder argument keys."""
    convert_schema = pytest.importorskip("agno.utils.gemini").convert_schema
    function = _search_toolkit().functions["call_tool"].model_copy(deep=True)
    function.process_entrypoint(strict=False)

    converted = convert_schema(function.parameters)

    assert converted is not None
    assert "example_key" not in str(converted)
    assert converted.properties is not None
    assert "JSON object" in (converted.properties["arguments"].description or "")


class _DualTools(Toolkit):
    """Registers a sync and an async implementation under one name, as async-capable toolkits do."""

    def __init__(self) -> None:
        super().__init__(name="dual", tools=[self.lookup])
        self.async_functions["lookup"] = Function.from_callable(self.lookup_async, name="lookup")

    def lookup(self, customer_id: int) -> str:
        """Look up a customer synchronously."""
        return str(customer_id)

    async def lookup_async(self, account_id: int) -> str:
        """Look up an account asynchronously."""
        return str(account_id)


def test_tool_search_describes_the_async_implementation_agno_runs() -> None:
    """Agno prefers async implementations in async runs, so search must describe that one."""
    toolkit = _search_toolkit(_deferred(_DualTools(), "Dual"))

    match = _tool_payload(toolkit.tool_search("lookup"))["matches"][0]

    assert set(match["parameters"]["properties"]) == {"account_id"}


class _PingTools(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="ping", tools=[self.ping])
        self.functions["ping"].instructions = "Only ping hosts the user named."

    def ping(self, host: str) -> str:
        """Ping a host."""
        return host


def test_tool_search_returns_function_instructions_kept_out_of_the_prompt() -> None:
    """Per-function guidance a deferred function would render arrives with its search result instead."""
    pings = _PingTools()
    toolkit = _search_toolkit(_deferred(pings, "Network checks"))

    match = _tool_payload(toolkit.tool_search("ping"))["matches"][0]

    assert match["instructions"] == "Only ping hosts the user named."
    assert pings.functions["ping"].add_instructions is False


class _GithubTools(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="github", tools=[self.close_issue, self.assign_issue])

    def close_issue(self, number: int) -> str:
        """Close an issue."""
        return str(number)

    def assign_issue(self, number: int, user: str) -> str:
        """Assign an issue to a user."""
        return f"{number} {user}"


class _JiraTools(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="jira", tools=[self.create_issue])

    def create_issue(self, summary: str) -> str:
        """Create an issue."""
        return summary


def test_tool_search_ranks_functions_matching_more_query_words_first() -> None:
    """A natural-language query naming the toolkit and the action finds that toolkit's function first."""
    toolkit = _search_toolkit(_deferred(_GithubTools(), "GitHub"), _deferred(_JiraTools(), "Jira"))

    matches = _tool_payload(toolkit.tool_search("jira create issue"))["matches"]

    assert (matches[0]["toolkit"], matches[0]["name"]) == ("jira", "create_issue")


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [("openai", "gpt-4o-mini"), ("anthropic", "claude-opus-5")],
)
@pytest.mark.parametrize("deferred_first", [False, True])
@pytest.mark.asyncio
async def test_config_order_decides_which_same_named_function_is_registered(
    tmp_path: Path,
    provider: str,
    model_id: str,
    *,
    deferred_first: bool,
) -> None:
    """Agno registers the first toolkit's function for a name; deferral follows that owner and search offers only it."""
    raw = _base_config_data()
    raw["models"]["default"] = {"provider": provider, "id": model_id}  # type: ignore[index]
    tools: list[object] = ["file", {"coding": {"defer": True}}]
    raw["agents"]["code"]["tools"] = tools[::-1] if deferred_first else tools  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert ("read_file" in agent.request_template.deferred_tools.names) is deferred_first
    if "tool_search" in _function_names(agent):
        matches = (await _search(agent, "read_file"))["matches"]
        offered = [match["toolkit"] for match in matches if match["name"] == "read_file"]
        assert offered == (["coding"] if deferred_first else [])


def test_explicit_dynamic_tools_entry_is_ignored_where_call_tool_cannot_be_rewritten(tmp_path: Path) -> None:
    """Native tool search never rewrites call_tool, so an authored dynamic_tools entry must not offer it."""
    raw = _base_config_data()
    raw["models"]["claude"] = {"provider": "anthropic", "id": "claude-opus-5"}  # type: ignore[index]
    raw["agents"]["code"]["model"] = "claude"  # type: ignore[index]
    raw["agents"]["code"]["tools"] = [{"sleep": {"defer": True}}, "dynamic_tools"]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    function_names = _function_names(agent)
    assert "call_tool" not in function_names
    assert "tool_search" not in function_names


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [("openai", "gpt-4o-mini"), ("anthropic", "claude-opus-5")],
)
def test_static_prompt_estimate_leaves_out_hidden_deferred_definitions(
    tmp_path: Path,
    provider: str,
    model_id: str,
) -> None:
    """The history budget must not charge definitions that tool search keeps off the request."""

    def estimate(tools: list[object]) -> int:
        raw = _base_config_data()
        raw["models"]["default"] = {"provider": provider, "id": model_id}  # type: ignore[index]
        raw["agents"]["code"]["tools"] = tools  # type: ignore[index]
        config = _validated_config(tmp_path, raw)
        agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)
        return build_static_token_estimator(
            prompt_for_turn(agent),
            prepare_run_tools(agent.spec).specs(),
            agent.request_template.deferred_tools.names,
        ).non_prompt_tokens()

    base = estimate(["sleep"])
    eager = estimate(["sleep", "github"])
    deferred = estimate(["sleep", {"github": {"defer": True}}])

    assert deferred - base < (eager - base) / 4


def test_tool_search_exact_name_match_ignores_case() -> None:
    """MCP functions often use camelCase, and an exact name query must outrank a function sharing its word."""
    camel = Toolkit(
        name="camel",
        tools=[
            Function.from_callable(lambda number: str(number), name="getIssue_v2"),
            Function.from_callable(lambda number: str(number), name="getIssue"),
        ],
    )
    toolkit = _search_toolkit(_deferred(camel, "Camel"))

    matches = _tool_payload(toolkit.tool_search("getIssue"))["matches"]

    assert matches[0]["name"] == "getIssue"


def test_tool_search_joins_instructions_of_toolkits_sharing_one_entry() -> None:
    """One authored entry can expand to several toolkits; none of their instructions may be dropped."""
    first = Toolkit(
        name="first",
        tools=[Function.from_callable(lambda: "a", name="alpha_tool")],
        instructions="Use alpha.",
    )
    second = Toolkit(
        name="second",
        tools=[Function.from_callable(lambda: "b", name="beta_tool")],
        instructions="Use beta.",
    )
    first.add_instructions = second.add_instructions = True
    toolkit = _search_toolkit(_deferred(first, "Shared", "shared"), _deferred(second, "Shared", "shared"))

    payload = _tool_payload(toolkit.tool_search("shared"))

    assert payload["instructions"] == {"shared": "Use alpha.\n\nUse beta."}


def test_call_tool_fallback_accepts_non_object_arguments() -> None:
    """An unresolved call with list arguments still gets the retry guidance, not a validation error."""
    function = _search_toolkit().functions["call_tool"]

    result = function.entrypoint(name="crm_lookup", arguments=[7])  # type: ignore[misc]

    assert "'crm_lookup' was not called" in _tool_payload(result)["message"]


def test_search_toolkit_construction_failure_does_not_stop_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like every other toolkit, a failing search toolkit is skipped instead of failing agent creation."""

    def broken(**_kwargs: object) -> DynamicToolsToolkit:
        msg = "synthetic construction failure"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.custom_tools.dynamic_tools.DynamicToolsToolkit", broken)
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", {"calculator": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert "sleep" in _toolkit_names(agent)
    assert "dynamic_tools" not in _toolkit_names(agent)


def test_call_tool_wire_definition_has_a_description() -> None:
    """Providers read the wrapper's description, and some (Cerebras) fail without one."""
    function = _search_toolkit().functions["call_tool"].model_copy(deep=True)
    function.process_entrypoint(strict=False)

    assert function.to_dict().get("description")


def test_call_tool_fallback_tolerates_missing_name_and_extra_keys() -> None:
    """Malformed wrapper calls still get the retry guidance instead of a Python signature error."""
    entrypoint = _search_toolkit().functions["call_tool"].entrypoint
    assert entrypoint is not None

    assert "tool_search" in _tool_payload(entrypoint(customer_id=7))["message"]
    assert "'crm_lookpu' was not called" in _tool_payload(entrypoint(name="crm_lookpu", customer_id=7))["message"]


def test_search_toolkit_results_are_never_redirected_to_output_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Definitions must reach the model inline, so the search toolkit skips output-file wrapping."""
    wrapped: list[str] = []
    original = agents_module.wrap_toolkit_for_output_files

    def recording_wrap(toolkit: Toolkit, policy: object) -> Toolkit:
        wrapped.append(toolkit.name)
        return original(toolkit, policy)  # type: ignore[arg-type]

    monkeypatch.setattr(agents_module, "wrap_toolkit_for_output_files", recording_wrap)
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", {"calculator": {"defer": True}}]  # type: ignore[index]
    config = _validated_config(tmp_path, raw)

    agent = create_agent("code", config, _runtime_paths(tmp_path), execution_identity=None)

    assert "dynamic_tools" in _toolkit_names(agent)
    assert "dynamic_tools" not in wrapped
