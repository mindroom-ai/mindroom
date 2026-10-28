"""Which toolkits persona, minimal, and identity-less builds offer, and how saved tool work rechecks its toolkit."""

# Already pinned elsewhere, so left out here (paths under tests/):
# - test_agent_tool_offer.py::test_one_build_offers_the_toolkits_config_and_turn_decide[room-tools-need-a-room]
# - test_invite_router_tool.py::test_invite_router_stays_hidden_without_matrix_room_context
# - test_invite_router_tool.py::test_matrix_runtime_ignores_authored_invite_router_function_filters
# - test_agent_personas.py::test_empty_persona_tools_build_toolless_agent
# - test_agent_personas.py::test_persona_may_name_the_matrix_room_runtime_tool
# - test_tool_job_authorization.py::test_disabling_graph_edits_revokes_a_retained_graph_edit (graph edits off vs on)
# - test_cli_approval_recovery.py::test_approval_tool_restore_requires_current_tool_search_assignment (kept, removed)
# - test_dynamic_toolkits.py pins deferred owner membership, but not order or owner names, in
#   ::test_attached_deferred_concrete_tool_preserves_override_over_preset_child,
#   ::test_attached_parent_keeps_implied_child_when_child_is_separately_deferred,
#   ::test_deferred_concrete_tool_gates_implied_children_over_preset_in_any_order

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agents import build_agent_toolkit, create_agent
from mindroom.approval.tool_owners import required_approval_tool_names
from mindroom.config.main import Config
from mindroom.delegation.state import SubagentPersona
from mindroom.event_journal import ApprovalCall
from mindroom.tool_jobs.authorization import authority_snapshot, locally_allowed, toolkit_authority
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.sandbox_proxy import tool_placement
from tests.identity_helpers import persist_entity_accounts
from tests.test_agent_tool_offer import _identity, _runtime_paths
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.config.memory import MemoryBackend
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _owned(owner: str, names: str) -> str:
    return " ".join(f"{owner}:{name}" for name in names.split())


_IDENTITIES: dict[str, ToolExecutionIdentity | None] = {
    "room": _identity(room=True),
    "roomless": _identity(room=False),
    "none": None,
    "api-room": replace(_identity(room=True), channel="openai_compat"),
}
_CALC = {"calculator": {"defer": True}}
_MESSAGE = {"matrix_message": {"defer": True}}
_SHELL = {"shell": {"defer": True}}
_CALC_TOOLS = ["shell", _CALC, {"file": {"defer": True, "initial": True}}]
_MESSAGE_TOOLS = ["shell", _MESSAGE]
_TRIO = "matrix_message attachments matrix_room"
_OPENCLAW = "coding duckduckgo website browser scheduler"
_OPENCLAW_ALL = _owned("openclaw_compat", f"{_OPENCLAW} {_TRIO}")
_FILE_CALC = {"tools": ["file", _CALC]}
_MESSAGES = {"tools": _MESSAGE_TOOLS}
_MESSAGE_ATTACH = {"tools": ["matrix_message", {"attachments": {"defer": True}}]}
_MEMORY = {"tools": ["memory"], "memory_backend": "mem0"}
_ROOM_TOOL = {"tools": ["matrix_message"]}
_DELEGATE = {"delegate_to": ["helper"]}
_AUTHORED_DELEGATE = {"tools": ["delegate"], "delegate_to": ["helper"]}
_REQUIRE_CALC = {"required_tool_names": ("calculator",)}
_REQUIRE_MESSAGE = {"required_tool_names": ("matrix_message",)}
_EAGER = {"eager_deferred_tools": True}


def _config(tmp_path: Path, agent: dict[str, Any]) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    data = {
        "defaults": {"tools": [], "allow_self_config": False},
        "agents": {"code": {"display_name": "Code", "role": "Write code", **agent}, "helper": {"display_name": "H"}},
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
    }
    config = Config.validate_with_runtime(data, runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    return config, runtime_paths


def _build(tmp_path: Path, agent: dict[str, Any], identity: str = "room", **options: Any) -> AgentBuild:  # noqa: ANN401
    config, paths = _config(tmp_path, agent)
    return create_agent(
        "code",
        config,
        paths,
        _IDENTITIES[identity],
        knowledge=None,
        model=turn_model(config, paths, "code"),
        **options,
    )


def _offered(build: AgentBuild) -> str:
    """Return the registered tools the build's offered functions come from, sorted."""
    names = {str(function.registered_toolkit) for toolkit in build.spec.toolkits for function in toolkit.functions}
    return " ".join(sorted(names))


@pytest.mark.parametrize(
    ("agent", "persona_tools", "expected"),
    [
        pytest.param({"tools": ["shell", "file", _CALC]}, ("shell", "calculator"), "calculator shell", id="deferred"),
        pytest.param(_MESSAGES, ("matrix_message", "attachments"), "attachments matrix_message", id="implied"),
        pytest.param(_FILE_CALC, ("calculator.add",), "calculator", id="function-entry"),
        pytest.param(_DELEGATE, ("delegate",), "delegate", id="injected-delegate"),
        pytest.param(_FILE_CALC, None, "calculator dynamic_tools file invite_router", id="no-tool-list"),
    ],
)
def test_a_persona_tool_list_builds_exactly_the_named_toolkits_eagerly(
    tmp_path: Path,
    agent: dict[str, Any],
    persona_tools: tuple[str, ...] | None,
    expected: str,
) -> None:
    """A persona tool list builds only the toolkits it names, deferred ones eager, without search or invite_router."""
    persona = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=persona_tools)
    build = _build(tmp_path, agent, persona=persona)
    assert _offered(build) == expected
    assert bool(build.request_template.deferred_tools.names) is (persona_tools is None)


@pytest.mark.parametrize(
    ("tools", "identity", "options", "attached", "deferred"),
    [
        pytest.param(_CALC_TOOLS, "room", {}, "file invite_router shell", "calculator", id="initial"),
        pytest.param(_CALC_TOOLS, "room", _REQUIRE_CALC, "calculator file invite_router shell", "", id="required"),
        pytest.param(_CALC_TOOLS, "room", _EAGER, "calculator file invite_router shell", "", id="eager"),
        pytest.param(_MESSAGE_TOOLS, "room", {}, "invite_router shell", _TRIO, id="implied"),
        pytest.param(
            _MESSAGE_TOOLS,
            "room",
            _REQUIRE_MESSAGE,
            "attachments invite_router matrix_message matrix_room shell",
            "",
            id="required-implied",
        ),
        pytest.param(_MESSAGE_TOOLS, "roomless", {}, "shell", "", id="roomless-hides"),
        pytest.param(_MESSAGE_TOOLS, "none", {}, "shell", _TRIO, id="identity-less-keeps"),
    ],
)
def test_minimal_mode_attaches_initial_required_or_eager_deferred_toolkits_and_defers_the_rest(
    tmp_path: Path,
    tools: list[object],
    identity: str,
    options: dict[str, Any],
    attached: str,
    deferred: str,
) -> None:
    """Minimal mode attaches initial, required, or eager deferred toolkits; the rest reach the agent CLI in order."""
    build = _build(tmp_path, {"tools": tools}, identity, agent_mode="minimal", **options)
    assert _offered(build) == attached
    assert " ".join(toolkit.name for toolkit in build.surface.deferred) == deferred


@pytest.mark.parametrize("identity", ["room", "roomless", "none"])
def test_the_minimal_roster_names_every_surface_toolkit_whatever_the_turn(tmp_path: Path, identity: str) -> None:
    """The minimal roster lists the whole authored surface, never invite_router, even room tools a roomless turn hid."""
    build = _build(tmp_path, {"tools": _MESSAGE_TOOLS}, identity, agent_mode="minimal")
    # today: deferred toolkits appear twice in toolkit_names; the rendered roster deduplicates them.
    assert sorted(set(build.surface.toolkit_names)) == ["attachments", "matrix_message", "matrix_room", "shell"]


@pytest.mark.parametrize(
    ("identity", "tools", "expected"),
    [
        pytest.param("none", ["matrix_message"], "attachments matrix_message matrix_room", id="identity-less"),
        pytest.param("api-room", ["matrix_message"], "attachments matrix_message matrix_room", id="non-matrix-room"),
        # today: an authored invite_router entry survives where the runtime one is never injected.
        pytest.param("none", ["invite_router", "calculator"], "calculator invite_router", id="identity-less-authored"),
        pytest.param("api-room", ["invite_router", "calculator"], "calculator invite_router", id="non-matrix-authored"),
        pytest.param("roomless", ["invite_router", "calculator"], "calculator", id="roomless-authored"),
    ],
)
def test_room_context_tools_need_only_a_room_and_invite_router_a_matrix_room(
    tmp_path: Path,
    identity: str,
    tools: list[object],
    expected: str,
) -> None:
    """Only an identity without a room hides room-context tools; only a Matrix room turn injects invite_router."""
    assert _offered(_build(tmp_path, {"tools": tools}, identity)) == expected


@pytest.mark.parametrize(
    ("agent", "options", "expected"),
    [
        pytest.param({"tools": ["openclaw_compat", _SHELL]}, {}, f"openclaw_compat shell {_OPENCLAW_ALL}", id="after"),
        pytest.param({"tools": [_SHELL, "openclaw_compat"]}, {}, f"shell openclaw_compat {_OPENCLAW_ALL}", id="before"),
        pytest.param(
            {"tools": ["openclaw_compat", _MESSAGE]},
            {},
            "openclaw_compat matrix_message "
            f"{_owned('openclaw_compat', f'shell {_OPENCLAW}')} {_owned('matrix_message', 'attachments matrix_room')}",
            id="implier-after-preset",
        ),
        pytest.param(_MESSAGE_ATTACH, {}, "matrix_message attachments matrix_message:matrix_room", id="attached-owns"),
        # today: an unattached deferred entry hides the toolkit an eager tool implies.
        pytest.param(
            _MESSAGE_ATTACH,
            {"deferred": []},
            "matrix_message matrix_message:matrix_room",
            id="unattached",
        ),
        pytest.param(
            {"tools": ["calculator", {"sleep": {"defer": True}}], "delegate_to": ["helper"], "allow_self_config": True},
            {"portable_search": True, "matrix_room_tools": True},
            "calculator sleep delegate self_config dynamic_tools invite_router",
            id="injected-after-authored",
        ),
        pytest.param(
            {"tools": ["calculator", "dynamic_tools", {"sleep": {"defer": True}}]},
            {"portable_search": True},
            "calculator dynamic_tools sleep",
            id="authored-search-keeps-its-place",
        ),
    ],
)
def test_the_surface_lists_each_toolkit_with_its_owner_in_available_tool_order_then_injected_tools(
    tmp_path: Path,
    agent: dict[str, Any],
    options: dict[str, Any],
    expected: str,
) -> None:
    """Each surface entry reads ``owner:name`` when an authored preset or implier owns it, else its own name."""
    config, _ = _config(tmp_path, agent)
    candidates = resolve_agent_tools(config, "code").candidates(**options)
    entries = [(entry.name, entry.authored_name or entry.name) for entry in candidates]
    assert " ".join(name if owner == name else f"{owner}:{name}" for name, owner in entries) == expected


@pytest.mark.parametrize(
    ("room_runtime", "expected"),
    [
        pytest.param(True, [("calculator", {}), ("invite_router", {})], id="matrix-room-replaces"),
        pytest.param(False, [("invite_router", {"exclude_tools": ["invite_router"]}), ("calculator", {})], id="kept"),
    ],
)
def test_a_matrix_room_turn_moves_an_authored_invite_router_to_the_end_without_its_filters(
    tmp_path: Path,
    room_runtime: bool,
    expected: list[tuple[str, dict[str, object]]],
) -> None:
    """On a Matrix room turn the authored invite_router entry gives way to the injected one, appended last."""
    config, _ = _config(tmp_path, {"tools": [{"invite_router": {"exclude_tools": ["invite_router"]}}, "calculator"]})
    candidates = resolve_agent_tools(config, "code").candidates(matrix_room_tools=room_runtime)
    assert [(entry.name, dict(entry.tool_config_overrides)) for entry in candidates] == expected


@pytest.mark.parametrize(
    ("agent", "toolkit", "function", "options", "expected"),
    [
        pytest.param(_MEMORY, "memory", "add_memory", {}, True, id="memory-on"),
        pytest.param(_MEMORY, "memory", "add_memory", {"memory_backend": "none"}, False, id="memory-none"),
        pytest.param(_ROOM_TOOL, "matrix_message", "matrix_message", {}, True, id="room-context-in-room"),
        pytest.param(_ROOM_TOOL, "matrix_message", "matrix_message", {"room": False}, False, id="roomless"),
        pytest.param(_DELEGATE, "delegate", "run_subagent", {}, True, id="delegate-depth-0"),
        pytest.param(_DELEGATE, "delegate", "run_subagent", {"depth": 3}, False, id="delegate-at-max-depth"),
        pytest.param(_AUTHORED_DELEGATE, "delegate", "run_subagent", {"depth": 3}, False, id="authored-delegate"),
    ],
)
def test_retained_tool_work_is_allowed_only_while_its_toolkit_rule_still_holds(
    tmp_path: Path,
    agent: dict[str, Any],
    toolkit: str,
    function: str,
    options: dict[str, Any],
    expected: bool,
) -> None:
    """Saved tool work rechecks the memory backend, the room, and the delegation depth its toolkit needs."""
    assert _locally_allowed(tmp_path, agent, toolkit, function, **options) is expected


def _locally_allowed(
    tmp_path: Path,
    agent: dict[str, Any],
    toolkit: str,
    function: str,
    *,
    room: bool = True,
    depth: int = 0,
    memory_backend: MemoryBackend | None = None,
) -> bool:
    """Build ``toolkit`` in a room, then recheck one call of it under the given turn and memory backend."""
    config, runtime_paths = _config(tmp_path, agent)
    built = build_agent_toolkit(
        toolkit,
        agent_tools=resolve_agent_tools(config, "code"),
        placement=tool_placement(runtime_paths),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity(room=True),
    )
    assert built is not None
    assert function in {*built.get_functions(), *built.get_async_functions()}
    grant = toolkit_authority(authority_snapshot(config, "code"), built)
    if memory_backend is not None:
        config.agents["code"].memory_backend = memory_backend
    owner = _identity(room=room)
    return locally_allowed(
        config,
        owner,
        tool_name=function,
        toolkit_name=toolkit,
        origin={},
        depth=depth,
        authority=grant,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "toolkit", "identity", "depth", "expected"),
    [
        pytest.param(_ROOM_TOOL, "matrix_message", "room", 0, ("matrix_message",), id="room-context-in-room"),
        pytest.param(_ROOM_TOOL, "matrix_message", "roomless", 0, "requires room context", id="room-context-roomless"),
        # The preset's other toolkits need no room, so its shell's approvals restore without one.
        pytest.param(
            {"tools": ["openclaw_compat"]},
            "openclaw_compat",
            "roomless",
            0,
            ("openclaw_compat",),
            id="preset",
        ),
        pytest.param({}, "invite_router", "roomless", 0, "requires room context", id="room-runtime-roomless"),
        # Only a Matrix room turn offers the Matrix room runtime tools.
        pytest.param({}, "invite_router", "api-room", 0, "requires room context", id="room-runtime-non-matrix-room"),
        pytest.param(
            {"tools": ["memory"], "memory_backend": "none"},
            "memory",
            "room",
            0,
            "no longer permitted",
            id="memory-none",
        ),
        pytest.param(
            {"tools": ["knowledge_graph"]},
            "knowledge_graph",
            "room",
            0,
            "no longer permitted",
            id="graph-off",
        ),
        pytest.param(_DELEGATE, "delegate", "room", 0, ("delegate",), id="delegate"),
        pytest.param(_DELEGATE, "delegate", "room", 3, "no longer permitted", id="delegate-at-max-depth"),
    ],
)
async def test_a_saved_approval_restores_only_toolkits_its_agent_may_still_use(
    tmp_path: Path,
    agent: dict[str, Any],
    toolkit: str,
    identity: str,
    depth: int,
    expected: tuple[str, ...] | str,
) -> None:
    """A saved approval's toolkit restores while the current surface permits it, else refuses with the reason."""
    config, paths = _config(tmp_path, agent)
    owner = _IDENTITIES[identity]
    assert owner is not None
    calls = (ApprovalCall("call", "function", "code", 100, toolkit_name=toolkit),)
    restored = required_approval_tool_names(
        "code",
        calls,
        config=config,
        runtime_paths=paths,
        execution_identity=owner,
        delegation_depth=depth,
    )
    if isinstance(expected, str):
        with pytest.raises(RuntimeError, match=expected):
            await restored
    else:
        assert await restored == expected
