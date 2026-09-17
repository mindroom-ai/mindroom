"""Startup opt-in and passive recovery for generic tool jobs."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent
from mindroom.approval_recovery import ApprovalRecovery
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    EventClass,
    EventKind,
    InboundEvent,
    JournalEvent,
    ProjectedEvent,
)
from mindroom.handled_turns import TurnRecordCodec
from mindroom.history.session_context import create_scope_session_storage, read_scope_session_run
from mindroom.history.types import HistoryScope
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.disabled import approval_is_parked, event_is_parked
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, get_background_runtime
from mindroom.tool_jobs.settings import (
    background_tool_jobs_enabled,
    pin_background_tool_jobs,
    release_background_tool_jobs,
)
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.turn_record import TurnRecord
from tests.conftest import test_runtime_paths, unwrap_extracted_collaborator
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot
from tests.test_config_lifecycle import _make_lifecycle
from tests.test_subagent_runtime import _job

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from typing import Literal


@pytest.mark.asyncio
async def test_default_startup_does_not_create_job_runtime(tmp_path: Path) -> None:
    """Default startup leaves job persistence and scheduling absent."""
    paths = test_runtime_paths(tmp_path)
    config = Config()
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    try:
        await coordinator.sync()
        assert get_background_runtime(paths) is None
        assert not (paths.storage_root / "tool_jobs").exists()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("team", "saved_private", "storage_change"),
    [
        (False, None, "same"),
        (True, None, "same"),
        (False, None, "shared_user"),
        (True, None, "shared_user"),
        (False, None, "removed"),
        (False, "user", "removed"),
        (False, "user_agent", "removed"),
        (False, None, "changed"),
        (False, "user", "changed"),
        (False, "user_agent", "changed"),
    ],
)
@pytest.mark.parametrize("call_kind", ["job", "wait", "null_wait", "ordinary", "custom_job"])
async def test_pre_execution_approval_is_parked_without_a_job_row(  # noqa: PLR0915
    tmp_path: Path,
    team: bool,
    call_kind: str,
    saved_private: Literal["user", "user_agent"] | None,
    storage_change: str,
) -> None:
    """Read the real saved SDK run without changing its normalized storage or ordinary approvals."""
    bot = _bot(tmp_path)
    paths = bot.runtime_paths
    if saved_private is not None:
        bot.config.agents["general"].private = AgentPrivateConfig(per=saved_private)
    saved_config = bot.config.model_copy(deep=True)
    owner = replace(_job().owner, agent_name="general", transport_agent_name=None)
    scope = HistoryScope(kind="team" if team else "agent", scope_id="general")
    name = "job" if call_kind in {"job", "custom_job"} else "write_report"
    arguments = {"wait_timeout": 0 if call_kind == "wait" else None} if call_kind in {"wait", "null_wait"} else {}
    tool = ToolExecution(tool_call_id="call", tool_name=name, tool_args=arguments, requires_confirmation=True)
    common = {
        "run_id": "paused",
        "session_id": owner.session_id,
        "user_id": owner.requester_id,
        "status": RunStatus.paused,
        "tools": [tool],
    }
    run = TeamRunOutput(team_id="general", **common) if team else RunOutput(agent_id="general", **common)
    storage = create_scope_session_storage(
        agent_name="general",
        scope=scope,
        config=bot.config,
        runtime_paths=paths,
        execution_identity=owner,
    )
    session_user = "@history-owner:localhost" if storage_change == "shared_user" else owner.requester_id
    session = (
        TeamSession(session_id=owner.session_id, team_id="general", user_id=session_user, runs=[run])
        if team
        else AgentSession(session_id=owner.session_id, agent_id="general", user_id=session_user, runs=[run])
    )
    storage.upsert_session(session)
    storage.upsert_run(run, session_id=owner.session_id, user_id=owner.requester_id)
    decoy = replace(
        run,
        run_id="another-run",
        tools=[ToolExecution(tool_call_id="another-call", tool_name="report", tool_args={"wait_timeout": 0})],
    )
    storage.upsert_run(decoy, session_id=owner.session_id, user_id=owner.requester_id)
    storage.close()
    store = bot._journal_store.principal(bot._journal_principal_id)
    await store.admit(
        InboundEvent(
            "$approval-source",
            "!room:localhost",
            "$thread",
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
            owner.requester_id,
            1,
            {"event_id": "$approval-source", "content": {"body": "report"}},
        ),
        ProjectedEvent(
            "$approval-source",
            "!room:localhost",
            "$thread",
            owner.requester_id,
            1,
            {"body": "report"},
            None,
            None,
        ),
    )
    continuation = ApprovalContinuation(
        approval_id="approval",
        run_id="paused",
        session_id=owner.session_id,
        entity_kind=scope.kind,
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id=owner.requester_id,
        response_event_id="$response",
        sources=ResponseSources(("$approval-source",), ("$approval-source",)),
        calls=(
            ApprovalCall(
                "call",
                name,
                "general",
                2**62,
                decision=ApprovalDecision.APPROVED,
                toolkit_name="job" if call_kind == "job" else "reports",
            ),
        ),
        state="ready",
        execution_identity=serialize_tool_execution_identity(owner),
        history_scope=scope,
    )
    assert await store.create_approval_continuation(continuation) is not None
    if storage_change == "removed":
        bot.config.agents.pop("general")
    elif storage_change == "changed":
        bot.config.agents["general"].private = AgentPrivateConfig(
            per="user_agent" if saved_private != "user_agent" else "user",
        )
    before = {path: path.read_bytes() for path in paths.storage_root.rglob("*.db")}
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: bot.config, lambda _: None, AgentReplyMembershipIndex())
    try:
        await coordinator.initialize(bot._journal_store)
        event = await store.load_event("$approval-source")
        assert event is not None
        assert event_is_parked(bot.config, paths, "general", event) is (call_kind not in {"ordinary", "custom_job"})
        assert approval_is_parked(paths, "approval") is (call_kind not in {"ordinary", "custom_job"})
        assert await store.is_pending(event.event_id)
        assert await store.approval_continuation("approval") == continuation
        assert not (paths.storage_root / "tool_jobs").exists()
        assert {path: path.read_bytes() for path in before} == before
        if storage_change == "removed":

            async def unavailable_notice(_continuation: ApprovalContinuation, _reason: str) -> None:
                pytest.fail("No notice can precede card settlement")

            recovery = ApprovalRecovery(
                deliver_unavailable_notice=unavailable_notice,
                journal_provider=lambda: bot._journal_store,
                entity_configured=lambda _: False,
                approval_is_parked=partial(approval_is_parked, paths),
            )
            await recovery._reconcile_unavailable_owner_pages({"general"})
            after_cleanup = await store.approval_continuation("approval")
            assert after_cleanup is not None
            assert (after_cleanup.state == "failing") is (call_kind in {"ordinary", "custom_job"})
            if call_kind not in {"ordinary", "custom_job"}:
                assert after_cleanup == continuation
                assert await store.is_pending(event.event_id)
    finally:
        await coordinator.stop()
    if storage_change != "same" and call_kind not in {"ordinary", "custom_job"}:
        saved_config.background_tool_jobs = True
        restarted = ToolJobRuntimeCoordinator(paths, lambda: saved_config, lambda _: None, AgentReplyMembershipIndex())
        try:
            await restarted.initialize(bot._journal_store)
            assert not event_is_parked(saved_config, paths, "general", event)
            assert not approval_is_parked(paths, "approval")
            claimed = await store.claim_approval_continuation("approval", runtime_generation="enabled-restart")
            assert claimed is not None
            assert claimed.run_id == continuation.run_id
        finally:
            await restarted.stop()


@pytest.mark.parametrize("private_scope", ["user", "user_agent"])
def test_passive_run_reader_preserves_private_root_isolation(
    tmp_path: Path,
    private_scope: Literal["user", "user_agent"],
) -> None:
    """Canonical row users never override exact run selection or requester-private storage roots."""
    paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={"general": AgentConfig(display_name="General", private=AgentPrivateConfig(per=private_scope))},
    )
    owner = replace(_job().owner, agent_name="general", transport_agent_name=None)
    scope = HistoryScope(kind="agent", scope_id="general")
    storage = create_scope_session_storage(
        agent_name="general",
        scope=scope,
        config=config,
        runtime_paths=paths,
        execution_identity=owner,
    )
    run = RunOutput(run_id="exact-run", agent_id="general", session_id=owner.session_id, user_id=None, content="saved")
    storage.upsert_session(
        AgentSession(session_id=owner.session_id, agent_id="general", user_id="@history-owner:localhost"),
    )
    storage.upsert_run(run, session_id=owner.session_id, user_id=None)
    storage.close()
    before = {path: path.read_bytes() for path in paths.storage_root.rglob("*.db")}
    read_run = partial(
        read_scope_session_run,
        agent_name="general",
        scope=scope,
        config=config,
        runtime_paths=paths,
        session_id=owner.session_id,
    )
    saved = read_run(execution_identity=owner, run_id="exact-run")
    assert isinstance(saved, RunOutput)
    assert (saved.run_id, saved.content, saved.user_id) == (run.run_id, "saved", None)
    assert read_run(execution_identity=owner, run_id="missing-run") is None
    assert read_run(execution_identity=replace(owner, requester_id="@another:localhost"), run_id="exact-run") is None
    assert {path: path.read_bytes() for path in paths.storage_root.rglob("*.db")} == before


def test_disabled_delegation_describes_only_available_tools(tmp_path: Path) -> None:
    """Default delegation must not promise a job API or reserved wait argument."""
    config = Config(agents={"lead": AgentConfig(display_name="Lead", delegate_to=["lead"])})
    toolkit = DelegateTools("lead", ["lead"], test_runtime_paths(tmp_path), config)
    instructions = toolkit.instructions or ""
    description = toolkit.async_functions["run_subagent"].description or ""
    assert "wait_timeout" not in instructions + description
    assert "job(action=" not in instructions + description
    assert "continue_subagent" in instructions + description


@pytest.mark.parametrize("initial", [False, True])
def test_reload_reports_restart_and_keeps_effective_mode(tmp_path: Path, initial: bool) -> None:
    """Reload can publish other settings without switching execution ownership."""
    paths = test_runtime_paths(tmp_path)
    config = Config(background_tool_jobs=initial)
    pin_background_tool_jobs(config, paths)
    try:
        changed = Config(background_tool_jobs=not initial, timezone="Europe/Amsterdam")
        lifecycle = _make_lifecycle(tmp_path, current_config=config)
        lifecycle.record_applied(changed)
        assert background_tool_jobs_enabled(changed, paths) is initial
        assert lifecycle.status.status == "restart_required"
        lifecycle.record_applied(config)
        assert lifecycle.status.status == "applied"
    finally:
        release_background_tool_jobs(paths)
    assert pin_background_tool_jobs(changed, paths) is not initial
    release_background_tool_jobs(paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_execution_scope_bypasses_owners_when_disabled(enabled: bool) -> None:
    """Both driver shapes run ordinary work without feature owners while disabled."""

    @partial(owned_tool_execution, enabled=lambda: enabled)
    async def blocking() -> bool:
        return current_execution_resources() is not None

    @partial(owned_tool_execution, enabled=lambda: enabled)
    async def streaming() -> AsyncIterator[bool]:
        yield current_execution_resources() is not None

    assert await blocking() is enabled
    assert [value async for value in streaming()] == [enabled]


@pytest.mark.parametrize("enabled", [False, True])
def test_agent_installs_job_adapters_only_when_enabled(tmp_path: Path, enabled: bool) -> None:
    """Construction in an off process must not patch a provider or the SDK resources."""
    config = Config(
        background_tool_jobs=enabled,
        agents={"lead": AgentConfig(display_name="Lead", tools=[])},
        models={"default": ModelConfig(provider="ollama", id="test")},
    )
    config.memory.backend = "none"
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    agent = create_agent("lead", config, paths, execution_identity=None, persist_runtime_state=False)
    assert agent.model is not None
    assert bool(vars(agent.model).get("_mindroom_tool_jobs")) is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "delegation"])
async def test_disabled_startup_parks_job_sources_and_completion_without_mutation(  # noqa: PLR0915 - One preserved outcome across three startups.
    tmp_path: Path,
    kind: str,
) -> None:
    """Saved ownership parks before approval handoff; new human ingress still dispatches."""
    bot = _bot(tmp_path)
    paths = bot.runtime_paths
    owner = replace(_job().owner, agent_name="general", transport_agent_name=None)
    runtime = ToolJobRuntime(paths.storage_root)

    executions = 0

    async def operation() -> BackgroundOutcome:
        nonlocal executions
        executions += 1
        return BackgroundOutcome("completed", "kept result")

    await runtime.start(
        JobSpec(
            "saved",
            "tool",
            0,
            kind=kind,
            adapter={**(_job().adapter if kind == "delegation" else {}), "source_event_id": "$saved"},
        ),
        owner=owner,
        operation=operation,
    )
    waited = await runtime.wait("saved", owner=owner, depth=0)
    await runtime.release_wait("saved", waited.token)
    await runtime.shutdown()
    path = paths.storage_root / "tool_jobs" / "saved.json"
    original = path.read_bytes()
    record = TurnRecord.create(("$saved", "$sibling"), anchor_event_id="$saved", completed=False)
    await bot._journal_store.turn_records("general").upsert(
        index_event_ids=record.indexed_event_ids,
        anchor_event_id="$saved",
        record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
    )
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: bot.config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    reached: list[str] = []

    async def continuation(event_id: str) -> bool:
        reached.append(event_id)
        return True

    dispatcher = unwrap_extracted_collaborator(bot._journal_dispatcher)
    dispatcher.callbacks = replace(dispatcher.callbacks, on_approval_continuation=continuation)
    dispatcher.release_turn_replay()
    event = JournalEvent("$saved", "!room:localhost", None, EventKind.MESSAGE, "@user:localhost", 1, {}, 1)
    try:
        await coordinator.initialize(bot._journal_store)
        await coordinator.sync()
        assert not await dispatcher._run_event(replace(event, event_id="$sibling"))
        assert not await dispatcher._run_event(event)
        assert dispatcher._deferral_is_live(event)
        completion = replace(event, event_id="$completion", kind=EventKind.TOOL_JOB_COMPLETION)
        assert not await dispatcher._run_event(completion)
        assert dispatcher._deferral_is_live(completion)
        assert await dispatcher._run_event(replace(event, event_id="$new-human"))
        assert reached == ["$new-human"]
        assert path.read_bytes() == original
        assert get_background_runtime(paths) is None
    finally:
        await coordinator.stop()

    bot.config.background_tool_jobs = True
    restarted = ToolJobRuntimeCoordinator(paths, lambda: bot.config, lambda _: None, AgentReplyMembershipIndex())
    try:
        await restarted.initialize(bot._journal_store)
        await restarted.sync()
        recovered = (await restarted.runtime.recover())[0]
        assert recovered is not None
        assert recovered.result == "kept result"
        assert not recovered.wait_acknowledged
        assert executions == 1
        assert await dispatcher._run_event(event)
    finally:
        await restarted.stop()
