"""Durable background delegation completion delivery and lifecycle tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, fields, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from agno.agent import Agent
from agno.tools import Toolkit
from agno.tools.function import Function

import mindroom.orchestration.tool_job_runtime as runtime_module
import mindroom.tool_system.metadata as metadata_module
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent
from mindroom.bot import AgentBot
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ToolConfigEntry
from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.delegation.lifecycle import child_run_context, start_child_turn
from mindroom.delegation.state import DelegationChild
from mindroom.matrix import state as matrix_state
from mindroom.matrix.identity import MatrixID
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.message_target import MessageTarget
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_runner import ResponseRunner, ResponseRunnerDeps
from mindroom.tool_jobs.authorization import (
    AUTHORITY_METADATA_KEY,
    authority_snapshot,
    bind_toolkit_authority,
    function_authority,
)
from mindroom.tool_jobs.control import job_checkpoint
from mindroom.tool_jobs.execution_authority import (
    authorized_tool_call,
    check_current_execution_authority,
    set_execution_authorizer,
)
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.runtime import (
    BackgroundJob,
    BackgroundOutcome,
    JobAccessError,
    get_background_runtime,
    register_background_runtime,
)
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry_state import TOOL_REGISTRY, tool_registry_origins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_mcp_toolkit import _oauth_server_config
from tests.test_queued_message_notify import _envelope

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def _job() -> BackgroundJob:
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="lead",
        requester_id="@human:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:localhost_$thread",
        transport_agent_name="team",
    )
    child = DelegationChild(
        delegation_id="job_123",
        parent_tool_call_id="call_123",
        caller_agent_name="lead",
        child_agent_name="worker",
        task="Inspect files",
        session_id="child_session",
        run_id="child_run",
        model_name="default",
        depth=1,
        execution_identity={},
    )
    return BackgroundJob(
        job_id="job_123",
        tool_name="delegate",
        depth=0,
        kind="delegation",
        adapter={"child": asdict(child)},
        owner=owner,
        status="completed",
        result="Finished @worker",
    )


def _config(tmp_path: Path) -> Config:
    access = ResponderAccessConfig(users=["@human:localhost"], current_room_members=False)
    return bind_runtime_paths(
        Config(
            background_tool_jobs=True,
            agents={
                "lead": AgentConfig(display_name="Lead", delegate_to=["worker"], access=access),
                "worker": AgentConfig(display_name="Worker", access=access),
            },
            teams={"team": TeamConfig(display_name="Team", role="Work", agents=["lead"], access=access)},
        ),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def test_completion_authority_uses_latest_config_and_team_membership(tmp_path: Path) -> None:
    """A config reload cannot leave completion delivery holding old authority."""
    config = _config(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    job = _job()
    assert coordinator._authorized(job)
    config.agents["lead"].delegate_to = []
    assert not coordinator._authorized(job)
    config.agents["lead"].delegate_to = ["worker"]
    config.teams["team"].agents = ["worker"]
    assert not coordinator._authorized(job)


def test_completion_authority_checks_requester_for_target_and_recipient(tmp_path: Path) -> None:
    """Current target or recipient access revocation blocks delivery."""
    config = _config(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    job = _job()
    assert not coordinator._authorized(replace(job, owner=replace(job.owner, requester_id="@stranger:localhost")))
    config.agents["worker"].access = ResponderAccessConfig(current_room_members=False)
    assert not coordinator._authorized(job)


def _delivery_coordinator(tmp_path: Path, config: Config) -> ToolJobRuntimeCoordinator:
    bot = MagicMock(spec=AgentBot)
    bot.running = True
    bot.client = MagicMock(spec=nio.AsyncClient)
    bot.client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=["!room:localhost"]))
    bot.matrix_id = MatrixID.parse("@mindroom_team:localhost")
    return ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda name: bot if name == "team" else None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )


async def _finish_job(coordinator: ToolJobRuntimeCoordinator) -> BackgroundJob:
    fixture = _job()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Saved answer")

    job = await start_delegation(
        coordinator.runtime,
        delegation_child(fixture),
        owner=fixture.owner,
        operation=operation,
    )
    result = await coordinator.runtime.wait(job.job_id, owner=job.owner, depth=0)
    await coordinator.runtime.release_wait(job.job_id, result.token)
    return result.job


@pytest.mark.asyncio
async def test_live_wait_claim_suppresses_completion_delivery(tmp_path: Path) -> None:
    """The delivery loop cannot race a result awaiting parent persistence."""
    coordinator = _delivery_coordinator(tmp_path, _config(tmp_path))
    job = await _finish_job(coordinator)
    waiting = await coordinator.runtime.wait(job.job_id, owner=job.owner, depth=0)
    bot = coordinator.bot_provider("team")
    assert bot is not None
    await coordinator.deliver_pending()
    bot.wake_tool_job_completion.assert_not_awaited()
    await coordinator.runtime.acknowledge_wait(job.job_id, waiting.token)
    await coordinator.deliver_pending()
    bot.wake_tool_job_completion.assert_not_awaited()
    await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_withdraws_service_and_interrupts_live_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown owns detached tasks and removes the managed runtime lookup."""
    monkeypatch.setattr(runtime_module, "interrupt_child", AsyncMock())
    coordinator = _delivery_coordinator(tmp_path, _config(tmp_path))
    await coordinator.sync()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError

    fixture = _job()
    await start_delegation(coordinator.runtime, delegation_child(fixture), owner=fixture.owner, operation=operation)
    await started.wait()
    assert get_background_runtime(coordinator.runtime_paths) is coordinator.runtime
    owner = coordinator.runtime
    await coordinator.sync()
    assert coordinator.runtime is owner
    await coordinator.stop()
    assert cancelled.is_set()
    assert get_background_runtime(coordinator.runtime_paths) is None
    restored = _delivery_coordinator(tmp_path, _config(tmp_path))
    await restored.runtime.recover()
    job = await restored.runtime.lookup(fixture.job_id, owner=fixture.owner, depth=0)
    assert job.status == "interrupted"
    await restored.stop()


def test_constructing_orchestrator_support_does_not_claim_runtime_storage(tmp_path: Path) -> None:
    """Only a started service may own the exclusive job-store lease."""
    config = _config(tmp_path)
    first = _delivery_coordinator(tmp_path, config)
    second = _delivery_coordinator(tmp_path, config)
    assert first is not second
    assert not (first.runtime_paths.storage_root / "tool_jobs").exists()


@pytest.mark.asyncio
async def test_authority_revoked_during_membership_read_prevents_send(
    tmp_path: Path,
) -> None:
    """An asynchronous room check cannot preserve pre-reload requester authority."""
    config = _config(tmp_path)
    coordinator = _delivery_coordinator(tmp_path, config)
    await _finish_job(coordinator)
    bot = coordinator.bot_provider("team")
    assert bot is not None
    assert bot.client is not None

    async def membership() -> nio.JoinedRoomsResponse:
        config.agents["lead"].delegate_to = []
        return nio.JoinedRoomsResponse(rooms=["!room:localhost"])

    bot.client.joined_rooms = membership
    await coordinator.deliver_pending()
    bot.wake_tool_job_completion.assert_not_awaited()
    await coordinator.stop()


@pytest.mark.asyncio
async def test_replaced_response_runner_releases_wait_without_pausing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement transport must signal jobs launched by its retired runner."""
    monkeypatch.setattr(runtime_module, "interrupt_child", AsyncMock())
    coordinator = _delivery_coordinator(tmp_path, _config(tmp_path))
    runtime = coordinator.runtime
    register_background_runtime(coordinator.runtime_paths, runtime)
    deps = MagicMock(spec=[definition.name for definition in fields(ResponseRunnerDeps)])
    deps.runtime_paths = coordinator.runtime_paths
    deps.agent_name = "team"
    original = ResponseRunner(deps)
    target = MessageTarget.resolve("!room:localhost", "$thread", "$human")
    signal = original._lifecycle_coordinator._get_or_create_queued_signal(target).human_signal
    advance, checkpoint_reached, tool_executed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await advance.wait()
        checkpoint_reached.set()
        await job_checkpoint()
        tool_executed.set()
        return BackgroundOutcome("completed", "Executed")

    fixture = _job()
    job = await start_delegation(
        runtime,
        delegation_child(fixture),
        owner=fixture.owner,
        operation=operation,
        human_signal=signal,
    )
    waiting = asyncio.create_task(runtime.wait(job.job_id, owner=fixture.owner, depth=0))
    await asyncio.sleep(0)
    replacement = ResponseRunner(deps)
    unrelated_thread = target.with_thread_root("$unrelated")
    replacement._lifecycle_coordinator.reserve_waiting_human_message(
        target=unrelated_thread,
        response_envelope=_envelope(target=unrelated_thread),
    )
    other_deps = MagicMock(spec=[definition.name for definition in fields(ResponseRunnerDeps)])
    other_deps.runtime_paths = coordinator.runtime_paths
    other_deps.agent_name = "lead"
    other_transport = ResponseRunner(other_deps)
    other_transport._lifecycle_coordinator.reserve_waiting_human_message(
        target=target,
        response_envelope=_envelope(target=target),
    )
    assert (await runtime.lookup(job.job_id, owner=fixture.owner, depth=0)).status == "running"
    assert not waiting.done()
    replacement._lifecycle_coordinator.reserve_waiting_human_message(
        target=target,
        response_envelope=_envelope(target=target),
    )
    released = await asyncio.wait_for(waiting, 1)
    assert released.job.status == "running"
    advance.set()
    await checkpoint_reached.wait()
    signal.clear()
    completed = await runtime.wait(job.job_id, owner=fixture.owner, depth=0)
    try:
        assert completed.job.status == "completed"
        assert tool_executed.is_set()
    finally:
        await runtime.release_wait(job.job_id, completed.token)
        await coordinator.stop()


def test_ordinary_job_authority_tracks_tool_grant_and_filters(tmp_path: Path) -> None:
    """Ordinary job authority tracks tool grant and filters."""
    config = _config(tmp_path)
    config.agents["lead"].tools = ["calculator"]
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    job = replace(
        _job(),
        kind="tool",
        tool_name="add",
        toolkit_name="calculator",
        adapter={
            "origin": {"module": "agno.tools.calculator", "qualname": "CalculatorTools.add"},
            "authority": {
                **authority_snapshot(config, "lead"),
                "construction": {"name": "calculator", "factory_origin": tool_registry_origins()["calculator"]},
            },
        },
    )
    assert coordinator._authorized(job)
    config.agents["lead"].tools = []
    assert not coordinator._authorized(job)


@pytest.mark.asyncio
async def test_native_admission_reserves_foreground_delivery(tmp_path: Path) -> None:
    """Native admission reserves foreground delivery."""
    coordinator = _delivery_coordinator(tmp_path, _config(tmp_path))
    fixture = _job()
    done = asyncio.Event()
    foreground_claim = "foreground"

    async def operation() -> BackgroundOutcome:
        done.set()
        return BackgroundOutcome("completed", "answer")

    try:
        job = await start_delegation(
            coordinator.runtime,
            delegation_child(fixture),
            owner=fixture.owner,
            operation=operation,
            initial_wait_token=foreground_claim,
        )
        await done.wait()
        assert await coordinator.runtime.pending_outcomes() == []
        waited = await coordinator.runtime.wait(
            job.job_id,
            owner=fixture.owner,
            depth=0,
            reserved_token=foreground_claim,
        )
        assert waited.token == foreground_claim
        await coordinator.runtime.release_wait(job.job_id, waited.token)
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
    coordinator = _delivery_coordinator(tmp_path, _config(tmp_path))
    runtime = coordinator.runtime
    job = await _finish_job(coordinator)
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
        await asyncio.wait_for(failed.wait(), 1)
        if wake == "signal":
            runtime.changed.set()
        await asyncio.wait_for(delivered.wait(), 1)
        assert coordinator._task is worker
        assert not worker.done()
        assert coordinator.runtime is runtime
        assert (await runtime.lookup(job.job_id, owner=job.owner, depth=0)).result == "Saved answer"
        assert await runtime.pending_outcomes() == [job]
    finally:
        await asyncio.wait_for(coordinator.stop(), 1)


@pytest.mark.asyncio
async def test_retained_child_leaf_checks_current_grant_and_native_ancestry(tmp_path: Path) -> None:
    """A retained child keeps exact transport ancestry but loses execution after a grant is revoked."""
    config = _config(tmp_path)
    config.agents["worker"].tools = ["calculator"]
    coordinator = _delivery_coordinator(tmp_path, config)
    owner = replace(_job().owner, agent_name="worker", session_id="child_session")
    child = delegation_child(_job())
    child.execution_identity = serialize_tool_execution_identity(owner)
    await start_child_turn(
        child,
        parent_run_id="parent",
        config=config,
        runtime_paths=coordinator.runtime_paths,
        caller_execution_identity=_job().owner,
    )
    function = Function(name="add", entrypoint=lambda: None)
    toolkit = Toolkit(name="calculator", auto_register=False)
    toolkit.functions["add"] = function
    bind_toolkit_construction(toolkit, ToolConstruction.from_factory("calculator", TOOL_REGISTRY["calculator"]))
    bind_toolkit_authority(toolkit, authored_name="calculator")
    function._agent = Agent(metadata={AUTHORITY_METADATA_KEY: authority_snapshot(config, "worker")})
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
async def test_retained_oauth_bridge_rechecks_current_remote_filters(tmp_path: Path) -> None:
    """The final leaf arguments cannot bypass current MCP filters through a catalog-free OAuth bridge."""
    config = _config(tmp_path)
    config.mcp_servers = {"demo": _oauth_server_config()}
    config.agents["lead"].tools = ["mcp_demo"]
    sync_mcp_tool_registry(config)
    coordinator = _delivery_coordinator(tmp_path, config)
    owner = _job().owner
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
    function._agent = Agent(metadata={AUTHORITY_METADATA_KEY: authority_snapshot(config, "lead")})
    register_background_runtime(coordinator.runtime_paths, coordinator.runtime)
    set_execution_authorizer(coordinator.runtime_paths, coordinator._authorize_execution)
    try:
        with (
            tool_runtime_context(
                _delegate_runtime_context(config, coordinator.runtime_paths, execution_identity=owner),
            ),
            authorized_tool_call(owner, function, arguments={"tool_name": "read"}),
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
    config = _config(tmp_path)
    config.agents["lead"].tools = [
        ToolConfigEntry(
            name=authored,
            overrides={"include_tools": ["matrix_message"]} if authored == "matrix_message" else {},
        ),
    ]
    config.memory.backend = "none"
    config.models["default"] = ModelConfig(provider="openai", id="gpt-6-astra")
    coordinator = _delivery_coordinator(tmp_path, config)
    owner = _job().owner
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
                _job(),
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
    config = _config(tmp_path)
    config.agents["lead"].tools = ["calculator"]
    coordinator = _delivery_coordinator(tmp_path, config)
    owner = _job().owner

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
    function._agent = Agent(metadata={AUTHORITY_METADATA_KEY: authority_snapshot(config, "lead")})
    stored = replace(
        _job(),
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
