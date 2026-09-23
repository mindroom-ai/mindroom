"""Startup opt-in and passive recovery for generic tool jobs."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import pytest
import yaml
from agno.models.response import ModelResponse
from pydantic import ValidationError

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig
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
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.disabled import approval_is_parked, event_is_parked
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, get_background_runtime
from mindroom.tool_jobs.settings import (
    background_tool_jobs_enabled,
    pending_background_tool_jobs_restart,
    pin_background_tool_jobs,
    release_background_tool_jobs,
)
from mindroom.turn_record import TurnRecord
from tests.conftest import test_runtime_paths, unwrap_extracted_collaborator
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot
from tests.test_config_lifecycle import _make_lifecycle
from tests.test_delegation_execution import DelegationModel
from tests.test_subagent_runtime import _job

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.parametrize("exclusions", [None, [], ["shell", "native_plugin"]])
def test_background_job_yaml_round_trip(exclusions: list[str] | None) -> None:
    """The public mapping preserves explicit toolkit exclusions and the shell default."""
    settings: dict[str, object] = {"enabled": True}
    if exclusions is not None:
        settings["exclude_toolkits"] = exclusions
    config = Config.model_validate(yaml.safe_load(yaml.safe_dump({"background_tool_jobs": settings})))
    assert config.background_tool_jobs.enabled
    assert config.background_tool_jobs.exclude_toolkits == (["shell"] if exclusions is None else exclusions)
    assert Config.model_validate(config.model_dump(mode="json")).background_tool_jobs == config.background_tool_jobs


@pytest.mark.parametrize("settings", [True, {"exclude_toolkits": "shell"}, {"exclude_toolkit": ["shell"]}])
def test_background_job_yaml_rejects_invalid_settings(settings: object) -> None:
    """Reject the old scalar and malformed lists instead of silently ignoring policy."""
    with pytest.raises(ValidationError):
        Config.model_validate({"background_tool_jobs": settings})


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_authority_stays_out_of_saved_metadata_with_startup_feature_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """Ordinary saved runs omit job authority, including after an ignored config toggle."""
    config = Config(
        agents={"lead": AgentConfig(display_name="Lead", tools=[], learning=False)},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
    )
    config.memory.backend = "none"
    config.defaults.tools = []
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    pin_background_tool_jobs(config, paths)
    config.background_tool_jobs.enabled = not enabled
    model = DelegationModel(id="test", responses=[ModelResponse(content="done")])
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    owner = _job().owner
    storage = create_session_storage("lead", config, paths, owner)
    try:
        agent = create_agent(
            "lead",
            config,
            paths,
            owner,
            session_id=owner.session_id,
            history_storage=storage,
            include_interactive_questions=False,
        )
        response = await agent.arun("hello", session_id=owner.session_id, user_id=owner.requester_id)
        saved = storage.get_run(response.run_id)
        assert "mindroom_tool_authority" not in (saved.metadata or {})
        assert ("mindroom_tool_authority" in vars(agent)) is enabled
    finally:
        storage.close()
        release_background_tool_jobs(paths)


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
async def test_disabled_startup_parks_only_explicitly_marked_approvals(tmp_path: Path) -> None:
    """Disabled startup uses durable ownership markers without opening SDK session storage."""
    bot = _bot(tmp_path)
    paths = bot.runtime_paths
    store = bot._journal_store.principal(bot._journal_principal_id)

    async def admit_source(event_id: str) -> None:
        await store.admit(
            InboundEvent(
                event_id,
                "!room:localhost",
                "$thread",
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                "@user:localhost",
                1,
                {"event_id": event_id, "content": {"body": "report"}},
            ),
            ProjectedEvent(
                event_id,
                "!room:localhost",
                "$thread",
                "@user:localhost",
                1,
                {"body": "report"},
                None,
                None,
            ),
        )

    async def create_continuation(
        approval_id: str,
        source_event_id: str,
        *,
        requires_background_tool_jobs: bool,
    ) -> ApprovalContinuation:
        await admit_source(source_event_id)
        continuation = ApprovalContinuation(
            approval_id=approval_id,
            run_id=f"run-{approval_id}",
            session_id="session",
            entity_kind="agent",
            entity_name="general",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            response_event_id=f"$response-{approval_id}",
            sources=ResponseSources((source_event_id,), (source_event_id,)),
            calls=(
                ApprovalCall(
                    "call",
                    "write_report",
                    "general",
                    2**62,
                    decision=ApprovalDecision.APPROVED,
                    toolkit_name="reports",
                ),
            ),
            state="ready",
            requires_background_tool_jobs=requires_background_tool_jobs,
        )
        saved = await store.create_approval_continuation(continuation)
        assert saved is not None
        return saved

    feature = await create_continuation("feature", "$feature-source", requires_background_tool_jobs=True)
    ordinary = await create_continuation("ordinary", "$ordinary-source", requires_background_tool_jobs=False)

    def remove_ordinary_marker(transaction: object) -> None:
        row = transaction.fetchone(  # type: ignore[attr-defined]
            "SELECT context_json FROM approval_continuations WHERE approval_id = ?",
            (ordinary.approval_id,),
        )
        context = json.loads(str(row["context_json"]))
        context.pop("requires_background_tool_jobs")
        transaction.execute(  # type: ignore[attr-defined]
            "UPDATE approval_continuations SET context_json = ? WHERE approval_id = ?",
            (json.dumps(context), ordinary.approval_id),
        )

    await store._backend.write(remove_ordinary_marker)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: bot.config, lambda _: None, AgentReplyMembershipIndex())
    try:
        await coordinator.initialize(bot._journal_store)

        assert approval_is_parked(paths, feature.approval_id)
        assert not approval_is_parked(paths, ordinary.approval_id)
        assert await store.is_pending("$feature-source")
        assert await store.is_pending("$ordinary-source")
        feature_event = await store.load_event("$feature-source")
        ordinary_event = await store.load_event("$ordinary-source")
        assert feature_event is not None
        assert ordinary_event is not None
        assert event_is_parked(bot.config, paths, "general", feature_event)
        assert not event_is_parked(bot.config, paths, "general", ordinary_event)
    finally:
        await coordinator.stop()


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
    config = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=initial))
    pin_background_tool_jobs(config, paths)
    try:
        changed = Config(
            background_tool_jobs=BackgroundToolJobsConfig(enabled=not initial),
            timezone="Europe/Amsterdam",
        )
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


def test_exclusion_order_is_not_an_execution_policy_change(tmp_path: Path) -> None:
    """Reordering toolkit exclusions does not claim a process restart is needed."""
    paths = test_runtime_paths(tmp_path)
    config = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=True, exclude_toolkits=["shell", "plugin"]))
    pin_background_tool_jobs(config, paths)
    try:
        config.background_tool_jobs.exclude_toolkits = ["plugin", "shell", "shell"]
        assert not pending_background_tool_jobs_restart(config, paths)
        config.background_tool_jobs.exclude_toolkits = ["plugin"]
        assert pending_background_tool_jobs_restart(config, paths)
    finally:
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
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
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

    bot.config.background_tool_jobs.enabled = True
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
