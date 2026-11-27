"""Tests for script_check execution: policy/gateway, live subprocess ticks, slow retirement & launch/exec timeout, reload."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import socket
import sys
import time
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import nio
import pytest
from fastapi import FastAPI

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.script_gateway import bind_script_tool_broker, serve_script_gateway_listener
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.approval.tool_approval import BackgroundScriptToolOrigin, ToolApprovalDecision
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookBindings, HookRegistry, ToolBeforeCallContext, hook
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.message_target import MessageTarget
from mindroom.orchestration import script_checks as sc_mod
from mindroom.orchestration.script_checks import build_script_check_tick_runner
from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle, _LiveScriptRuntimeResolver
from mindroom.reactive_triggers.dispatch import DispatchResult, check_reactive_source_access
from mindroom.reactive_triggers.models import (
    ReactiveTriggerRecord,
    ScriptCheckCheckpoint,
    ScriptCheckSourceSpec,
    TriageSpec,
    TriggerEvent,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.script_check_poller import ScriptCheckPoller
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.reactive_triggers.triage import TriageOutcome
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker
from mindroom.script_runs.manager import ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import ScriptRunRecord, ScriptToolGrant, utc_timestamp
from mindroom.script_runs.policy import resolve_current_script_tool
from mindroom.script_runs.reasons import PROCESS_EXIT_OBSERVED
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.shell.supervisor import _ShellSupervisorManager
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolFunction, ToolGate
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
)
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_OWNER = "@alice:example.org"
_ROOM = "!room:example.org"
_SRC = "def check():\n    return None\n"
_DIGEST = hashlib.sha256(_SRC.encode()).hexdigest()
_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="Local background script supervision requires Linux")


def _script_ctx(
    tmp_path: Path,
    *,
    tools: list[str],
    policy: dict[str, Any] | None = None,
    tool_approval: dict[str, Any] | None = None,
    hook_registry: HookRegistry | None = None,
    allow_local: bool = False,
) -> ToolRuntimeContext:
    env = {"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"}
    if allow_local:
        env["MINDROOM_ALLOW_UNSAFE_LOCAL_SCRIPTS"] = "1"
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env,
    )
    raw_cfg: dict[str, Any] = {
        "defaults": {"allow_shell_without_worker": allow_local},
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
        "agents": {"coder": {"display_name": "Coder", "rooms": [_ROOM], "tools": tools, "model": "default"}},
        "authorization": {"global_users": [_OWNER]},
        "external_trigger_policy": {"enabled": True, **(policy or {})},
    }
    if tool_approval is not None:
        raw_cfg["tool_approval"] = tool_approval
    cfg = bind_runtime_paths(Config.model_validate(raw_cfg), paths)
    return make_test_tool_runtime_context(
        agent_name="coder",
        target=MessageTarget(
            room_id=_ROOM,
            source_thread_id="$thread:example.org",
            resolved_thread_id="$thread:example.org",
            reply_to_event_id="$e:example.org",
            session_id="session-1",
        ),
        requester_id=_OWNER,
        client=make_matrix_client_mock(user_id="@mindroom_coder:example.org"),
        config=cfg,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hooks=HookBindings(registry=hook_registry or HookRegistry.empty()),
    )


class _SimpleResolver:
    def __init__(self, context: ToolRuntimeContext) -> None:
        self.context = context
        self.approval_requests = 0

    def is_authorized(self, _run: ScriptRunRecord, *, config: Config | None = None) -> bool | None:
        del config
        return True

    def resolve(self, _run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        return replace(self.context, correlation_id=correlation_id)

    def resolve_worker_authority(
        self,
        _run: ScriptRunRecord,
        *,
        context: ToolRuntimeContext,
    ) -> ScriptRuntimeWorkerAuthority:
        return ScriptRuntimeWorkerAuthority(
            worker_id=None,
            local_unsafe=True,
            worker_target=context.resolve_worker_target(),
        )

    async def request_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        context: ToolRuntimeContext,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> ToolApprovalDecision:
        del origin, context, grant, arguments, timeout_seconds
        self.approval_requests += 1
        return ToolApprovalDecision(approved=True)

    async def settle_approval(self, origin: BackgroundScriptToolOrigin, *, reason: str) -> None:
        del origin, reason

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del run_id, reason


def _create_run(
    store: ScriptRunStore,
    ctx: ToolRuntimeContext,
    run_id: str,
    *,
    sc_uid: str | None = None,
) -> tuple[ScriptRunRecord, str]:
    token, tok_hash = mint_script_capability()
    run = ScriptRunRecord(
        run_id=run_id,
        agent_name=ctx.agent_name,
        owner_user_id=ctx.requester_id,
        room_id=ctx.room_id,
        thread_root_event_id=ctx.resolved_thread_id,
        execution_identity=serialize_tool_execution_identity(ctx.execution_identity),
        source_digest=_DIGEST,
        grants=(),
        token_hash=tok_hash,
        script_check_rule_uid=sc_uid,
        local_unsafe=True,
    )
    return store.create_run(run), token


class _FeedToolkit(Toolkit):
    def __init__(self, items: list[str]) -> None:
        self.items = items
        super().__init__(name="feed", tools=[self.fetch_items, self.confirm_op, self.flaky_op])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        return replace(function, gate=ToolGate.CONFIRM) if function.name == "confirm_op" else function

    def fetch_items(self) -> str:
        return json.dumps({"items": list(self.items)})

    def confirm_op(self) -> str:
        return "confirmed"

    def flaky_op(self) -> str:
        msg = "HTTP 500 upstream error"
        raise RuntimeError(msg)


class _FakeBot:
    def __init__(self, ctx: ToolRuntimeContext, *, send_log: list[dict[str, Any]]) -> None:
        self.agent_name, self.running, self.config, self.send_log = ctx.agent_name, True, ctx.config, send_log
        self.conversation_reader, self.orchestrator = ctx.conversation_reader, None
        client = make_matrix_client_mock(user_id="@mindroom_coder:example.org")
        client.rooms = {
            _ROOM: SimpleNamespace(users={_OWNER: object(), "@mindroom_coder:example.org": object()}, encrypted=False),
        }
        client.room_send = AsyncMock(side_effect=self._room_send)
        client.room_get_state = AsyncMock(
            return_value=nio.RoomGetStateResponse(
                events=[{"type": "m.room.member", "state_key": _OWNER, "content": {"membership": "join"}}],
                room_id=_ROOM,
            ),
        )
        self.client = client
        self.tool_runtime_support = SimpleNamespace(
            build_context=lambda target, **kw: replace(
                ctx,
                target=target,
                requester_id=kw.get("user_id") or ctx.requester_id,
                agent_name=kw.get("agent_name") or ctx.agent_name,
                correlation_id=kw.get("correlation_id"),
                client=self.client,
            ),
        )

    async def _room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        **_kw: object,
    ) -> nio.RoomSendResponse:
        self.send_log.append({"room_id": room_id, "message_type": message_type, "content": content})
        return nio.RoomSendResponse(event_id=f"$sent_{len(self.send_log)}", room_id=room_id)


@asynccontextmanager
async def _live_harness(
    tmp_path: Path,
    *,
    tools: list[str],
    policy: dict[str, Any] | None = None,
    tool_approval: dict[str, Any] | None = None,
    hook_registry: HookRegistry | None = None,
) -> AsyncIterator[SimpleNamespace]:
    ctx = _script_ctx(
        tmp_path,
        tools=tools,
        policy=policy,
        tool_approval=tool_approval,
        hook_registry=hook_registry,
        allow_local=True,
    )
    send_log: list[dict[str, Any]] = []
    bot, memberships = _FakeBot(ctx, send_log=send_log), AgentReplyMembershipIndex()
    script_store, resolver = ScriptRunStore(ctx.runtime_paths), _SimpleResolver(ctx)
    broker = ScriptToolBroker(store=script_store, runtime_resolver=resolver)
    broker.open_call_admission()
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    listener_paths = resolve_primary_runtime_paths(
        config_path=ctx.runtime_paths.config_path,
        storage_path=ctx.runtime_paths.storage_root,
        process_env={**ctx.runtime_paths.process_env, "MINDROOM_SCRIPT_GATEWAY_PORT": str(port)},
    )
    manager = ScriptRunManager(
        store=script_store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url=f"http://127.0.0.1:{port}/api/script-gateway",
    )
    trig_store = ReactiveTriggerStore(ctx.runtime_paths)
    runner = build_script_check_tick_runner(
        runtime_paths=ctx.runtime_paths,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        manager_provider=lambda: manager,
        broker=broker,
    )
    service = ReactiveTriggerService(
        runtime_paths=ctx.runtime_paths,
        config_provider=lambda: ctx.config,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        store=trig_store,
        script_check_runner=runner,
    )
    supervisor = _ShellSupervisorManager()
    socket_path = supervisor.ensure()
    try:
        with (
            patch("mindroom.script_runs.manager.ensure_shell_supervisor", return_value=socket_path),
            patch("mindroom.script_runs.manager.background_script_supervision_supported", return_value=True),
        ):
            async with serve_script_gateway_listener(
                listener_paths,
                host="127.0.0.1",
                broker=broker,
                log_level="WARNING",
                agent_cli_registry=None,
                workflow_engine=None,
            ):
                yield SimpleNamespace(
                    ctx=ctx,
                    bot=bot,
                    send_log=send_log,
                    script_store=script_store,
                    trig_store=trig_store,
                    resolver=resolver,
                    broker=broker,
                    manager=manager,
                    runner=runner,
                    service=service,
                )
    finally:
        await service.stop()
        supervisor.shutdown()


def _create_live_rule(
    h: SimpleNamespace,
    tid: str,
    src: str,
    *,
    triage: TriageSpec | None = None,
    reopen_on_fire: bool = False,
) -> ReactiveTriggerRecord:
    rule = ReactiveTriggerRecord(
        trigger_id=tid,
        owner_user_id=_OWNER,
        source=ScriptCheckSourceSpec(
            agent="coder",
            room_id=_ROOM,
            source_digest=hashlib.sha256(src.encode()).hexdigest(),
            poll_interval_seconds=60,
        ),
        triage=triage,
        target=TriggerTargetSpec(
            agent="coder",
            room_id=_ROOM,
            thread_id="$thread:example.org",
            reopen_on_fire=reopen_on_fire,
            prompt="Check alert",
        ),
        created_at=1000.0,
        updated_at=1000.0,
    )
    cp = ScriptCheckCheckpoint(
        revision=1,
        source_utf8=src,
        identity_json=dict(serialize_tool_execution_identity(h.ctx.execution_identity)),
        state={},
        next_poll_at=0.0,
        updated_at=1000.0,
    )
    return h.trig_store.create_trigger(rule, initial_script_check_checkpoint=cp)


@pytest.mark.asyncio
async def test_policy_gateway_routes_and_manager_isolation(tmp_path: Path) -> None:
    """Policy restricts toolkits, gateway bounds check-input/check-result, and ScriptRunManager hides script_check runs."""
    ctx_pol = _script_ctx(
        tmp_path / "pol",
        tools=["triggers", "script", "calculator", "website"],
        policy={"script_check_allowed_tools": ["calculator", "triggers"]},
    )
    assert resolve_current_script_tool(ctx_pol, ScriptToolGrant("calculator", "add"), script_check=True) is not None
    assert resolve_current_script_tool(ctx_pol, ScriptToolGrant("website", "read_url"), script_check=True) is None
    assert resolve_current_script_tool(ctx_pol, ScriptToolGrant("triggers", "triggers"), script_check=True) is None

    store = ScriptRunStore(ctx_pol.runtime_paths)
    broker = ScriptToolBroker(store=store, runtime_resolver=_SimpleResolver(ctx_pol))
    broker.open_call_admission()
    sc_run, sc_tok = _create_run(store, ctx_pol, f"script-{'a' * 32}", sc_uid="uid-a")
    plain_run, plain_tok = _create_run(store, ctx_pol, f"script-{'b' * 32}")
    app = FastAPI()
    app.include_router(script_gateway_router)
    bind_script_tool_broker(app, broker)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        hdr = {"Authorization": f"Bearer {sc_tok}"}
        inp, res = (
            f"/api/script-gateway/runs/{sc_run.run_id}/check-input",
            f"/api/script-gateway/runs/{sc_run.run_id}/check-result",
        )
        assert (await client.get(inp, headers=hdr)).status_code == 404
        with broker.check_tick(sc_run.run_id, source=_SRC, source_digest=_DIGEST, initial_state={"k": 1}) as slot:
            assert (await client.get(inp, headers={"Authorization": f"Bearer {plain_tok}"})).status_code == 404
            assert (await client.get(inp, headers=hdr)).json() == {
                "source": _SRC,
                "source_digest": _DIGEST,
                "state": {"k": 1},
            }
            assert (await client.post(res, headers=hdr, json={"extra": 1})).status_code == 422
            assert (
                await client.post(res, headers={**hdr, "Content-Type": "application/json"}, content='{"result": NaN}')
            ).status_code == 422
            assert (
                await client.post(
                    res,
                    headers={**hdr, "Content-Type": "application/json"},
                    content=json.dumps({"result": "x" * (129 * 1024)}),
                )
            ).status_code == 413
            body = {"result": {"hits": 2}, "state": {"k": 2}, "state_changed": True, "error": None}
            assert (await client.post(res, headers=hdr, json=body)).status_code == 200
            assert (await client.post(res, headers=hdr, json=body)).status_code == 200
            assert (await client.post(res, headers=hdr, json={**body, "result": {"hits": 9}})).status_code == 409
            assert (slot.result, slot.state, slot.state_changed) == ({"hits": 2}, {"k": 2}, True)

    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
    )
    assert [r.run_id for r in await manager.list(ctx_pol)] == [plain_run.run_id]
    with pytest.raises(ScriptRunManagerError, match="not found"):
        await manager.cancel(ctx_pol, run_id=sc_run.run_id)


@_LINUX_ONLY
@pytest.mark.asyncio
async def test_live_subprocess_ticks_state_ergonomics_triage_dispatch_and_guards(tmp_path: Path) -> None:
    """Real subprocess runs check(): state ergonomics + tool() + triage + reopen_on_fire + module-load/agent/approval guards."""
    with registered_toolkit("feed", lambda: _FeedToolkit(["a", "b"])):
        async with _live_harness(tmp_path, tools=["triggers", "script", "feed"]) as h:
            src = (
                "def check():\n"
                "    items = tool('feed', 'fetch_items')\n"
                "    if 'seen' not in state:\n"
                "        state.set('seen', 1)\n"
                "        state.set('tmp', 'x')\n"
                "        assert state.pop('tmp') == 'x'\n"
                "        return None\n"
                "    state.set('seen', state.get('seen') + 1)\n"
                "    return {'title': 'New items', 'data': {'raw': items, 'seen': state['seen']}}\n"
            )
            _create_live_rule(
                h,
                "sc_live",
                src,
                triage=TriageSpec(instructions="Approve", model="default"),
                reopen_on_fire=True,
            )
            with (
                patch(
                    "mindroom.reactive_triggers.service.evaluate_triage",
                    AsyncMock(return_value=TriageOutcome(act=True, reason="ok", failure=None)),
                ) as triage_m,
                patch(
                    "mindroom.reactive_triggers.dispatch.reopen_thread_if_resolved",
                    AsyncMock(return_value=True),
                ) as reopen_m,
            ):
                assert await h.service.poll_script_checks_once() == 0
                assert triage_m.await_count == 0
                cp1 = h.trig_store.get_script_check_checkpoint("sc_live")
                assert cp1 is not None
                assert cp1.state == {"seen": 1}

                h.trig_store.upsert_script_check_checkpoint("sc_live", cp1.model_copy(update={"next_poll_at": 0.0}))
                assert await h.service.poll_script_checks_once() == 1
                await h.service.drain_due_firings_once()
                assert (triage_m.await_count, reopen_m.await_count, len(h.send_log)) == (1, 1, 1)
                assert h.trig_store.get_script_check_checkpoint("sc_live").state == {"seen": 2}

            h.trig_store.delete_trigger("sc_live")
            for tid, gsrc, expected_err in (
                ("g_agent", "def check():\n    agent('coder', 'hi')\n    return {'x': 1}\n", "script_error"),
                ("g_mod_tool", "tool('feed', 'fetch_items')\ndef check():\n    return None\n", "script_error"),
                ("g_mod_state", "state.set('x', 1)\ndef check():\n    return None\n", "script_error"),
                (
                    "g_confirm",
                    "def check():\n    state.set('dirty', 1)\n    tool('feed', 'confirm_op')\n    return {'x': 1}\n",
                    "approval_required",
                ),
            ):
                _create_live_rule(h, tid, gsrc)
                assert await h.service.poll_script_checks_once() == 0
                cp = h.trig_store.get_script_check_checkpoint(tid)
                assert cp is not None
                assert (cp.last_error_code, cp.state) == (expected_err, {})
                h.trig_store.delete_trigger(tid)
            assert h.resolver.approval_requests == 0


@_LINUX_ONLY
@pytest.mark.asyncio
async def test_slow_k8s_worker_retirement_preserves_clean_tick_result_and_state(tmp_path: Path) -> None:
    """Astra #1: slow worker retirement (>2-5s) never discards an exit_code==0 tick; observe_process_exit commits immediately."""
    async with _live_harness(tmp_path, tools=["triggers", "script"]) as h:
        src = "def check():\n    state.set('done', True)\n    return {'title': 'Clean', 'data': {'ok': True}}\n"
        rule = _create_live_rule(h, "sc_slow_retire", src)
        cp = h.trig_store.get_script_check_checkpoint("sc_slow_retire")
        assert cp is not None

        retire_started, release_retire = asyncio.Event(), asyncio.Event()
        orig_reconcile = ScriptRunManager.reconcile_durable

        async def slow_reconcile(
            self_mgr: ScriptRunManager,
            *,
            run_id: str,
            broker_revoked: bool = False,
        ) -> ScriptRunRecord:
            retire_started.set()
            await release_retire.wait()
            return await orig_reconcile(self_mgr, run_id=run_id, broker_revoked=broker_revoked)

        with patch.object(ScriptRunManager, "reconcile_durable", slow_reconcile):
            t0 = time.monotonic()
            outcome = await h.runner(rule, cp, h.ctx.config, 1000.0)
            assert (outcome.status, outcome.result, outcome.state) == (
                "ok",
                {"title": "Clean", "data": {"ok": True}},
                {"done": True},
            )
            assert time.monotonic() - t0 < 3.0
            assert outcome.run_id is not None
            await asyncio.wait_for(retire_started.wait(), timeout=2.0)
            row = h.script_store.get_run(outcome.run_id)
            assert (row.exit_code, row.cancellation_reason) == (0, PROCESS_EXIT_OBSERVED)
            release_retire.set()
            await h.service.stop_script_checks()
            assert outcome.run_id not in {r.run_id for r in h.script_store.list_runs(include_finished=True)}


@_LINUX_ONLY
@pytest.mark.asyncio
async def test_cold_launch_timeout_releases_slot_immediately_and_execution_timeout(tmp_path: Path) -> None:
    """Astra #2: slow cold launch revokes/returns launch_timeout freeing poller slot; infinite check() hits execution timeout."""
    async with _live_harness(tmp_path, tools=["triggers", "script"], policy={"script_check_timeout_seconds": 5}) as h:
        r1 = _create_live_rule(h, "sc_hang_launch", _SRC)
        _create_live_rule(h, "sc_second", "def check():\n    return {'ok': 2}\n")

        shielded_done, release_shielded = asyncio.Event(), asyncio.Event()
        launch_calls, cancelled_runs = 0, []
        orig_run, orig_cancel = ScriptRunManager.run, ScriptToolBroker.cancel_run

        async def slow_first_launch(self_mgr: ScriptRunManager, *args: object, **kwargs: object) -> ScriptRunRecord:
            nonlocal launch_calls
            launch_calls += 1
            if launch_calls == 1:
                try:
                    await asyncio.sleep(60)
                finally:
                    await asyncio.shield(release_shielded.wait())
                    shielded_done.set()
            return await orig_run(self_mgr, *args, **kwargs)  # type: ignore[arg-type]

        async def spy_cancel(self_broker: ScriptToolBroker, run_id: str) -> None:
            cancelled_runs.append(run_id)
            await orig_cancel(self_broker, run_id)

        with (
            patch.object(sc_mod, "_LAUNCH_TIMEOUT_SECONDS", 0.25),
            patch.object(ScriptRunManager, "run", slow_first_launch),
            patch.object(ScriptToolBroker, "cancel_run", spy_cancel),
        ):
            poller = ScriptCheckPoller(
                store=h.trig_store,
                config_provider=lambda: h.ctx.config,
                runtime_paths=h.ctx.runtime_paths,
                tick_runner=h.runner,
                max_concurrency=1,
            )
            t0 = time.monotonic()
            assert await poller.poll_due_once(now=1000.0) == 0
            assert len(cancelled_runs) >= 1
            cp1 = h.trig_store.get_script_check_checkpoint(r1.trigger_id)
            assert cp1 is not None
            assert cp1.last_error_code == "launch_timeout"
            assert not shielded_done.is_set()
            # The freed slot admits sc_second immediately without waiting for shielded launch cleanup.
            assert await poller.poll_due_once(now=1000.0) == 1
            assert time.monotonic() - t0 < 3.0
            assert not shielded_done.is_set()
            release_shielded.set()
            await poller.stop()
            assert shielded_done.is_set()

        # Real execution timeout: infinite loop inside check() terminates with error_code="timeout" without committing state
        r_loop = _create_live_rule(
            h,
            "sc_loop",
            "import time\ndef check():\n    state.set('x', 1)\n    time.sleep(30)\n",
        )
        cp_loop = h.trig_store.get_script_check_checkpoint(r_loop.trigger_id)
        assert cp_loop is not None
        fast_pol = h.ctx.config.external_trigger_policy.model_construct(
            **{**h.ctx.config.external_trigger_policy.model_dump(), "script_check_timeout_seconds": 1},
        )
        fast_cfg = h.ctx.config.model_copy(update={"external_trigger_policy": fast_pol})
        out_to = await h.runner(r_loop, cp_loop, fast_cfg, 2000.0)
        assert (out_to.status, out_to.error_code, out_to.state) == ("error", "timeout", None)


@pytest.mark.asyncio
async def test_reload_lifecycle_and_dispatch_source_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ScriptRuntimeLifecycle revokes script_check runs on policy/tool removal and dispatch rechecks direct agent access."""
    monkeypatch.setattr("mindroom.script_runs.manager.background_script_supervision_supported", lambda: True)
    ctx_both = _script_ctx(tmp_path / "lc", tools=["triggers", "script", "workflow"], allow_local=True)
    store, bot = ScriptRunStore(ctx_both.runtime_paths), _FakeBot(ctx_both, send_log=[])
    cfg_box = [ctx_both.config]
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=ctx_both.runtime_paths,
        bot_provider=lambda name: bot if name == "coder" else None,  # type: ignore[arg-type,return-value]
        worker_backend_provider=lambda _run: None,
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
    )
    lifecycle = ScriptRuntimeLifecycle(
        runtime_paths=ctx_both.runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: cfg_box[0],
        worker_lease_provider=lambda _loc: None,
        api_enabled=False,
    )
    plain, _ = _create_run(store, ctx_both, f"script-{'1' * 32}")
    sc_active, _ = _create_run(store, ctx_both, f"script-{'2' * 32}", sc_uid="rule-2")
    assert [r.run_id for r in await lifecycle.active_runs()] == [plain.run_id]

    cfg_disabled = _script_ctx(
        tmp_path / "lc",
        tools=["triggers", "script"],
        policy={"script_check_enabled": False},
        allow_local=True,
    ).config
    cfg_no_trig = _script_ctx(tmp_path / "lc", tools=["workflow"], allow_local=True).config
    for next_cfg, expected_ids in ((cfg_disabled, {sc_active.run_id}), (cfg_no_trig, {plain.run_id, sc_active.run_id})):
        with patch.object(ScriptRuntimeLifecycle, "_interrupt_runs", AsyncMock()) as interrupt_m:
            await lifecycle._apply_update(
                next_cfg,
                removed_entities=frozenset(),
                authorization_changed=False,
                plugins_changed=False,
            )
            assert {r.run_id for r in interrupt_m.await_args.args[0]} == expected_ids

    # Workflow-only agent loses script_check eligibility on reload when start_workflow requires approval.
    cfg_box[0] = _script_ctx(tmp_path / "lc", tools=["triggers", "workflow"], allow_local=True).config
    cfg_wf_gated = _script_ctx(
        tmp_path / "lc",
        tools=["triggers", "workflow"],
        tool_approval={"default": "auto_approve", "rules": [{"match": "start_workflow", "action": "require_approval"}]},
        allow_local=True,
    ).config
    with patch.object(ScriptRuntimeLifecycle, "_interrupt_runs", AsyncMock()) as interrupt_m:
        await lifecycle._apply_update(
            cfg_wf_gated,
            removed_entities=frozenset(),
            authorization_changed=False,
            plugins_changed=False,
        )
        assert {r.run_id for r in interrupt_m.await_args.args[0]} == {sc_active.run_id}

    rule = ReactiveTriggerRecord(
        trigger_id="sc_disp",
        owner_user_id=_OWNER,
        source=ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest=_DIGEST),
        target=TriggerTargetSpec(agent="coder", room_id=_ROOM, prompt="Ping"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    ev = TriggerEvent(
        source_type="script_check",
        source_id="sc_disp",
        kind="script_check.match",
        event_id="sc:1",
        timestamp=1000.0,
    )
    memberships = AgentReplyMembershipIndex()

    def _state(membership: str) -> dict[str, RoomStateSnapshot]:
        return {
            _ROOM: RoomStateSnapshot(
                room_id=_ROOM,
                creators=frozenset(),
                events={("m.room.member", _OWNER): {"membership": membership}},
            ),
        }

    for check_cfg, membership, expected in (
        (ctx_both.config, "join", None),
        (ctx_both.config, "leave", DispatchResult(status="skipped", reason="source_room_not_joined")),
        (cfg_disabled, "join", DispatchResult(status="skipped", reason="script_check_disabled")),
    ):
        result = await check_reactive_source_access(
            rule=rule,
            event=ev,
            source_bot=bot,
            config=check_cfg,
            runtime_paths=ctx_both.runtime_paths,
            memberships=memberships,
            known_room_states=_state(membership),  # type: ignore[arg-type]
        )
        assert result == expected


@_LINUX_ONLY
@pytest.mark.asyncio
async def test_concurrent_reconcile_durable_lock_does_not_block_observe_process_exit(tmp_path: Path) -> None:
    """Astra follow-up #2: observe_process_exit reads durable exit_code==0 without blocking on reconcile_durable lock."""
    async with _live_harness(tmp_path / "live", tools=["triggers", "script"]) as h:
        src = "def check():\n    state.set('done', 1)\n    return {'title': 'Observed', 'data': {'ok': True}}\n"
        _create_live_rule(h, "sc_race", src)

        orig_observe = ScriptRunManager.observe_process_exit
        orig_finalize = ScriptRunManager._finalize_observed_exit
        retire_started, release_retire = asyncio.Event(), asyncio.Event()
        reconcile_task: asyncio.Task[ScriptRunRecord] | None = None
        captured_run_id: str | None = None

        async def slow_finalize(
            self_mgr: ScriptRunManager,
            run: ScriptRunRecord,
            *,
            broker_revoked: bool = False,
        ) -> ScriptRunRecord:
            if run.run_id == captured_run_id:
                retire_started.set()
                await release_retire.wait()
            return await orig_finalize(self_mgr, run, broker_revoked=broker_revoked)

        async def observe_while_reconciling(self_mgr: ScriptRunManager, run_id: str) -> ScriptRunRecord | None:
            nonlocal reconcile_task, captured_run_id
            slot = h.broker._check_ticks.get(run_id)
            if reconcile_task is None and slot is not None and slot.done.is_set():
                captured_run_id = run_id
                while True:
                    st = await self_mgr._process_status(self_mgr.store.get_run(run_id))
                    if st.state != "running":
                        break
                    await asyncio.sleep(0.02)
                reconcile_task = asyncio.create_task(self_mgr.reconcile_durable(run_id=run_id))
                await asyncio.wait_for(retire_started.wait(), timeout=2.0)
                assert self_mgr._run_lock(run_id).locked()
            return await orig_observe(self_mgr, run_id)

        with (
            patch.object(ScriptRunManager, "_finalize_observed_exit", slow_finalize),
            patch.object(ScriptRunManager, "observe_process_exit", observe_while_reconciling),
        ):
            t0 = time.monotonic()
            enqueued = await h.service.poll_script_checks_once()
            assert enqueued == 1
            assert time.monotonic() - t0 < 3.0
            assert reconcile_task is not None
            assert not reconcile_task.done()
            cp = h.trig_store.get_script_check_checkpoint("sc_race")
            assert cp is not None
            assert (cp.state, cp.last_error_code) == ({"done": 1}, None)
            assert len(h.trig_store.list_firings(trigger_id="sc_race")) == 1
            release_retire.set()
            await reconcile_task
            await h.service.stop_script_checks()


@pytest.mark.asyncio
async def test_reconcile_pass_skips_active_runner_owned_script_check_runs(tmp_path: Path) -> None:
    """Astra follow-up #2: _reconcile_pass skips active runner-owned script_check ticks while reconciling orphans/finished/expired."""
    ctx = _script_ctx(tmp_path / "pass", tools=["triggers", "script"], allow_local=True)
    store, bot = ScriptRunStore(ctx.runtime_paths), _FakeBot(ctx, send_log=[])
    bot.orchestrator = SimpleNamespace(agent_reply_memberships=AgentReplyMembershipIndex())
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=ctx.runtime_paths,
        bot_provider=lambda name: bot if name == "coder" else None,  # type: ignore[arg-type,return-value]
        worker_backend_provider=lambda _run: None,
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
        clock=lambda: now,
    )
    lifecycle = ScriptRuntimeLifecycle(
        runtime_paths=ctx.runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: ctx.config,
        worker_lease_provider=lambda _loc: None,
        api_enabled=False,
    )
    _, tok_hash = mint_script_capability()
    base_kw: dict[str, Any] = {
        "agent_name": "coder",
        "owner_user_id": _OWNER,
        "room_id": _ROOM,
        "thread_root_event_id": ctx.execution_identity.resolved_thread_id,
        "execution_identity": dict(serialize_tool_execution_identity(ctx.execution_identity)),
        "source_digest": _DIGEST,
        "grants": (),
        "token_hash": tok_hash,
        "local_unsafe": True,
        "max_runtime_seconds": 30,
    }
    r_active = store.create_run(
        ScriptRunRecord(
            run_id=f"script-{'a' * 32}",
            script_check_rule_uid="u-active",
            created_at=utc_timestamp(now - timedelta(seconds=10)),
            **base_kw,
        ),
    )
    r_orphan = store.create_run(
        ScriptRunRecord(
            run_id=f"script-{'b' * 32}",
            script_check_rule_uid="u-orphan",
            created_at=utc_timestamp(now - timedelta(seconds=10)),
            **base_kw,
        ),
    )
    r_finished = store.create_run(
        ScriptRunRecord(
            run_id=f"script-{'c' * 32}",
            script_check_rule_uid="u-finished",
            created_at=utc_timestamp(now - timedelta(seconds=10)),
            **base_kw,
        ),
    )
    store.record_process_exit(
        r_finished.run_id,
        exit_code=0,
        error=None,
        output="",
        cancellation_reason=PROCESS_EXIT_OBSERVED,
    )
    r_expired = store.create_run(
        ScriptRunRecord(
            run_id=f"script-{'d' * 32}",
            script_check_rule_uid="u-expired",
            created_at=utc_timestamp(now - timedelta(seconds=120)),
            **base_kw,
        ),
    )
    with (
        broker.check_tick(r_active.run_id, source=_SRC, source_digest=_DIGEST, initial_state={}),
        broker.check_tick(r_finished.run_id, source=_SRC, source_digest=_DIGEST, initial_state={}),
        broker.check_tick(r_expired.run_id, source=_SRC, source_digest=_DIGEST, initial_state={}),
        patch.object(ScriptRunManager, "reconcile_durable", AsyncMock()) as reconcile_m,
    ):
        await lifecycle._reconcile_pass()
        reconciled_ids = {call.kwargs["run_id"] for call in reconcile_m.await_args_list}
    assert reconciled_ids == {r_orphan.run_id, r_finished.run_id, r_expired.run_id}


@_LINUX_ONLY
@pytest.mark.asyncio
async def test_script_check_denied_tool_call_cannot_commit_state_or_fire(tmp_path: Path) -> None:
    """Astra fresh #1: catching a policy-denied tool() error cannot persist state mutations or fire an event."""
    with registered_toolkit("feed", lambda: _FeedToolkit(["a", "b"])):
        async with _live_harness(
            tmp_path,
            tools=["triggers", "script", "feed", "calculator"],
            policy={"script_check_allowed_tools": ["calculator"]},
        ) as h:
            for tid, denied_call in (
                ("sc_excluded", "tool('feed', 'fetch_items')"),
                ("sc_restricted", "tool('triggers', 'triggers', action='list')"),
            ):
                src = (
                    "def check():\n"
                    "    try:\n"
                    f"        {denied_call}\n"
                    "    except Exception:\n"
                    "        pass\n"
                    "    state.set('dirty', 1)\n"
                    "    return {'title': 'Bypass attempt', 'data': {'dirty': True}}\n"
                )
                _create_live_rule(h, tid, src)
                assert await h.service.poll_script_checks_once() == 0
                cp = h.trig_store.get_script_check_checkpoint(tid)
                assert cp is not None
                assert (cp.state, cp.last_error_code, cp.consecutive_errors) == ({}, "tool_denied", 1)
                assert h.trig_store.list_firings(trigger_id=tid) == []
                h.trig_store.delete_trigger(tid)


@_LINUX_ONLY
@pytest.mark.asyncio
async def test_script_check_hook_veto_and_approval_script_failure_block_commit_while_ordinary_errors_recover(
    tmp_path: Path,
) -> None:
    """Astra follow-up #1 & P2 #3: hook veto & approval-script failure set tool_denied; ordinary tool errors recover."""

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def veto_fetch(context: ToolBeforeCallContext) -> None:
        if context.tool_name == "fetch_items":
            context.decline("Blocked by before_call policy hook")

    hook_plugin = SimpleNamespace(
        name="script-check-veto-test",
        discovered_hooks=(veto_fetch,),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path="./plugins/script-check-veto-test"),
        plugin_order=0,
    )
    veto_registry = HookRegistry.from_plugins([hook_plugin])

    catch_fetch_src = (
        "def check():\n"
        "    try:\n"
        "        tool('feed', 'fetch_items')\n"
        "    except Exception:\n"
        "        pass\n"
        "    state.set('dirty', 1)\n"
        "    return {'title': 'Bypass attempt', 'data': {'dirty': True}}\n"
    )
    recover_flaky_src = (
        "def check():\n"
        "    caught = False\n"
        "    try:\n"
        "        tool('feed', 'flaky_op')\n"
        "    except Exception:\n"
        "        caught = True\n"
        "    state.set('recovered', caught)\n"
        "    return {'title': 'Recovered', 'data': {'recovered': caught}}\n"
    )

    with registered_toolkit("feed", lambda: _FeedToolkit(["a", "b"])):
        # 1. tool:before_call hook veto caught inside check() -> tool_denied, no state or firing persisted.
        async with _live_harness(
            tmp_path / "hook_veto",
            tools=["triggers", "script", "feed"],
            hook_registry=veto_registry,
        ) as h_veto:
            _create_live_rule(h_veto, "sc_veto", catch_fetch_src)
            assert await h_veto.service.poll_script_checks_once() == 0
            cp_veto = h_veto.trig_store.get_script_check_checkpoint("sc_veto")
            assert cp_veto is not None
            assert (cp_veto.state, cp_veto.last_error_code, cp_veto.consecutive_errors) == ({}, "tool_denied", 1)
            assert h_veto.trig_store.list_firings(trigger_id="sc_veto") == []

            # Ordinary recoverable tool error (RuntimeError("HTTP 500")) caught in check() still commits and fires.
            h_veto.trig_store.delete_trigger("sc_veto")
            _create_live_rule(h_veto, "sc_recover", recover_flaky_src)
            assert await h_veto.service.poll_script_checks_once() == 1
            cp_rec = h_veto.trig_store.get_script_check_checkpoint("sc_recover")
            assert cp_rec is not None
            assert (cp_rec.state, cp_rec.last_error_code, cp_rec.consecutive_errors) == ({"recovered": True}, None, 0)
            assert len(h_veto.trig_store.list_firings(trigger_id="sc_recover")) == 1

        # 2. Approval script raising during evaluate_tool_approval caught inside check() -> tool_denied, no state/firing.
        script_dir = tmp_path / "approval_fail"
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "broken_approval.py").write_text(
            "def check(tool_name, arguments, agent_name):\n    raise RuntimeError('approval script crashed')\n",
            encoding="utf-8",
        )
        async with _live_harness(
            script_dir,
            tools=["triggers", "script", "feed"],
            tool_approval={
                "default": "auto_approve",
                "rules": [{"match": "fetch_items", "script": "broken_approval.py"}],
            },
        ) as h_appr:
            _create_live_rule(h_appr, "sc_appr_fail", catch_fetch_src)
            assert await h_appr.service.poll_script_checks_once() == 0
            cp_appr = h_appr.trig_store.get_script_check_checkpoint("sc_appr_fail")
            assert cp_appr is not None
            assert (cp_appr.state, cp_appr.last_error_code, cp_appr.consecutive_errors) == ({}, "tool_denied", 1)
            assert h_appr.trig_store.list_firings(trigger_id="sc_appr_fail") == []


@pytest.mark.asyncio
async def test_stop_respects_drain_deadline_with_unreleased_stalled_launch(tmp_path: Path) -> None:
    """stop() cancels cleanup cleanly when drain deadline expires even if a stalled launch is still unreleased."""
    ctx = _script_ctx(tmp_path, tools=["triggers", "script"], allow_local=True)
    bot, memberships = _FakeBot(ctx, send_log=[]), AgentReplyMembershipIndex()
    script_store, resolver = ScriptRunStore(ctx.runtime_paths), _SimpleResolver(ctx)
    broker = ScriptToolBroker(store=script_store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=script_store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
    )
    trig_store = ReactiveTriggerStore(ctx.runtime_paths)
    runner = build_script_check_tick_runner(
        runtime_paths=ctx.runtime_paths,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        manager_provider=lambda: manager,
        broker=broker,
    )
    r1 = _create_live_rule(SimpleNamespace(ctx=ctx, trig_store=trig_store), "sc_stalled_stop", _SRC)
    release_launch, launch_finished = asyncio.Event(), asyncio.Event()
    reconcile_called = False

    async def stalled_launch(self_mgr: ScriptRunManager, *args: object, **kwargs: object) -> ScriptRunRecord:
        del self_mgr, args, kwargs
        try:
            while not release_launch.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await release_launch.wait()
        finally:
            launch_finished.set()
        msg = "stalled launch cancelled"
        raise asyncio.CancelledError(msg)

    async def slow_reconcile(self_mgr: ScriptRunManager, *, run_id: str) -> ScriptRunRecord:
        del self_mgr, run_id
        nonlocal reconcile_called
        reconcile_called = True
        await asyncio.sleep(30)
        msg = "unreachable"
        raise AssertionError(msg)

    with (
        patch("mindroom.script_runs.manager.background_script_supervision_supported", return_value=True),
        patch.object(sc_mod, "_LAUNCH_TIMEOUT_SECONDS", 0.1),
        patch.object(sc_mod, "_STOP_DRAIN_TIMEOUT_SECONDS", 0.15),
        patch.object(ScriptRunManager, "run", stalled_launch),
        patch.object(ScriptRunManager, "reconcile_durable", slow_reconcile),
    ):
        poller = ScriptCheckPoller(
            store=trig_store,
            config_provider=lambda: ctx.config,
            runtime_paths=ctx.runtime_paths,
            tick_runner=runner,
            max_concurrency=1,
        )
        try:
            assert await poller.poll_due_once(now=1000.0) == 0
            cp = trig_store.get_script_check_checkpoint(r1.trigger_id)
            assert cp is not None
            assert cp.last_error_code == "launch_timeout"
            assert not launch_finished.is_set()

            t_stop = time.monotonic()
            await poller.stop()
            elapsed = time.monotonic() - t_stop

            assert elapsed < 1.0
            assert not launch_finished.is_set()
            assert not reconcile_called
        finally:
            release_launch.set()
            await asyncio.wait_for(launch_finished.wait(), timeout=2.0)
