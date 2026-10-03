"""Durable background delegation completion delivery and lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.agent import Agent
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall

import mindroom.orchestration.tool_job_runtime as runtime_module
import mindroom.tool_system.metadata as metadata_module
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ToolConfigEntry
from mindroom.delegation import recovery as delegation_recovery
from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.delegation.lifecycle import child_run_context, start_child_turn
from mindroom.matrix import state as matrix_state
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.authorization import (
    authority_snapshot,
    bind_actor_authority,
    bind_toolkit_authority,
    function_authority,
)
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_jobs.disabled import ParkedWork
from mindroom.tool_jobs.execution_authority import authorized_tool_call, check_current_execution_authority
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.runtime import (
    BackgroundJob,
    BackgroundOutcome,
    JobAccessError,
    ToolJobRuntime,
    get_background_runtime,
    register_background_runtime,
)
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction, tool_config_signature
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry_state import TOOL_REGISTRY, tool_registry_origin
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.conftest import test_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context
from tests.test_mcp_toolkit import _oauth_server_config
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    completed_delegation_job,
    delivery_coordinator,
    finish_delegation_job,
    job_child,
    job_owner,
    keep_child,
    managed_team_config,
    start_delegation_job,
    start_job,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_startup", [False, True])
async def test_runtime_startup_io_keeps_loop_live_and_retains_cancelled_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_startup: bool,
) -> None:
    """Storage setup runs off-loop, and cancellation cannot orphan its newly acquired lease."""
    config = managed_team_config(tmp_path)
    config.background_tool_jobs.enabled = True
    paths = test_runtime_paths(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: config, lambda _: None, AgentReplyMembershipIndex())
    original_init = ToolJobRuntime.__init__
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    ready, release = asyncio.Event(), threading.Event()

    def gated_init(runtime: ToolJobRuntime, *args: object, **kwargs: object) -> None:
        assert threading.get_ident() != loop_thread, "storage setup blocks the event loop"
        original_init(runtime, *args, **kwargs)
        loop.call_soon_threadsafe(ready.set)
        assert release.wait(30)

    monkeypatch.setattr(ToolJobRuntime, "__init__", gated_init)
    startup = asyncio.create_task(coordinator.sync())
    ready_waiter = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait({startup, ready_waiter}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if startup in done:
            await startup
        assert ready_waiter in done
        if cancel_startup:
            startup.cancel()
        release.set()
        if cancel_startup:
            with pytest.raises(asyncio.CancelledError):
                await startup
        else:
            await startup
        monkeypatch.setattr(ToolJobRuntime, "__init__", original_init)
        with pytest.raises(BlockingIOError):
            tool_job_runtime(paths.storage_root)
    finally:
        release.set()
        ready_waiter.cancel()
        await asyncio.gather(startup, ready_waiter, return_exceptions=True)
        await coordinator.stop()
    replacement = tool_job_runtime(paths.storage_root)
    await replacement.shutdown()


def test_completion_authority_uses_latest_config_and_team_membership(tmp_path: Path) -> None:
    """A config reload cannot leave completion delivery holding old authority."""
    config = managed_team_config(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    job = completed_delegation_job()
    assert coordinator._authorized(job)
    config.agents["lead"].delegate_to = []
    assert not coordinator._authorized(job)
    config.agents["lead"].delegate_to = ["worker"]
    config.teams["team"].agents = ["worker"]
    assert not coordinator._authorized(job)


def test_completion_authority_checks_requester_for_target_and_recipient(tmp_path: Path) -> None:
    """Current target or recipient access revocation blocks delivery."""
    config = managed_team_config(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    job = completed_delegation_job()
    assert not coordinator._authorized(replace(job, owner=replace(job.owner, requester_id="@stranger:localhost")))
    config.agents["worker"].access = ResponderAccessConfig(current_room_members=False)
    assert not coordinator._authorized(job)


class _ResolvingMembership(AgentReplyMembershipIndex):
    """Room membership that is still resolving until a test proves it either way."""

    def __init__(self) -> None:
        super().__init__()
        self.state = "pending"

    def is_current_room_member(
        self,
        sender_id: str,
        room_id: str,
        config: Config,
        runtime_paths: RuntimePaths,
    ) -> bool:
        del sender_id, room_id, config, runtime_paths
        return self.state == "member"

    def grants_pending(
        self,
        config: Config,
        *,
        joined_rooms: Sequence[str],
        current_room_id: str | None,
    ) -> bool:
        del config, joined_rooms, current_room_id
        return self.state == "pending"


@pytest.mark.asyncio
async def test_revocation_waits_for_resolving_room_membership(tmp_path: Path) -> None:
    """Unresolved membership hides a job from access but cancels it only after a proven denial."""
    config = managed_team_config(tmp_path)
    for entity in (config.agents["lead"], config.agents["worker"], config.teams["team"]):
        entity.access = ResponderAccessConfig(current_room_members=True)
    membership = _ResolvingMembership()
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=membership,
    )
    runtime = tool_job_runtime(tmp_path, authorize=coordinator._authorized)
    fixture = completed_delegation_job()
    release = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await release.wait()
        return BackgroundOutcome("completed", "Saved answer")

    try:
        membership.state = "member"
        await start_delegation_job(runtime, delegation_child(fixture), owner=fixture.owner, operation=operation)
        for state, running in (("pending", True), ("member", True), ("pending", True), ("stranger", False)):
            membership.state = state
            assert coordinator._authorized(fixture) is (state == "member")
            await runtime.cancel_revoked(denied=coordinator._denied)
            status = runtime._entries[fixture.job_id].job.status
            assert (status == "running") is running, state
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
async def test_revocation_cancels_hidden_work_without_delivering_its_result(tmp_path: Path, approval: bool) -> None:
    """Current permission loss also stops accepted work through its internal owner."""
    config = managed_team_config(tmp_path)
    coordinator = delivery_coordinator(tmp_path, config)
    await coordinator.initialize()
    fixture = completed_delegation_job()
    child = delegation_child(fixture)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        if approval:
            return BackgroundOutcome("awaiting_approval")
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(retained: DelegationChild) -> None:
        retained.status = "cancelled"
        retained.result = "Cancelled after revocation"
        cancelled.set()

    try:
        await start_delegation_job(coordinator.runtime, child, owner=fixture.owner, operation=operation, cancel=cleanup)
        await started.wait()
        if approval:
            waited = await coordinator.runtime.wait(child.delegation_id, owner=fixture.owner, depth=0)
            await coordinator.runtime.release_wait(child.delegation_id, waited.claim)
        config.agents["lead"].delegate_to.clear()
        await coordinator.deliver_pending()
        assert await coordinator.runtime.list_jobs(owner=fixture.owner, depth=0) == []
        await asyncio.wait_for(cancelled.wait(), JOB_TEST_TIMEOUT)
        config.agents["lead"].delegate_to.append("worker")
        waited = await coordinator.runtime.wait(child.delegation_id, owner=fixture.owner, depth=0)
        assert waited.job.status == "cancelled"
        await coordinator.runtime.release_wait(child.delegation_id, waited.claim)
        coordinator.bot_provider("team").wake_tool_job_completion.assert_not_awaited()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_retry_passes_for_unjoined_recipient_do_not_query_the_homeserver(tmp_path: Path) -> None:
    """Retry passes check a burst of jobs for a bot outside their room against synced room state, not Matrix."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    fixture = completed_delegation_job()
    bot = coordinator.bot_provider("team")
    assert bot is not None
    client = bot.client
    client.rooms = {}
    client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[]))

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "result")

    try:
        for name in ("one", "two"):
            await start_job(
                coordinator.runtime,
                name,
                tool_name=fixture.tool_name,
                depth=0,
                kind=fixture.kind,
                adapter=fixture.adapter,
                owner=fixture.owner,
                operation=completed,
            )
            waited = await coordinator.runtime.wait(name, owner=fixture.owner, depth=0)
            await coordinator.runtime.release_wait(name, waited.claim)
        await coordinator.deliver_pending()
        await coordinator.deliver_pending()
        assert len(client.method_calls) <= 1
        bot.wake_tool_job_completion.assert_not_awaited()
        client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_team:localhost")}
        await coordinator.deliver_pending()
        assert bot.wake_tool_job_completion.await_count == 2
        await coordinator.deliver_pending()
        assert bot.wake_tool_job_completion.await_count == 2
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_failed_coordinator_stop_releases_pinned_state_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-process restart must choose fresh settings and a fresh runtime after a shutdown save error."""
    config = managed_team_config(tmp_path)
    coordinator = delivery_coordinator(tmp_path, config)
    await coordinator.initialize()
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    fixture = completed_delegation_job()
    await start_delegation_job(coordinator.runtime, delegation_child(fixture), owner=fixture.owner, operation=operation)
    await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)

    async def failed_save(*_args: object, **_kwargs: object) -> None:
        msg = "snapshot unavailable"
        raise OSError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(coordinator.runtime, "_publish", failed_save)
        with pytest.raises(ExceptionGroup):
            await coordinator.stop()
    config.background_tool_jobs.enabled = False
    try:
        await coordinator.sync()
        assert get_background_runtime(coordinator.runtime_paths) is None
        await coordinator.stop()
        config.background_tool_jobs.enabled = True
        await coordinator.sync()
        assert get_background_runtime(coordinator.runtime_paths) is coordinator.runtime
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_live_wait_claim_suppresses_completion_delivery(tmp_path: Path) -> None:
    """The delivery loop cannot race a result awaiting parent persistence."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    job = await finish_delegation_job(coordinator)
    waiting = await coordinator.runtime.wait(job.job_id, owner=job.owner, depth=0)
    bot = coordinator.bot_provider("team")
    assert bot is not None
    await coordinator.deliver_pending()
    bot.wake_tool_job_completion.assert_not_awaited()
    await coordinator.runtime.acknowledge_wait(job.job_id, waiting.claim)
    await coordinator.deliver_pending()
    bot.wake_tool_job_completion.assert_not_awaited()
    await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_withdraws_service_and_interrupts_live_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown owns detached tasks and removes the managed runtime lookup."""
    monkeypatch.setattr(delegation_recovery, "interrupt_child", AsyncMock())
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.sync()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError

    fixture = completed_delegation_job()
    await start_delegation_job(coordinator.runtime, delegation_child(fixture), owner=fixture.owner, operation=operation)
    await started.wait()
    assert get_background_runtime(coordinator.runtime_paths) is coordinator.runtime
    owner = coordinator.runtime
    await coordinator.sync()
    assert coordinator.runtime is owner
    await coordinator.stop()
    assert cancelled.is_set()
    assert get_background_runtime(coordinator.runtime_paths) is None
    restored = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await restored.initialize()
    await restored.runtime.recover()
    job = await restored.runtime.lookup(fixture.job_id, owner=fixture.owner, depth=0)
    assert job.status == "interrupted"
    await restored.stop()


def test_constructing_orchestrator_support_does_not_claim_runtime_storage(tmp_path: Path) -> None:
    """Only a started service may own the exclusive job-store lease."""
    config = managed_team_config(tmp_path)
    first = delivery_coordinator(tmp_path, config)
    second = delivery_coordinator(tmp_path, config)
    assert first is not second
    assert not (first.runtime_paths.storage_root / "tool_jobs").exists()


def test_ordinary_job_authority_tracks_tool_grant_and_filters(tmp_path: Path) -> None:
    """Ordinary job authority tracks tool grant and filters."""
    config = managed_team_config(tmp_path)
    config.agents["lead"].tools = ["calculator"]
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    job = replace(
        completed_delegation_job(),
        kind="tool",
        tool_name="add",
        toolkit_name="calculator",
        adapter={
            "origin": {"module": "agno.tools.calculator", "qualname": "CalculatorTools.add"},
            "authority": {
                **authority_snapshot(config, "lead"),
                "construction": {
                    "name": "calculator",
                    "factory_origin": tool_registry_origin("calculator"),
                    "config_signature": tool_config_signature(None),
                },
            },
        },
    )
    assert coordinator._authorized(job)
    config.agents["lead"].tools = []
    assert not coordinator._authorized(job)


@pytest.mark.asyncio
async def test_native_admission_reserves_foreground_delivery(tmp_path: Path) -> None:
    """Native admission reserves foreground delivery."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    fixture = completed_delegation_job()
    done = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        done.set()
        return BackgroundOutcome("completed", "answer")

    try:
        job, claim = await start_delegation(
            coordinator.runtime,
            delegation_child(fixture),
            owner=fixture.owner,
            operation=operation,
            cancel=keep_child,
        )
        await done.wait()
        assert await coordinator.runtime.pending_outcomes() == []
        waited = await coordinator.runtime.wait(job.job_id, owner=fixture.owner, depth=0, claim=claim)
        assert waited.claim == claim
        await coordinator.runtime.release_wait(job.job_id, waited.claim)
        assert len(await coordinator.runtime.pending_outcomes()) == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", ["signal", "timer"])
async def test_completion_worker_retries_transient_authorization_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wake: str,
) -> None:
    """A failed Matrix-state read cannot strand accepted outcomes or require config reload."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    runtime = coordinator.runtime
    job = await finish_delegation_job(coordinator)
    failed, delivered = asyncio.Event(), asyncio.Event()
    read_state = matrix_state._load_matrix_state_file
    matrix_state._load_matrix_state_file_cached.cache_clear()

    def transient_state_read(*args: object, **kwargs: object) -> matrix_state.MatrixState:
        if not failed.is_set():
            failed.set()
            message = "transient Matrix-state read"
            raise OSError(message)
        return read_state(*args, **kwargs)

    async def completed(saved: BackgroundJob) -> None:
        assert saved == job
        delivered.set()

    bot = coordinator.bot_provider("team")
    assert bot is not None
    bot.wake_tool_job_completion.side_effect = completed
    monkeypatch.setattr(matrix_state, "_load_matrix_state_file", transient_state_read)
    monkeypatch.setattr(runtime_module, "_RETRY_SECONDS", 0.01 if wake == "timer" else 60)
    try:
        await coordinator.sync()
        worker = coordinator._task
        assert worker is not None
        await asyncio.wait_for(failed.wait(), JOB_TEST_TIMEOUT)
        if wake == "signal":
            runtime.changed.set()
        await asyncio.wait_for(delivered.wait(), JOB_TEST_TIMEOUT)
        assert coordinator._task is worker
        assert not worker.done()
        assert coordinator.runtime is runtime
        assert (await runtime.lookup(job.job_id, owner=job.owner, depth=0)).result == "Saved answer"
        assert await runtime.pending_outcomes() == [job]
    finally:
        await asyncio.wait_for(coordinator.stop(), JOB_TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_retained_child_leaf_checks_current_grant_and_native_ancestry(tmp_path: Path) -> None:
    """A retained child keeps exact transport ancestry but loses execution after a grant is revoked."""
    config = managed_team_config(tmp_path)
    config.agents["worker"].tools = ["calculator"]
    coordinator = delivery_coordinator(tmp_path, config)
    await coordinator.initialize()
    owner = replace(completed_delegation_job().owner, agent_name="worker", session_id="child_session")
    child = delegation_child(completed_delegation_job())
    child.execution_identity = serialize_tool_execution_identity(owner)
    await start_child_turn(
        child,
        parent_run_id="parent",
        config=config,
        runtime_paths=coordinator.runtime_paths,
        caller_execution_identity=completed_delegation_job().owner,
    )
    function = Function(name="add", entrypoint=lambda: None)
    toolkit = Toolkit(name="calculator", auto_register=False)
    toolkit.functions["add"] = function
    bind_toolkit_construction(toolkit, ToolConstruction.from_factory("calculator", TOOL_REGISTRY["calculator"]))
    bind_toolkit_authority(toolkit, authored_name="calculator")
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "worker"))
    register_background_runtime(coordinator.runtime_paths, coordinator.runtime)
    try:
        with tool_runtime_context(
            _delegate_runtime_context(config, coordinator.runtime_paths, execution_identity=owner),
        ):
            with pytest.raises(JobAccessError):
                coordinator._authorize_execution(owner, function)
            async with child_run_context(child, config=config, runtime_paths=coordinator.runtime_paths):
                coordinator._authorize_execution(owner, function)
                config.agents["worker"].tools = []
                with pytest.raises(JobAccessError):
                    coordinator._authorize_execution(owner, function)
                config.agents["worker"].tools = ["calculator"]
                config.agents["lead"].delegate_to = []
                with pytest.raises(JobAccessError):
                    coordinator._authorize_execution(owner, function)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_accepted_execution_continues_while_room_membership_resolves(tmp_path: Path) -> None:
    """A running job's leaf call is stopped by a proven denial, never by membership that is still resolving."""
    config = managed_team_config(tmp_path)
    config.agents["worker"].tools = ["calculator"]
    for entity in (config.agents["lead"], config.agents["worker"], config.teams["team"]):
        entity.access = ResponderAccessConfig(current_room_members=True)
    coordinator = delivery_coordinator(tmp_path, config)
    membership = _ResolvingMembership()
    coordinator.agent_reply_memberships = membership
    await coordinator.initialize()
    owner = replace(completed_delegation_job().owner, agent_name="worker", session_id="child_session")
    child = delegation_child(completed_delegation_job())
    child.execution_identity = serialize_tool_execution_identity(owner)
    await start_child_turn(
        child,
        parent_run_id="parent",
        config=config,
        runtime_paths=coordinator.runtime_paths,
        caller_execution_identity=completed_delegation_job().owner,
    )
    function = Function(name="add", entrypoint=lambda: None)
    toolkit = Toolkit(name="calculator", auto_register=False)
    toolkit.functions["add"] = function
    bind_toolkit_construction(toolkit, ToolConstruction.from_factory("calculator", TOOL_REGISTRY["calculator"]))
    bind_toolkit_authority(toolkit, authored_name="calculator")
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "worker"))
    register_background_runtime(coordinator.runtime_paths, coordinator.runtime)
    try:
        with tool_runtime_context(
            _delegate_runtime_context(config, coordinator.runtime_paths, execution_identity=owner),
        ):
            async with child_run_context(child, config=config, runtime_paths=coordinator.runtime_paths):
                for state in ("pending", "member"):
                    membership.state = state
                    coordinator._authorize_execution(owner, function)
                membership.state = "stranger"
                with pytest.raises(JobAccessError):
                    coordinator._authorize_execution(owner, function)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_retained_oauth_bridge_rechecks_current_remote_filters(tmp_path: Path) -> None:
    """The final leaf arguments cannot bypass current MCP filters through a catalog-free OAuth bridge."""
    config = managed_team_config(tmp_path)
    config.mcp_servers = {"demo": _oauth_server_config()}
    config.agents["lead"].tools = ["mcp_demo"]
    sync_mcp_tool_registry(config)
    coordinator = delivery_coordinator(tmp_path, config)
    await coordinator.initialize()
    owner = completed_delegation_job().owner
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=None,
        catalog=None,
        tool_name="mcp_demo",
        server_config=config.mcp_servers["demo"],
    )
    function = next(item for name, item in toolkit.async_functions.items() if name.endswith("_call_tool"))
    bind_toolkit_construction(toolkit, ToolConstruction.from_factory("mcp_demo", TOOL_REGISTRY["mcp_demo"]))
    bind_toolkit_authority(toolkit, authored_name="mcp_demo")
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "lead"))
    register_background_runtime(coordinator.runtime_paths, coordinator.runtime)
    try:
        with (
            tool_runtime_context(
                _delegate_runtime_context(config, coordinator.runtime_paths, execution_identity=owner),
            ),
            authorized_tool_call(owner, FunctionCall(function=function, arguments={"tool_name": "read"})),
        ):
            check_current_execution_authority()
            config.mcp_servers["demo"] = config.mcp_servers["demo"].model_copy(update={"exclude_tools": ["write"]})
            check_current_execution_authority()
            with pytest.raises(JobAccessError):
                check_current_execution_authority(arguments={"tool_name": "write"})
            config.mcp_servers["demo"] = config.mcp_servers["demo"].model_copy(update={"exclude_tools": ["read"]})
            with pytest.raises(JobAccessError):
                check_current_execution_authority()
    finally:
        await coordinator.stop()
        sync_mcp_tool_registry(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authored", "function_name"),
    [
        ("matrix_message", "list_attachments"),
        ("openclaw_compat", "run_shell_command"),
        ("compact_context", "compact_context"),
    ],
)
async def test_expanded_tool_authority_retains_exact_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authored: str,
    function_name: str,
) -> None:
    """Implied filters and preset child factory identity survive Function copying and saved-result projection."""
    config = managed_team_config(tmp_path)
    config.agents["lead"].tools = [
        ToolConfigEntry(
            name=authored,
            overrides={"include_tools": ["matrix_message"]} if authored == "matrix_message" else {},
        ),
    ]
    config.memory.backend = "none"
    config.models["default"] = ModelConfig(provider="openai", id="gpt-6-astra")
    coordinator = delivery_coordinator(tmp_path, config)
    await coordinator.initialize()
    owner = completed_delegation_job().owner
    register_background_runtime(coordinator.runtime_paths, coordinator.runtime)
    try:
        with tool_runtime_context(
            _delegate_runtime_context(config, coordinator.runtime_paths, execution_identity=owner),
        ):
            agent = create_agent(
                "lead",
                config,
                coordinator.runtime_paths,
                execution_identity=owner,
                persist_runtime_state=False,
            )
            function = next(
                function
                for toolkit in agent.tools
                for function in toolkit.get_async_functions().values()
                if function.name == function_name
            ).model_copy(deep=True)
            function._agent = agent
            assert function.owning_toolkit == authored
            stored = replace(
                completed_delegation_job(),
                kind="tool",
                tool_name=function_name,
                toolkit_name=authored,
                adapter=json.loads(
                    json.dumps({"authority": function_authority(function), "origin": function_provenance(function)}),
                ),
            )
            coordinator._authorize_execution(owner, function)
            assert coordinator._authorized(stored)
            if authored == "openclaw_compat":

                def replaced_factory() -> type[Toolkit]:
                    pytest.fail("Current authority must not construct replacement tools")

                monkeypatch.setitem(TOOL_REGISTRY, "shell", replaced_factory)
                with pytest.raises(JobAccessError):
                    coordinator._authorize_execution(owner, function)
                assert not coordinator._authorized(stored)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_factory_replaced_during_constructor_cannot_relabel_old_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapped: bool,
) -> None:
    """Registry mutation inside construction cannot authorize the old implementation as its replacement."""
    config = managed_team_config(tmp_path)
    config.agents["lead"].tools = ["calculator"]
    coordinator = delivery_coordinator(tmp_path, config)
    await coordinator.initialize()
    owner = completed_delegation_job().owner

    def replacement_factory() -> type[Toolkit]:
        pytest.fail("Authority must not instantiate the replacement")

    class SlowConstruction(Toolkit):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(name="calculator", auto_register=False)
            self.functions["add"] = Function(name="add", entrypoint=lambda: None)
            monkeypatch.setitem(TOOL_REGISTRY, "calculator", replacement_factory)

    def original_factory() -> type[Toolkit]:
        return SlowConstruction

    def wrap(_name: str, toolkit: Toolkit, **_kwargs: object) -> Toolkit:
        proxy = Toolkit(name="proxy", auto_register=False)
        proxy.functions = toolkit.functions.copy()
        return proxy

    monkeypatch.setitem(TOOL_REGISTRY, "calculator", original_factory)
    if wrapped:
        monkeypatch.setattr(metadata_module, "maybe_wrap_toolkit_for_sandbox_proxy", wrap)
    toolkit = get_tool_by_name(
        "calculator",
        coordinator.runtime_paths,
        disable_sandbox_proxy=not wrapped,
        worker_target=None,
    )
    bind_toolkit_authority(toolkit, authored_name="calculator")
    function = toolkit.get_async_functions()["add"].model_copy(deep=True)
    function._agent = bind_actor_authority(Agent(), authority_snapshot(config, "lead"))
    stored = replace(
        completed_delegation_job(),
        kind="tool",
        tool_name="add",
        toolkit_name="calculator",
        adapter={"authority": function_authority(function), "origin": function_provenance(function)},
    )
    register_background_runtime(coordinator.runtime_paths, coordinator.runtime)
    try:
        with tool_runtime_context(
            _delegate_runtime_context(config, coordinator.runtime_paths, execution_identity=owner),
        ):
            with pytest.raises(JobAccessError):
                coordinator._authorize_execution(owner, function)
            assert not coordinator._authorized(stored)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_sync_keeps_the_journal_of_an_initialize_that_ran_before_config(tmp_path: Path) -> None:
    """Parking indexes the startup journal even when configuration arrived only after the first initialize."""
    config: Config | None = None
    paths = test_runtime_paths(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: config, lambda _name: None, AgentReplyMembershipIndex())
    journal = MagicMock()
    await coordinator.initialize(journal)
    config = Config()
    with patch(
        "mindroom.orchestration.tool_job_runtime.index_parked_work",
        new=AsyncMock(return_value=ParkedWork()),
    ) as index:
        await coordinator.sync()
    try:
        index.assert_awaited_once_with(paths, journal)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_cancelled_pause_cards_expire_once_and_retry_after_a_failed_expiry(tmp_path: Path) -> None:
    """The coordinator keeps a cancelled pause until its cards expire, then forgets it."""
    coordinator = ToolJobRuntimeCoordinator(
        test_runtime_paths(tmp_path),
        lambda: managed_team_config(tmp_path),
        lambda _: None,
        AgentReplyMembershipIndex(),
    )
    runtime = tool_job_runtime(tmp_path)
    coordinator._runtime = runtime

    async def pause() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"toolkit_owners": []})

    expired: list[set[str]] = []
    outcomes = iter([False, True])

    async def expire_job_cards(job_ids: set[str]) -> bool:
        expired.append(set(job_ids))
        return next(outcomes)

    manager = MagicMock(expire_job_cards=expire_job_cards)
    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=pause)
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        with patch.object(runtime_module.approval_manager, "get_approval_store", return_value=manager):
            for _ in range(3):
                await coordinator._expire_withdrawn_approval_cards()
    finally:
        await runtime.shutdown()
    assert expired == [{job.job_id}, {job.job_id}]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_recovered_child_records_a_restart_only_when_the_restart_stopped_it(
    tmp_path: Path,
    restart: bool,
) -> None:
    """A crash-interrupted child reads like any restart interruption; a cancelled recovered child stays cancelled."""
    coordinator = ToolJobRuntimeCoordinator(
        test_runtime_paths(tmp_path),
        lambda: managed_team_config(tmp_path),
        lambda _: None,
        AgentReplyMembershipIndex(),
    )
    recorded: list[tuple[str, str]] = []

    async def interrupt(child: object, *, reason: str, status: str = "cancelled", **_kwargs: object) -> None:
        recorded.append((reason, status))
        child.status, child.result = status, reason

    control = JobControl()
    control.cancel(shutdown=restart)
    job = replace(completed_delegation_job(), status="running", result=None)
    with job_control_context(control), patch.object(delegation_recovery, "interrupt_child", new=interrupt):
        outcome = await coordinator._interrupt_child(job)
    assert outcome is not None
    assert recorded == [
        ("Subagent turn was interrupted by a restart. Send a follow-up to continue its history.", "failed")
        if restart
        else ("Background execution was cancelled; tools were not replayed.", "cancelled"),
    ]
    assert outcome.status == ("failed" if restart else "cancelled")
