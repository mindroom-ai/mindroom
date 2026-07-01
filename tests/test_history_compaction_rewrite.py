"""Tests for working-session compaction rewrite, chunk persistence, and compaction hooks."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.models import CompactionOverrideConfig
from mindroom.constants import (
    MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
)
from mindroom.history.compaction import (
    _build_summary_input,
    _emit_compaction_hook,
    _rewrite_working_session_for_compaction,
    _strip_stale_anthropic_replay_fields,
    compact_scope_history,
    estimate_prompt_visible_history_tokens,
)
from mindroom.history.storage import (
    read_scope_state,
    write_scope_state,
)
from mindroom.history.summary_call import CompactionSummaryOutputLimitError
from mindroom.history.types import (
    CompactionLifecycleProgress,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_COMPACTION_AFTER,
    EVENT_COMPACTION_BEFORE,
    CompactionHookContext,
    HookRegistry,
    build_hook_matrix_admin,
    hook,
)
from mindroom.hooks.types import default_timeout_ms_for_event, validate_event_name
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.token_budget import estimate_text_tokens
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    FakeModel,
    make_conversation_cache_mock,
    make_event_cache_mock,
    prepare_history_for_run_for_test,
)
from tests.history_helpers import (  # noqa: F401
    RecordingCompactionLifecycle,
    _agent,
    _close_test_storages,
    _completed_run,
    _forced_compaction_context,
    _hook_runtime_context,
    _make_config,
    _plugin,
    _session,
)


@pytest.mark.asyncio
async def test_rewrite_passes_full_summary_input_budget_into_chunk_construction(tmp_path: Path) -> None:
    """Regression for ISSUE-216: rewrite must forward the full summary_input_budget.

    Locks the contract that one healthy pass folds every selected run in one summary
    call sized at the full resolved budget, with no hidden per-call cap by any name.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    runs = [
        _completed_run(
            f"run-{index}",
            messages=[
                Message(role="user", content=f"run-{index} user " + ("u" * 20_000)),
                Message(role="assistant", content=f"run-{index} assistant " + ("a" * 20_000)),
            ],
        )
        for index in range(1, 6)
    ]
    working_session = _session("session-1", runs=runs)
    summary_inputs: list[str] = []

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=fake_summary),
        ),
        patch(
            "mindroom.history.compaction._build_summary_input",
            wraps=_build_summary_input,
        ) as build_summary_input_spy,
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=None,
            selected_run_ids=tuple(f"run-{index}" for index in range(1, 6)),
            summary_input_budget=70_000,
            before_tokens=0,
            runs_before=len(runs),
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is not None
    assert len(summary_inputs) == 1
    assert build_summary_input_spy.call_count == 1
    assert build_summary_input_spy.call_args.kwargs["max_input_tokens"] == 70_000
    assert "run-1 user" in summary_inputs[0]
    assert "run-5 user" in summary_inputs[0]
    assert rewrite_result.compacted_run_count == 5


@pytest.mark.asyncio
async def test_rewrite_retries_summary_with_smaller_chunk_after_timeout(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 8_000),
                    Message(role="assistant", content="a" * 8_000),
                ],
            ),
        ],
    )
    summary_inputs: list[str] = []

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        if len(summary_inputs) == 1:
            msg = f"compaction summary timed out after {MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS}s"
            raise RuntimeError(msg)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=fake_summary),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=None,
            selected_run_ids=("run-1",),
            summary_input_budget=8_000,
            before_tokens=0,
            runs_before=1,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is not None
    assert len(summary_inputs) == 2
    assert estimate_text_tokens(summary_inputs[1]) < estimate_text_tokens(summary_inputs[0])


@pytest.mark.asyncio
async def test_rewrite_retries_summary_with_smaller_chunk_after_output_cap(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 8_000),
                    Message(role="assistant", content="a" * 8_000),
                ],
            ),
        ],
    )
    summary_inputs: list[str] = []

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        if len(summary_inputs) == 1:
            msg = "renamed owned output-limit signal"
            raise CompactionSummaryOutputLimitError(msg)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=fake_summary),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=None,
            selected_run_ids=("run-1",),
            summary_input_budget=8_000,
            before_tokens=0,
            runs_before=1,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is not None
    assert len(summary_inputs) == 2
    assert estimate_text_tokens(summary_inputs[1]) < estimate_text_tokens(summary_inputs[0])


def test_compaction_hook_events_are_registered() -> None:
    assert EVENT_COMPACTION_BEFORE in BUILTIN_EVENT_NAMES
    assert EVENT_COMPACTION_AFTER in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_COMPACTION_BEFORE) == EVENT_COMPACTION_BEFORE
    assert validate_event_name(EVENT_COMPACTION_AFTER) == EVENT_COMPACTION_AFTER
    with pytest.raises(ValueError, match="reserved namespace"):
        validate_event_name("compaction:custom")
    assert default_timeout_ms_for_event(EVENT_COMPACTION_BEFORE) == 15000
    assert default_timeout_ms_for_event(EVENT_COMPACTION_AFTER) == 5000


@pytest.mark.asyncio
async def test_prepare_history_for_run_emits_compaction_before_and_after_hooks(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    observed: list[tuple[str, list[str], int, int | None, str | None]] = []

    @hook(EVENT_COMPACTION_BEFORE, priority=10)
    async def before_first(ctx: CompactionHookContext) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert [run.run_id for run in persisted.runs or []] == ["run-1", "run-2"]
        observed.append(
            (
                ctx.event_name,
                ctx.scope.key,
                [str(message.content) for message in ctx.messages],
                ctx.token_count_before,
                ctx.token_count_after,
                ctx.compaction_summary,
            ),
        )

    @hook(EVENT_COMPACTION_BEFORE, priority=20)
    async def before_second(ctx: CompactionHookContext) -> None:
        observed.append((f"{ctx.event_name}:second", [], 0, None, None))

    @hook(EVENT_COMPACTION_AFTER)
    async def after(ctx: CompactionHookContext) -> None:
        observed.append(
            (
                ctx.event_name,
                ctx.scope.key,
                [str(message.content) for message in ctx.messages],
                ctx.token_count_before,
                ctx.token_count_after,
                ctx.compaction_summary,
            ),
        )

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before_first, before_second, after])])
    agent = _agent(db=storage)
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed[0] == (
        "compaction:before",
        "agent:test_agent",
        ["run-1 question", "run-1 answer", "run-2 question", "run-2 answer"],
        observed[0][3],
        None,
        None,
    )
    assert observed[1] == ("compaction:before:second", [], 0, None, None)
    assert observed[2] == (
        "compaction:after",
        "agent:test_agent",
        ["run-1 question", "run-1 answer", "run-2 question", "run-2 answer"],
        observed[2][3],
        prepared.compaction_outcomes[0].after_tokens,
        "merged summary",
    )
    assert observed[0][3] == prepared.compaction_outcomes[0].before_tokens
    assert observed[2][3] == prepared.compaction_outcomes[0].before_tokens


@pytest.mark.asyncio
async def test_compact_scope_history_emits_before_hook_for_each_persisted_chunk(tmp_path: Path) -> None:
    """Every destructive compaction chunk should expose raw messages before persistence."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    first_run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="u" * 200),
            Message(role="assistant", content="a" * 200),
        ],
    )
    second_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="v" * 200),
            Message(role="assistant", content="b" * 200),
        ],
    )
    session = _session("session-1", runs=[first_run, second_run])
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    summary_input_budget = next(
        budget
        for budget in range(1, 5_000)
        if len(
            _build_summary_input(
                previous_summary=None,
                compacted_runs=[first_run, second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and len(
            _build_summary_input(
                previous_summary="merged summary",
                compacted_runs=[second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
    )
    observed: list[tuple[str, list[str], list[str]]] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(ctx: CompactionHookContext) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        observed.append(
            (
                ctx.event_name,
                [run.run_id for run in persisted.runs or []],
                [str(message.content) for message in ctx.messages],
            ),
        )

    @hook(EVENT_COMPACTION_AFTER)
    async def after(ctx: CompactionHookContext) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        observed.append(
            (
                ctx.event_name,
                [run.run_id for run in persisted.runs or []],
                [str(message.content) for message in ctx.messages],
            ),
        )

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(),
            history_settings=history_settings,
            available_history_budget=1,
            summary_input_budget=summary_input_budget,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            active_context_window=16_000,
            replay_window_tokens=16_000,
            threshold_tokens=1,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    assert observed == [
        ("compaction:before", ["run-1", "run-2"], ["u" * 200, "a" * 200]),
        ("compaction:before", ["run-2"], ["v" * 200, "b" * 200]),
        ("compaction:after", [], ["u" * 200, "a" * 200, "v" * 200, "b" * 200]),
    ]


@pytest.mark.asyncio
async def test_prepare_history_for_run_does_not_emit_compaction_hooks_for_no_op_branch(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    storage.upsert_session(session)

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(_ctx: CompactionHookContext) -> None:
        observed.append("before")

    @hook(EVENT_COMPACTION_AFTER)
    async def after(_ctx: CompactionHookContext) -> None:
        observed.append("after")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )
    lifecycle = RecordingCompactionLifecycle()

    with tool_runtime_context(runtime_context):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert prepared.compaction_outcomes == []
    assert observed == []
    assert lifecycle.events == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_does_not_collect_compaction_messages_without_hooks(tmp_path: Path) -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )

    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(
        tmp_path,
        session=session,
        registry=HookRegistry.empty(),
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
        patch(
            "mindroom.history.compaction._messages_for_runs",
            side_effect=AssertionError("compaction messages should not be collected without hooks"),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1


@pytest.mark.asyncio
async def test_prepare_history_for_run_does_not_emit_compaction_hooks_when_rewrite_returns_none(
    tmp_path: Path,
) -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(_ctx: CompactionHookContext) -> None:
        observed.append("before")

    @hook(EVENT_COMPACTION_AFTER)
    async def after(_ctx: CompactionHookContext) -> None:
        observed.append("after")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    config, runtime_paths, storage, scope, runtime_context = _forced_compaction_context(
        tmp_path,
        session=session,
        registry=registry,
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._rewrite_working_session_for_compaction",
            new=AsyncMock(return_value=None),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert len(persisted.runs or []) == 2
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []
    assert observed == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_applies_compaction_hook_agent_and_room_scopes(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE, agents=["test_agent"], rooms=["!room:localhost"])
    async def matching(ctx: CompactionHookContext) -> None:
        observed.append(f"{ctx.scope.key}:{ctx.agent_name}:{ctx.room_id}:{ctx.thread_id}")

    @hook(EVENT_COMPACTION_BEFORE, agents=["other_agent"], rooms=["!room:localhost"])
    async def wrong_agent(ctx: CompactionHookContext) -> None:
        observed.append(f"wrong-agent:{ctx.agent_name}")

    @hook(EVENT_COMPACTION_BEFORE, agents=["test_agent"], rooms=["!elsewhere:localhost"])
    async def wrong_room(ctx: CompactionHookContext) -> None:
        observed.append(f"wrong-room:{ctx.room_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [matching, wrong_agent, wrong_room])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed == ["agent:test_agent:test_agent:!room:localhost:$thread"]


@pytest.mark.asyncio
async def test_compaction_hooks_use_team_scope_agent_name(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    observed: list[str] = []
    saw_matrix_admin: list[bool] = []

    @hook(EVENT_COMPACTION_BEFORE, agents=["team_general"], rooms=["!room:localhost"])
    async def matching(ctx: CompactionHookContext) -> None:
        saw_matrix_admin.append(ctx.matrix_admin is not None)
        observed.append(f"{ctx.scope.key}:{ctx.agent_name}:{ctx.room_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [matching])])
    client = AsyncMock()
    runtime_context = ToolRuntimeContext(
        agent_name="router",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        session_id="session-1",
        hook_registry=registry,
        correlation_id="corr-compaction",
        matrix_admin=build_hook_matrix_admin(client, runtime_paths),
    )

    with tool_runtime_context(runtime_context):
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=HistoryScope(kind="team", scope_id="team_general"),
            messages=[Message(role="user", content="hello")],
            session_id="session-1",
            token_count_before=10,
            token_count_after=None,
            compaction_summary=None,
        )

    assert observed == ["team:team_general:team_general:!room:localhost:$thread"]
    assert saw_matrix_admin == [True]


@pytest.mark.asyncio
async def test_compaction_hooks_continue_after_timeout(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE, priority=10, timeout_ms=10)
    async def slow_before(_ctx: CompactionHookContext) -> None:
        observed.append("slow")
        await asyncio.sleep(0.05)

    @hook(EVENT_COMPACTION_BEFORE, priority=20)
    async def fast_before(ctx: CompactionHookContext) -> None:
        observed.append(f"fast:{ctx.session_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [slow_before, fast_before])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed == ["slow", "fast:session-1"]


@pytest.mark.asyncio
async def test_compaction_hooks_continue_after_runtime_error(tmp_path: Path) -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE, priority=10)
    async def failing(_ctx: CompactionHookContext) -> None:
        observed.append("failed")
        msg = "hook failed"
        raise RuntimeError(msg)

    @hook(EVENT_COMPACTION_BEFORE, priority=20)
    async def fast(ctx: CompactionHookContext) -> None:
        observed.append(f"fast:{ctx.session_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [failing, fast])])
    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(
        tmp_path,
        session=session,
        registry=registry,
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed == ["failed", "fast:session-1"]


def test_private_strip_stale_anthropic_replay_fields_returns_zero_without_user_messages() -> None:
    assistant = Message(
        role="assistant",
        content="assistant",
        provider_data={"signature": "sig-1", "keep": "yes"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
    )

    assert _strip_stale_anthropic_replay_fields([assistant]) == 0
    assert assistant.provider_data == {"signature": "sig-1", "keep": "yes"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


def test_private_strip_stale_anthropic_replay_fields_preserves_single_turn_after_last_user() -> None:
    assistant = Message(
        role="assistant",
        content="assistant",
        provider_data={"signature": "sig-1"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
    )
    messages = [
        Message(role="user", content="question"),
        assistant,
    ]

    assert _strip_stale_anthropic_replay_fields(messages) == 0
    assert assistant.provider_data == {"signature": "sig-1"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


def test_private_strip_stale_anthropic_replay_fields_strips_old_assistants_and_preserves_current_turn() -> None:
    old_assistant = Message(
        role="assistant",
        content="old assistant",
        provider_data={"signature": "sig-old", "keep": "yes"},
        reasoning_content="old thinking",
        redacted_reasoning_content="old redacted",
    )
    current_assistant = Message(
        role="assistant",
        content="current assistant",
        provider_data={"signature": "sig-current"},
        reasoning_content="current thinking",
        redacted_reasoning_content="current redacted",
    )
    messages = [
        Message(role="user", content="old user"),
        old_assistant,
        Message(role="user", content="current user"),
        current_assistant,
    ]

    assert _strip_stale_anthropic_replay_fields(messages) == 1
    assert old_assistant.provider_data == {"keep": "yes"}
    assert old_assistant.reasoning_content is None
    assert old_assistant.redacted_reasoning_content is None
    assert current_assistant.provider_data == {"signature": "sig-current"}
    assert current_assistant.reasoning_content == "current thinking"
    assert current_assistant.redacted_reasoning_content == "current redacted"


def test_private_strip_stale_anthropic_replay_fields_preserves_tool_chain_after_last_user() -> None:
    tool_assistant = Message(
        role="assistant",
        content="tool call",
        provider_data={"signature": "sig-tool"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
        tool_calls=[
            {"id": "call-1", "type": "function", "function": {"name": "tool", "arguments": "{}"}},
        ],
    )
    final_assistant = Message(
        role="assistant",
        content="final answer",
        provider_data={"signature": "sig-final"},
        reasoning_content="more thinking",
        redacted_reasoning_content="more redacted",
    )
    messages = [
        Message(role="user", content="question"),
        tool_assistant,
        Message(role="tool", content="tool result", tool_call_id="call-1"),
        final_assistant,
    ]

    assert _strip_stale_anthropic_replay_fields(messages) == 0
    assert tool_assistant.provider_data == {"signature": "sig-tool"}
    assert tool_assistant.reasoning_content == "thinking"
    assert tool_assistant.redacted_reasoning_content == "redacted"
    assert final_assistant.provider_data == {"signature": "sig-final"}
    assert final_assistant.reasoning_content == "more thinking"
    assert final_assistant.redacted_reasoning_content == "more redacted"


def test_private_strip_stale_anthropic_replay_fields_ignores_reasoning_without_signature() -> None:
    assistant = Message(
        role="assistant",
        content="assistant",
        provider_data={"keep": "yes"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
    )
    messages = [
        Message(role="user", content="old user"),
        assistant,
        Message(role="user", content="current user"),
    ]

    assert _strip_stale_anthropic_replay_fields(messages) == 0
    assert assistant.provider_data == {"keep": "yes"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


@pytest.mark.asyncio
async def test_rewrite_working_session_for_compaction_strips_stale_replay_fields_from_remaining_runs(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    remaining_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="old user"),
            Message(
                role="assistant",
                content="old assistant",
                provider_data={"signature": "sig-old", "keep": "yes"},
                reasoning_content="old thinking",
                redacted_reasoning_content="old redacted",
            ),
            Message(role="user", content="current user"),
            Message(
                role="assistant",
                content="current assistant",
                provider_data={"signature": "sig-current"},
                reasoning_content="current thinking",
                redacted_reasoning_content="current redacted",
            ),
        ],
    )
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            remaining_run,
        ],
    )
    summary_text = "merged summary " * 40
    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if len(
            _build_summary_input(
                previous_summary=None,
                compacted_runs=list(working_session.runs or []),
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and _build_summary_input(
            previous_summary=summary_text,
            compacted_runs=[remaining_run],
            max_input_tokens=budget,
        )[1]
        == []
    )

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            selected_run_ids=("run-1", "run-2"),
            summary_input_budget=summary_input_budget,
            before_tokens=0,
            runs_before=2,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )
    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 1
    assert [run.run_id for run in working_session.runs] == ["run-2"]
    remaining_messages = working_session.runs[0].messages or []
    assert remaining_messages[1].provider_data == {"keep": "yes"}
    assert remaining_messages[1].reasoning_content is None
    assert remaining_messages[1].redacted_reasoning_content is None
    assert remaining_messages[3].provider_data == {"signature": "sig-current"}
    assert remaining_messages[3].reasoning_content == "current thinking"
    assert remaining_messages[3].redacted_reasoning_content == "current redacted"


@pytest.mark.asyncio
async def test_compact_scope_history_ignores_runs_without_stable_ids(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    unremovable_run = RunOutput(
        run_id=None,
        agent_id="test_agent",
        status=RunStatus.completed,
        messages=[
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
        ],
    )
    working_session = _session("session-1", runs=[unremovable_run])

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary="summary", updated_at=datetime.now(UTC))),
    ) as mock_generate:
        outcome = await compact_scope_history(
            storage=storage,
            session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            summary_input_budget=16_000,
            active_context_window=64_000,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is None
    # The durable row moved relative to the state this run read (nothing was
    # ever persisted), so the concurrent-writer-wins clear refuses to write.
    assert get_agent_session(storage, "session-1") is None
    assert mock_generate.await_count == 0
    assert working_session.summary is None
    assert working_session.runs == [unremovable_run]


@pytest.mark.asyncio
async def test_compact_scope_history_persists_sanitized_remaining_runs(tmp_path: Path) -> None:
    """Final compaction persist should copy sanitized remaining runs onto the latest session."""
    config, _runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, _runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    remaining_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="old user"),
            Message(
                role="assistant",
                content="old assistant",
                provider_data={"signature": "sig-old", "keep": "yes"},
                reasoning_content="old thinking",
                redacted_reasoning_content="old redacted",
            ),
            Message(role="user", content="current user"),
            Message(
                role="assistant",
                content="current assistant",
                provider_data={"signature": "sig-current"},
                reasoning_content="current thinking",
                redacted_reasoning_content="current redacted",
            ),
        ],
    )
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            remaining_run,
        ],
    )
    storage.upsert_session(session)
    summary_text = "merged summary " * 40
    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if len(
            _build_summary_input(
                previous_summary=None,
                compacted_runs=list(session.runs or []),
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and _build_summary_input(
            previous_summary=summary_text,
            compacted_runs=[remaining_run],
            max_input_tokens=budget,
        )[1]
        == []
    )

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            summary_input_budget=summary_input_budget,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            active_context_window=16_000,
            replay_window_tokens=16_000,
            threshold_tokens=1,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs or []] == ["run-2"]
    remaining_messages = (persisted.runs or [])[0].messages or []
    assert remaining_messages[1].provider_data == {"keep": "yes"}
    assert remaining_messages[1].reasoning_content is None
    assert remaining_messages[1].redacted_reasoning_content is None
    assert remaining_messages[3].provider_data == {"signature": "sig-current"}
    assert remaining_messages[3].reasoning_content == "current thinking"
    assert remaining_messages[3].redacted_reasoning_content == "current redacted"


@pytest.mark.asyncio
async def test_rewrite_working_session_emits_progress_after_persisted_chunks(tmp_path: Path) -> None:
    """Visible compaction should update progress after each durable non-final chunk."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    first_run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="u" * 200),
            Message(role="assistant", content="a" * 200),
        ],
    )
    second_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="v" * 200),
            Message(role="assistant", content="b" * 200),
        ],
    )
    working_session = _session("session-1", runs=[first_run, second_run])
    storage.upsert_session(working_session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    before_tokens = estimate_prompt_visible_history_tokens(
        session=working_session,
        scope=scope,
        history_settings=history_settings,
    )
    summary_input_budget = next(
        budget
        for budget in range(1, 5_000)
        if len(
            _build_summary_input(
                previous_summary=None,
                compacted_runs=[first_run, second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and len(
            _build_summary_input(
                previous_summary="merged summary",
                compacted_runs=[second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
    )
    progress_events: list[CompactionLifecycleProgress] = []

    async def record_progress(event: CompactionLifecycleProgress) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.summary is not None
        assert [run.run_id for run in persisted.runs or []] == ["run-2"]
        progress_events.append(event)

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(),
            history_settings=history_settings,
            available_history_budget=1,
            selected_run_ids=("run-1", "run-2"),
            summary_input_budget=summary_input_budget,
            before_tokens=before_tokens,
            runs_before=2,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            lifecycle_notice_event_id="$notice",
            progress_callback=record_progress,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 2
    assert len(progress_events) == 1
    assert progress_events[0].notice_event_id == "$notice"
    assert progress_events[0].mode == "auto"
    assert progress_events[0].session_id == "session-1"
    assert progress_events[0].scope == "agent:test_agent"
    assert progress_events[0].summary_model == "summary-model"
    assert progress_events[0].before_tokens == before_tokens
    assert progress_events[0].compacted_run_count == 1
    assert progress_events[0].runs_before == 2
    assert progress_events[0].runs_remaining == 1
