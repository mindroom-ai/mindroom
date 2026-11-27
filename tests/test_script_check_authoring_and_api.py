"""Tests for triggers(action="watch_script"): authoring guards, router relay, metadata-only update, and voice responder."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import apply_tool_approval_capability, create_agent
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.constants import (
    AUTOMATION_ORIGIN_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTE_EVENT_TYPE,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    resolve_primary_runtime_paths,
)
from mindroom.custom_tools.desktop import DesktopTools
from mindroom.custom_tools.triggers import _ACTION_ALLOWED_PARAMS, _WATCH_TURN_MUTATION_ERROR, TriggersTools
from mindroom.delegation.state import SubagentPersona
from mindroom.dispatch_source import SCHEDULED_SOURCE_KIND, TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_sources import event_source_hook_source
from mindroom.hooks import HookBindings, HookRegistry
from mindroom.ingress_validation import IngressValidator
from mindroom.journal_dispatch import _route_as_message
from mindroom.matrix_rtc.call_tools import _TEXT_CHAT_REQUIRED_MESSAGE, _refuse_text_chat_functions
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers.models import TriggerEvent
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.router.relay import _router_handoff_extra_content
from mindroom.script_runs.manager import ScriptRunManager
from mindroom.script_runs.store import ScriptRunStore
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.invocation import DETACHED_SESSION_ID, ToolCallContext, run_tool_call
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate, ToolOutcome
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.tools.triggers import _NON_CODE_TRIGGER_ACTIONS
from mindroom.turn_model import ModelScope, resolve_turn_model
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    parsed_route,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_OWNER = "@alice:example.org"
_OTHER = "@admin:example.org"
_ROOM = "!room:example.org"
_SRC = "def check():\n    return None\n"


def _make_ctx(
    tmp_path: Path,
    *,
    agent_name: str = "coder",
    requester_id: str = _OWNER,
    allow_local: bool = True,
    with_manager: bool = True,
    joined_users: tuple[str, ...] = (_OWNER,),
    tool_approval: ToolApprovalConfig | None = None,
    **ctx_kwargs: object,
) -> tuple[ToolRuntimeContext, MagicMock]:
    storage = tmp_path / "data"
    env = {"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"}
    if allow_local:
        env["MINDROOM_ALLOW_UNSAFE_LOCAL_SCRIPTS"] = "1"
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage, process_env=env)
    cfg = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"allow_shell_without_worker": allow_local},
                "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
                "agents": {
                    "coder": {
                        "display_name": "Coder",
                        "rooms": [_ROOM],
                        "tools": ["triggers", "script"],
                        "model": "default",
                    },
                    "watcher": {"display_name": "Watcher", "rooms": [_ROOM], "tools": ["triggers"], "model": "default"},
                },
                "teams": {
                    "ops_team": {
                        "display_name": "Ops",
                        "role": "Ops team",
                        "agents": ["coder", "watcher"],
                        "rooms": [_ROOM],
                        "mode": "coordinate",
                    },
                },
                "administrators": [_OTHER],
                "external_trigger_policy": {"enabled": True, "admin_users": [_OTHER]},
            },
        ),
        paths,
    )
    if tool_approval is not None:
        cfg = bind_runtime_paths(cfg.model_copy(update={"tool_approval": tool_approval}), paths)
    notify = MagicMock()
    manager = (
        ScriptRunManager(
            store=ScriptRunStore(paths),
            broker=SimpleNamespace(),  # type: ignore[arg-type]
            worker_client=ScriptWorkerClient(),
            worker_backend=None,
            gateway_url="http://127.0.0.1:8765",
        )
        if with_manager
        else None
    )
    orch = SimpleNamespace(
        config=cfg,
        reactive_triggers=SimpleNamespace(
            notify_work=notify,
            notify_github_pr_rules_changed=MagicMock(),
            notify_script_check_rules_changed=notify,
            event_sources=None,
        ),
        script_runtime=SimpleNamespace(tool_manager=manager),
        event_sources=None,
        agent_bots={},
    )
    client = make_matrix_client_mock(user_id=f"@mindroom_{agent_name}:example.org")
    client.rooms = {_ROOM: SimpleNamespace(users=dict.fromkeys(joined_users))}
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[{"type": "m.room.member", "state_key": u, "content": {"membership": "join"}} for u in joined_users],
            room_id=_ROOM,
        ),
    )
    memberships = AgentReplyMembershipIndex()
    session_id = ctx_kwargs.pop("session_id", "session-1")  # type: ignore[arg-type]
    target = MessageTarget(
        room_id=_ROOM,
        source_thread_id="$thread:example.org",
        resolved_thread_id="$thread:example.org",
        reply_to_event_id="$e:example.org",
        session_id=session_id or "placeholder",
    )
    if session_id is None:
        object.__setattr__(target, "session_id", None)
    ctx = make_test_tool_runtime_context(
        agent_name=agent_name,
        target=target,
        requester_id=requester_id,
        client=client,
        config=cfg,
        runtime_paths=runtime_paths_for(cfg),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hooks=HookBindings(registry=HookRegistry.empty()),
        orchestrator=orch,
        agent_reply_memberships=memberships,
        **ctx_kwargs,  # type: ignore[arg-type]
    )
    return ctx, notify


async def _call(tool: TriggersTools, ctx: ToolRuntimeContext, **kwargs: object) -> dict[str, Any]:
    with tool_runtime_context(ctx):
        return json.loads(await tool.triggers(**kwargs))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_watch_script_create_source_path_team_target_and_authoring_guards(tmp_path: Path) -> None:
    """watch_script supports inline/path source & team targets while enforcing capability, direct-agent, and context guards."""
    tool = TriggersTools()
    ctx, notify = _make_ctx(tmp_path / "ok")

    res = await _call(
        tool,
        ctx,
        action="watch_script",
        trigger_id="sc_team",
        prompt="Notify ops",
        source=_SRC,
        target_agent="ops_team",
        poll_interval_seconds=120,
    )
    assert (res["status"], res["trigger"]["source"]["agent"], res["trigger"]["target"]["agent"], notify.call_count) == (
        "ok",
        "coder",
        "ops_team",
        1,
    )

    ws_file = ctx.agent_workspace_root(create=True) / "check_feed.py"
    ws_file.write_text("def check():\n    return {'found': 1}\n")
    res_path = await _call(tool, ctx, action="watch_script", trigger_id="sc_path", prompt="Ping", path="check_feed.py")
    assert (res_path["status"], res_path["trigger"]["source"]["source_digest"]) == (
        "ok",
        hashlib.sha256(ws_file.read_bytes()).hexdigest(),
    )

    # Direct-agent-only authoring (Astra #5): team-transport authoring (recipient != agent_name) is rejected
    ctx_team, _ = _make_ctx(tmp_path / "team_auth", transport_agent_name="ops_team")
    err_team = await _call(tool, ctx_team, action="watch_script", prompt="Alert", source=_SRC)
    assert err_team["status"] == "error"
    assert "direct agent turn" in err_team["message"]

    guard_cases: list[tuple[dict[str, Any], str]] = [
        ({"agent_name": "watcher"}, "requires 'triggers' plus 'script' or 'workflow'"),
        ({"allow_local": False}, "Route this agent to a worker"),
        ({"tool_function_filter": lambda _f: True}, "tool-filtered"),
        (
            {
                "persona": SubagentPersona(
                    source_kind="configured",
                    source_name="p",
                    system_prompt="s",
                    tools=("triggers",),
                ),
            },
            "tool-restricted persona",
        ),
        ({"correlation_id": "background-script:1"}, "background script or workflow"),
        ({"session_id": DETACHED_SESSION_ID}, "detached"),
        ({"with_manager": False}, "not ready"),
    ]
    for idx, (kwargs, needle) in enumerate(guard_cases):
        bad_ctx, _ = _make_ctx(tmp_path / f"g_{idx}", **kwargs)
        err = await _call(tool, bad_ctx, action="watch_script", prompt="Alert", source=_SRC)
        assert err["status"] == "error"
        assert needle in err["message"]


@pytest.mark.asyncio
async def test_automation_router_relay_and_event_source_watch_turn_guards(tmp_path: Path) -> None:
    """Direct automation, router-routed scheduled turns (Astra #3), and event_source watch turns (#4423) block watch_script."""
    tool = TriggersTools()
    for kind in ("external_trigger", "reactive_trigger", "scheduled", "hook_dispatch"):
        ctx_auto, _ = _make_ctx(tmp_path / f"auto_{kind}", source_kind=kind)
        res = await _call(tool, ctx_auto, action="watch_script", prompt="Alert", source=_SRC)
        assert res["status"] == "error"
        assert "unattended automation" in res["message"]

    # Astra #3: Router relay preserves SOURCE_KIND_KEY=trusted_internal_relay AND carries AUTOMATION_ORIGIN_KEY
    ctx_base, _ = _make_ctx(tmp_path / "relay")
    cfg, paths = ctx_base.config, ctx_base.runtime_paths
    ids = entity_identity_registry(cfg, paths)
    router_uid = ids.current_id(ROUTER_AGENT_NAME).full_id
    ingress = IngressValidator(
        SimpleNamespace(
            agent_name="coder",
            matrix_id=ids.current_id("coder"),
            runtime=SimpleNamespace(config=cfg),
            runtime_paths=paths,
            logger=MagicMock(),
            resolver=SimpleNamespace(),
            turn_store=SimpleNamespace(),
        ),  # type: ignore[arg-type]
    )
    relay_deps = SimpleNamespace(ingress=ingress)

    sched_event = SimpleNamespace(
        sender=router_uid,
        source={
            "content": {"body": "Run scheduled", SOURCE_KIND_KEY: SCHEDULED_SOURCE_KIND, ORIGINAL_SENDER_KEY: _OWNER},
        },
    )
    routed_sched = _router_handoff_extra_content(
        relay_deps,  # type: ignore[arg-type]
        event=sched_event,  # type: ignore[arg-type]
        extra_content={ORIGINAL_SENDER_KEY: _OWNER},
        requester_user_id=_OWNER,
        thread_event_id="$sched_root",
    )
    assert (routed_sched[SOURCE_KIND_KEY], routed_sched[AUTOMATION_ORIGIN_KEY]) == (
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
    )
    coder_uid = ids.current_id("coder").full_id
    sched_route_msg = _route_as_message(
        parsed_route(
            {
                **routed_sched,
                ROUTE_EVENT_TYPE: {"entity": coder_uid, "event_ids": ["$sched_ev"], "thread_id": "$sched_root"},
                "body": "Run scheduled",
            },
            sender=router_uid,
            event_id="$route_sched",
        ),
    )
    assert sched_route_msg.source["content"][AUTOMATION_ORIGIN_KEY] == SCHEDULED_SOURCE_KIND
    sched_facts = ingress.classify(sched_route_msg, room_id=_ROOM)
    assert (sched_facts.source_kind, sched_facts.origin.automation_origin) == (
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
    )
    err_sched = await _call(
        tool,
        replace(ctx_base, source_kind=sched_facts.source_kind, automation_origin=sched_facts.origin.automation_origin),
        action="watch_script",
        prompt="Alert",
        source=_SRC,
    )
    assert err_sched["status"] == "error"
    assert "unattended automation" in err_sched["message"]

    # Ordinary routed human turn strips spoofed AUTOMATION_ORIGIN_KEY through _route_as_message and SUCCEEDS
    human_event = SimpleNamespace(
        sender=_OWNER,
        source={"content": {"body": "Monitor", AUTOMATION_ORIGIN_KEY: SCHEDULED_SOURCE_KIND}},
    )
    routed_human = _router_handoff_extra_content(
        relay_deps,  # type: ignore[arg-type]
        event=human_event,  # type: ignore[arg-type]
        extra_content={AUTOMATION_ORIGIN_KEY: SCHEDULED_SOURCE_KIND},
        requester_user_id=_OWNER,
        thread_event_id="$human_root",
    )
    assert routed_human[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert AUTOMATION_ORIGIN_KEY not in routed_human
    human_route_msg = _route_as_message(
        parsed_route(
            {
                **routed_human,
                ROUTE_EVENT_TYPE: {"entity": coder_uid, "event_ids": ["$human_ev"], "thread_id": "$human_root"},
                "body": "Monitor",
            },
            sender=router_uid,
            event_id="$route_human",
        ),
    )
    assert AUTOMATION_ORIGIN_KEY not in human_route_msg.source["content"]
    human_facts = ingress.classify(human_route_msg, room_id=_ROOM)
    assert human_facts.origin.automation_origin is None
    ok_human = await _call(
        tool,
        replace(ctx_base, source_kind=human_facts.source_kind, automation_origin=human_facts.origin.automation_origin),
        action="watch_script",
        prompt="Alert",
        source=_SRC,
    )
    assert ok_human["status"] == "ok"

    # #4423 is_event_source_watch_turn blocks watch_script while allowing read-only list
    ctx_watch, _ = _make_ctx(tmp_path / "watch_turn", hook_source=event_source_hook_source("slack"))
    assert await _call(tool, ctx_watch, action="watch_script", prompt="Alert", source=_SRC) == {
        "tool": "triggers",
        "status": "error",
        "message": _WATCH_TURN_MUTATION_ERROR,
    }
    assert (await _call(tool, ctx_watch, action="list"))["status"] == "ok"


@pytest.mark.asyncio
async def test_metadata_only_update_list_redaction_history_and_test(tmp_path: Path) -> None:
    """Update is metadata-only (rejects source/path, bumps revision on interval edit); list/history redact cleanly."""
    tool = TriggersTools()
    ctx, _ = _make_ctx(tmp_path)
    created = await _call(
        tool,
        ctx,
        action="watch_script",
        trigger_id="sc_u",
        prompt="Alert",
        source=_SRC,
        poll_interval_seconds=300,
    )
    assert created["status"] == "ok"

    store = ReactiveTriggerStore(ctx.runtime_paths)
    rule = store.get_trigger("sc_u")
    cp = store.get_script_check_checkpoint("sc_u")
    assert rule is not None
    assert cp is not None
    store.upsert_script_check_checkpoint(
        "sc_u",
        cp.model_copy(
            update={
                "state": {"secret_cursor": "tok-1"},
                "consecutive_errors": 2,
                "last_error_code": "timeout",
                "last_run_at": 900.0,
            },
        ),
        expected_rule_uid=rule.rule_uid,
    )
    ev = TriggerEvent(
        source_type="script_check",
        source_id="sc_u",
        kind="script_check.match",
        event_id="sc:r1",
        timestamp=1000.0,
        data={"hits": 3},
    )
    store.update_script_check_checkpoint_and_enqueue(
        "sc_u",
        store.get_script_check_checkpoint("sc_u"),
        event=ev,
        expected_rule_uid=rule.rule_uid,
        expected_revision=1,
    )  # type: ignore[arg-type]

    bad_src = await _call(tool, ctx, action="update", trigger_id="sc_u", source=_SRC)
    bad_path = await _call(tool, ctx, action="update", trigger_id="sc_u", path="a.py")
    ok_upd = await _call(tool, ctx, action="update", trigger_id="sc_u", prompt="Updated", poll_interval_seconds=90)
    assert (bad_src["status"], "Unsupported parameter" in bad_src["message"]) == ("error", True)
    assert (bad_path["status"], "Unsupported parameter" in bad_path["message"]) == ("error", True)
    assert (ok_upd["status"], ok_upd["trigger"]["source"]["poll_interval_seconds"]) == ("ok", 90)
    cp_after = store.get_script_check_checkpoint("sc_u")
    assert cp_after is not None
    assert (cp_after.revision, cp_after.state) == (3, {"secret_cursor": "tok-1"})

    owner_list = await _call(tool, ctx, action="list")
    assert owner_list["reactive_triggers"][0]["health"]["consecutive_errors"] == 2
    assert "tok-1" not in json.dumps(owner_list)
    assert "def check" not in json.dumps(owner_list)

    admin_ctx = replace(ctx, requester_id=_OTHER)
    admin_list = await _call(tool, admin_ctx, action="list")
    admin_hist = await _call(tool, admin_ctx, action="history", trigger_id="sc_u")
    assert "health" not in admin_list["reactive_triggers"][0]
    assert admin_hist["firings"][0]["event"] is None

    owner_hist = await _call(tool, ctx, action="history", trigger_id="sc_u")
    dry = await _call(tool, ctx, action="test", trigger_id="sc_u", text="urgent", sample_data={"hits": 3})
    assert (owner_hist["firings"][0]["event"]["event_id"], dry["status"]) == ("sc:r1", "ok")

    ctx_left, _ = _make_ctx(tmp_path, joined_users=())
    denied_hist = await _call(tool, ctx_left, action="history", trigger_id="sc_u")
    assert (denied_hist["status"], "no longer have access" in denied_hist["message"]) == ("error", True)


@pytest.mark.asyncio
async def test_approval_alias_grants_non_resumable_and_voice_responder(tmp_path: Path) -> None:
    """ApprovalAlias, grants, non-resumable passthrough, and voice responder (Astra #4) gate only watch_script."""
    assert _NON_CODE_TRIGGER_ACTIONS | {"watch_script"} == set(_ACTION_ALLOWED_PARAMS)
    registry = builtin_tool_registry()
    triggers_alias = registry.entries["triggers"].approval_aliases["triggers"]
    desktop_alias = registry.entries["desktop"].approval_aliases["run_desktop_shell"]
    assert (triggers_alias.allow_non_resumable_passthrough, desktop_alias.allow_non_resumable_passthrough) == (
        True,
        False,
    )
    assert len(triggers_alias.judge({"action": "watch_script", "source": _SRC}) or []) == 1
    assert (
        triggers_alias.judge({"action": "update", "trigger_id": "t1"}),
        triggers_alias.judge({"action": "list"}),
        triggers_alias.judge({"action": "bogus"}),
    ) == (
        [],
        [],
        None,
    )

    approval_cfg = ToolApprovalConfig(
        default="auto_approve",
        rules=[ApprovalRuleConfig(match="start_script", action="require_approval")],
    )
    ctx, _ = _make_ctx(tmp_path, tool_approval=approval_cfg)
    assert grant_operation(ctx.config, "triggers", {"action": "watch_script", "source": _SRC}) is None
    assert grant_operation(ctx.config, "triggers", {"action": "list"}) is not None
    assert grant_operation(ctx.config, "triggers", {"action": "update", "trigger_id": "t1"}) is not None

    toolkit = TriggersTools()
    offered = apply_tool_approval_capability(
        adapt_toolkit(toolkit, key="triggers", registered="triggers"),
        toolkit,
        ctx.config,
        tools=registry,
        registered_tool_name="triggers",
        resumable=False,
        approvals_as_jobs=False,
    )
    assert [f.name for f in offered.functions] == ["triggers"]
    call_ctx = ToolCallContext(run_id="nonres-1", session_id="sess-1", user_id=_OWNER)
    with tool_runtime_context(ctx):
        ok_list = await run_tool_call(offered.functions[0], {"action": "list"}, call_ctx, middleware=())
        denied_ws = await run_tool_call(
            offered.functions[0],
            {"action": "watch_script", "prompt": "p", "source": _SRC},
            call_ctx,
            middleware=(),
        )
    assert ok_list.status == "ok"
    assert isinstance(denied_ws.value, ToolOutcome)
    assert (denied_ws.status, denied_ws.value.refused_by) == ("denied", "approval")

    def _cfg_with(rule_match: str) -> Config:
        return bind_runtime_paths(
            ctx.config.model_copy(
                update={
                    "tool_approval": ToolApprovalConfig(
                        default="auto_approve",
                        rules=[ApprovalRuleConfig(match=rule_match, action="require_approval")],
                    ),
                },
            ),
            ctx.runtime_paths,
        )

    desk_offered = apply_tool_approval_capability(
        adapt_toolkit(DesktopTools(), key="desktop", registered="desktop"),
        DesktopTools(),
        _cfg_with("desktop"),
        tools=registry,
        registered_tool_name="desktop",
        resumable=False,
        approvals_as_jobs=False,
    )
    assert desk_offered.functions == ()

    # Astra #4: Voice responder allows triggers(list) and refuses triggers(watch_script) when start_script requires approval
    def _voice_build(cfg: Config) -> AgentBuild:
        chat = create_agent(
            "coder",
            cfg,
            ctx.runtime_paths,
            execution_identity=ctx.execution_identity,
            knowledge=None,
            model=resolve_turn_model(cfg, ctx.runtime_paths, ModelScope(entity="coder"), None),
            plugins=config_plugins(cfg, ctx.runtime_paths),
            supports_native_tool_approval=True,
            eager_deferred_tools=True,
            inline_tools=True,
        )
        assert chat.spec.functions["triggers"].gate is ToolGate.POLICY
        return _refuse_text_chat_functions(chat, cfg, tools=registry)

    voice = _voice_build(ctx.config)
    vfn, vmw = voice.spec.functions["triggers"], agent_tool_middleware(voice)
    assert vfn.gate is None
    with tool_runtime_context(ctx):
        v_list = await run_tool_call(vfn, {"action": "list"}, call_ctx, middleware=vmw)
        v_ws = await run_tool_call(
            vfn,
            {"action": "watch_script", "prompt": "Alert", "source": _SRC},
            call_ctx,
            middleware=vmw,
        )
    assert (v_list.status, json.loads(str(v_list.value))["status"], v_ws.value) == (
        "ok",
        "ok",
        _TEXT_CHAT_REQUIRED_MESSAGE,
    )

    # When triggers itself requires approval, voice responder refuses list too
    cfg_trig = _cfg_with("triggers")
    voice_trig = _voice_build(cfg_trig)
    with tool_runtime_context(replace(ctx, config=cfg_trig)):
        refused = await run_tool_call(
            voice_trig.spec.functions["triggers"],
            {"action": "list"},
            call_ctx,
            middleware=agent_tool_middleware(voice_trig),
        )
    assert refused.value == _TEXT_CHAT_REQUIRED_MESSAGE
