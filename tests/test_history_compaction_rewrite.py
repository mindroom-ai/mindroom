"""Tests for working-session compaction rewrite, chunk persistence, and compaction hooks."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agno.exceptions import ContextWindowExceededError, ModelProviderError, ModelRateLimitError
from agno.models.message import Message
from agno.models.openai.chat import OpenAIChat
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.summary import SessionSummary
from structlog.testing import capture_logs

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.models import CompactionOverrideConfig
from mindroom.constants import (
    MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
)
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.history.compaction import (
    _build_summary_input,
    _CarriedSummaryUnfitError,
    _emit_compaction_hook,
    _previous_summary_block,
    _resolve_compaction_sizing_context,
    _rewrite_working_session_for_compaction,
    _strip_stale_anthropic_replay_fields,
    _summary_digest,
    compact_scope_history,
    estimate_prompt_visible_history_tokens,
)
from mindroom.history.policy import persistable_summary_limit
from mindroom.history.storage import (
    read_scope_state,
    record_compaction_chunk,
    set_force_compaction_state,
    write_scope_state,
)
from mindroom.history.summary_call import (
    DEFAULT_SUMMARY_RETRY_POLICY,
    CompactionSummaryOutputLimitError,
    CompactionSummaryOversizedOutputError,
)
from mindroom.history.types import (
    CarriedSummaryUnfitMarker,
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
from mindroom.message_target import MessageTarget
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
    _ALL_HISTORY_SETTINGS,
    _SUMMARY_MODEL_BOUND,
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.session.agent import AgentSession

    from mindroom.history.compaction import _CompactionRewriteResult


async def _rewrite_single_run(
    *,
    storage: BaseDb,
    working_session: AgentSession,
    selected_run_ids: tuple[str, ...] = ("run-1",),
    summary_input_budget: int = 8_000,
    summary_model: Model | None = None,
    fallback_summary_model: Model | None = None,
    fallback_summary_model_name: str | None = None,
    fallback_summary_input_budget: int | None = None,
    state: HistoryScopeState | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
) -> _CompactionRewriteResult | None:
    if fallback_summary_model is not None and fallback_summary_input_budget is None:
        fallback_summary_input_budget = summary_input_budget
    return await _rewrite_working_session_for_compaction(
        storage=storage,
        persisted_session=working_session,
        working_session=working_session,
        summary_model=summary_model or FakeModel(id="summary-model", provider="fake"),
        summary_model_name="summary-model",
        fallback_summary_model=fallback_summary_model,
        fallback_summary_model_name=fallback_summary_model_name,
        fallback_summary_input_budget=fallback_summary_input_budget,
        session_id=working_session.session_id,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        state=state if state is not None else HistoryScopeState(force_compact_before_next_run=True),
        history_settings=_ALL_HISTORY_SETTINGS,
        available_history_budget=None,
        selected_run_ids=selected_run_ids,
        summary_input_budget=summary_input_budget,
        before_tokens=0,
        runs_before=len(working_session.runs or []),
        threshold_tokens=None,
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        lifecycle_notice_event_id=None,
        progress_callback=progress_callback,
        collect_compaction_hook_messages=False,
    )


@pytest.mark.asyncio
async def test_rewrite_passes_full_summary_input_budget_into_chunk_construction(tmp_path: Path) -> None:
    """Regression for ISSUE-216: rewrite must forward the full summary_input_budget.

    Locks the contract that one healthy pass folds every selected run in one summary
    call sized at the full resolved budget, with no hidden per-call cap by any name.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
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
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            selected_run_ids=tuple(f"run-{index}" for index in range(1, 6)),
            summary_input_budget=300_000,
        )

    assert rewrite_result is not None
    assert len(summary_inputs) == 1
    assert build_summary_input_spy.call_count == 1
    assert build_summary_input_spy.call_args.kwargs["max_input_tokens"] == 300_000
    assert "run-1 user" in summary_inputs[0]
    assert "run-5 user" in summary_inputs[0]
    assert rewrite_result.compacted_run_count == 5


@pytest.mark.asyncio
async def test_rewrite_condenses_an_inherited_oversized_summary_into_durable_summary_only_progress(
    tmp_path: Path,
) -> None:
    """Corner happy path (redesign test 3 + the migration sentinel borrowing).

    An INHERITED stored summary fills the whole 6,000 byte-denominated input
    budget, so no run fits beside it. The rewrite must neither stall on it nor
    truncate it: it condenses the COMPLETE summary in its own request — the
    sentinel in the final Critical Context line reaches the model verbatim —
    persists the model's fitting output IMMEDIATELY as a summary-only chunk
    (empty run set), and then keeps chunking against the condensed text.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    oversized_summary = ("chunk one summary " * 336) + "\n\n## Critical Context\nCRITICAL-CONTEXT-SENTINEL"
    working_session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER " + ("u" * 3_500))]),
            _completed_run("run-2", messages=[Message(role="user", content="RUN2-MARKER " + ("v" * 3_500))]),
        ],
        summary=SessionSummary(summary=oversized_summary, updated_at=datetime.now(UTC)),
    )
    summary_inputs: list[str] = []
    summaries = [
        SessionSummary(summary="condensed carry CRITICAL-CONTEXT-SENTINEL", updated_at=datetime.now(UTC)),
        SessionSummary(summary="chunk one merged summary CRITICAL-CONTEXT-SENTINEL", updated_at=datetime.now(UTC)),
        SessionSummary(summary="final merged summary CRITICAL-CONTEXT-SENTINEL", updated_at=datetime.now(UTC)),
    ]

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        return summaries[len(summary_inputs) - 1]

    persisted_summaries: list[tuple[tuple[str, ...], str]] = []

    def record_and_snapshot(**kwargs: object) -> None:
        record_compaction_chunk(**kwargs)  # type: ignore[arg-type]
        working = kwargs["working_session"]
        assert working.summary is not None  # type: ignore[union-attr]
        persisted_summaries.append(
            (tuple(kwargs["compacted_run_ids"]), working.summary.summary),  # type: ignore[union-attr, arg-type]
        )

    progress_events: list[CompactionLifecycleProgress] = []

    async def record_progress(event: CompactionLifecycleProgress) -> None:
        progress_events.append(event)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=fake_summary),
        ),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            side_effect=record_and_snapshot,
        ),
    ):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            selected_run_ids=("run-1", "run-2"),
            summary_input_budget=6_000,
            progress_callback=record_progress,
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 2
    assert rewrite_result.summary_text == "final merged summary CRITICAL-CONTEXT-SENTINEL"
    assert len(summary_inputs) == 3
    # The condensation request carries the COMPLETE previous summary — the
    # sentinel in the final Critical Context line reaches the model verbatim —
    # and no run content.
    assert oversized_summary in summary_inputs[0]
    assert "CRITICAL-CONTEXT-SENTINEL" in summary_inputs[0]
    assert "<note>" in summary_inputs[0]
    assert "RUN1-MARKER" not in summary_inputs[0]
    assert "RUN2-MARKER" not in summary_inputs[0]
    # The fitting condensation persists FIRST as a summary-only chunk (empty
    # run set), before any run chunk — durable progress, never discarded —
    # and the admitted result carries the sentinel verbatim.
    assert persisted_summaries[0] == ((), "condensed carry CRITICAL-CONTEXT-SENTINEL")
    assert [run_ids for run_ids, _summary in persisted_summaries[1:]] == [("run-1",), ("run-2",)]
    # The lifecycle notice reports both the summary-only progress and the run
    # chunk (the final chunk empties the scope, which suppresses its event).
    assert [(event.compacted_run_count, event.runs_remaining) for event in progress_events] == [(0, 2), (1, 1)]
    # Chunk merges build against the condensed text, never a truncated copy.
    assert "condensed carry CRITICAL-CONTEXT-SENTINEL" in summary_inputs[1]
    assert "RUN1-MARKER" in summary_inputs[1]
    assert "RUN2-MARKER" in summary_inputs[2]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "final merged summary CRITICAL-CONTEXT-SENTINEL"
    assert persisted.runs == []


@pytest.mark.asyncio
async def test_rewrite_keeps_noop_contract_for_degenerate_budget_with_carried_summary(tmp_path: Path) -> None:
    """Pin the guard: at or below 2x the retry floor, a carried summary triggers no model call.

    The planner already reports such budgets unavailable, so the rewrite must
    return None without condensing, truncating, or touching summary and runs.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 975) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    generate_summary = AsyncMock()

    with patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=1_500,
        )

    assert rewrite_result is None
    generate_summary.assert_not_awaited()
    assert working_session.summary is not None
    assert working_session.summary.summary == stored_summary
    assert [run.run_id for run in working_session.runs or []] == ["run-1"]


@pytest.mark.asyncio
async def test_rewrite_sends_condensation_even_when_the_estimate_exceeds_the_budget(tmp_path: Path) -> None:
    """Round-3 stall regression (redesign test 5): no client-side send gate.

    The byte-bound estimate of the complete condensation request (~13,000
    units) dwarfs the 2,100-unit budget — the round-3 population where the
    bound over-estimates ~4.5x — but the provider accepts. The request must be
    SENT, with the complete summary and its tail sentinel, and the fitting
    condensed output must persist durably before chunking continues.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    summary_inputs: list[str] = []
    summaries = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        return summaries[len(summary_inputs) - 1]

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=fake_summary),
    ):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 1
    assert len(summary_inputs) == 2
    # The complete summary — tail sentinel included — reached the provider.
    assert stored_summary in summary_inputs[0]
    assert "RUN1-MARKER" not in summary_inputs[0]
    assert "condensed TAIL-FACT-MUST-SURVIVE" in summary_inputs[1]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"
    assert persisted.runs == []


def test_build_summary_input_accounts_for_wrappers_separators_and_run_indexes() -> None:
    runs = [
        _completed_run(
            f"run-{index}",
            messages=[Message(role="user", content="payload")],
        )
        for index in range(300)
    ]
    full_input, full_runs = _build_summary_input(
        previous_summary="existing summary",
        compacted_runs=runs,
        max_input_tokens=1_000_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=_SUMMARY_MODEL_BOUND,
    )
    assert len(full_runs) == len(runs)

    tight_budget = _SUMMARY_MODEL_BOUND(full_input) - 50
    summary_input, included_runs = _build_summary_input(
        previous_summary="existing summary",
        compacted_runs=runs,
        max_input_tokens=tight_budget,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=_SUMMARY_MODEL_BOUND,
    )

    assert 0 < len(included_runs) < len(runs)
    assert _SUMMARY_MODEL_BOUND(summary_input) <= tight_budget


@pytest.mark.asyncio
async def test_rewrite_retries_summary_with_smaller_chunk_after_timeout(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
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
        rewrite_result = await _rewrite_single_run(storage=storage, working_session=working_session)

    assert rewrite_result is not None
    assert len(summary_inputs) == 2
    assert estimate_text_tokens(summary_inputs[1]) < estimate_text_tokens(summary_inputs[0])


@pytest.mark.asyncio
async def test_rewrite_retries_summary_with_smaller_chunk_after_output_cap(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
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
        rewrite_result = await _rewrite_single_run(storage=storage, working_session=working_session)

    assert rewrite_result is not None
    assert len(summary_inputs) == 2
    assert estimate_text_tokens(summary_inputs[1]) < estimate_text_tokens(summary_inputs[0])


@pytest.mark.asyncio
async def test_rewrite_switches_to_fallback_and_uses_it_for_later_chunks(tmp_path: Path) -> None:
    """A refusal switches once to the fallback; the fallback then serves later chunks and reporting."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="RUN1-MARKER " + ("u" * 16_000)),
                    Message(role="assistant", content="a" * 16_000),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="RUN2-MARKER " + ("u" * 16_000)),
                    Message(role="assistant", content="b" * 16_000),
                ],
            ),
        ],
    )
    primary = FakeModel(id="summary-model", provider="fake")
    fallback = FakeModel(id="fallback-model-id", provider="fake")
    summary_mock = AsyncMock(
        side_effect=[
            ModelSafeguardRefusalError(message="Vertex Claude returned stop_reason=refusal"),
            SessionSummary(summary="first chunk summary", updated_at=datetime.now(UTC)),
            SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
        ],
    )
    retry_sleep = AsyncMock()
    progress_events: list[CompactionLifecycleProgress] = []

    async def record_progress(event: CompactionLifecycleProgress) -> None:
        progress_events.append(event)

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
        patch(
            "mindroom.history.compaction._build_summary_input",
            wraps=_build_summary_input,
        ) as build_summary_input_spy,
    ):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            selected_run_ids=("run-1", "run-2"),
            summary_input_budget=6_000,
            summary_model=primary,
            fallback_summary_model=fallback,
            fallback_summary_model_name="fallback-model",
            progress_callback=record_progress,
        )

    assert rewrite_result is not None
    summary_inputs = [call.kwargs["summary_input"] for call in summary_mock.await_args_list]
    summary_models = [call.kwargs["model"] for call in summary_mock.await_args_list]
    assert len(summary_inputs) == 3
    # The fallback receives the refused first chunk's unchanged summary input.
    assert summary_inputs[1] == summary_inputs[0]
    assert summary_models == [primary, fallback, fallback]
    assert "RUN1-MARKER" in summary_inputs[0]
    assert "RUN2-MARKER" in summary_inputs[2]
    retry_sleep.assert_not_awaited()
    # The second chunk rebuilds its input with the fallback's token estimator.
    assert build_summary_input_spy.call_args_list[-1].kwargs["token_estimator"].keywords["model_id"] == fallback.id
    assert [call.kwargs["compacted_run_ids"] for call in persist_spy.call_args_list] == [
        ("run-1",),
        ("run-2",),
    ]
    assert rewrite_result.compacted_run_ids == ("run-1", "run-2")
    assert rewrite_result.summary_model is fallback
    assert rewrite_result.summary_model_name == "fallback-model"
    assert [event.summary_model for event in progress_events] == ["fallback-model"]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert persisted.runs == []


@pytest.mark.asyncio
async def test_rewrite_propagates_fallback_refusal_without_persisting(tmp_path: Path) -> None:
    """A refusing fallback propagates after its single unchanged-input attempt."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_mock = AsyncMock(
        side_effect=ModelSafeguardRefusalError(message="Vertex Claude returned stop_reason=refusal"),
    )
    retry_sleep = AsyncMock()

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
        pytest.raises(ModelSafeguardRefusalError),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            fallback_summary_model=FakeModel(id="fallback-model-id", provider="fake"),
            fallback_summary_model_name="fallback-model",
        )

    assert summary_mock.await_count == 2
    summary_inputs = [call.kwargs["summary_input"] for call in summary_mock.await_args_list]
    assert summary_inputs[1] == summary_inputs[0]
    retry_sleep.assert_not_awaited()
    persist_spy.assert_not_called()
    assert working_session.summary is None
    assert [run.run_id for run in working_session.runs or []] == ["run-1"]


@pytest.mark.parametrize(
    "error",
    [
        ModelRateLimitError(message="rate limited", status_code=429),
        ModelProviderError(message="request timed out while provider unavailable", status_code=503),
        ModelRateLimitError(message="overloaded", status_code=529),
    ],
)
@pytest.mark.asyncio
async def test_rewrite_retries_transient_provider_error_with_same_input_and_one_persist(
    tmp_path: Path,
    error: ModelProviderError,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 16_000),
                    Message(role="assistant", content="a" * 16_000),
                ],
            ),
        ],
    )
    summary_mock = AsyncMock(
        side_effect=[
            error,
            SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
        ],
    )
    retry_sleep = AsyncMock()

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
    ):
        rewrite_result = await _rewrite_single_run(storage=storage, working_session=working_session)

    assert rewrite_result is not None
    summary_inputs = [call.kwargs["summary_input"] for call in summary_mock.await_args_list]
    assert len(summary_inputs) == 2
    assert summary_inputs[1] == summary_inputs[0]
    retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)
    assert persist_spy.call_count == 1
    assert persist_spy.call_args.kwargs["compacted_run_ids"] == ("run-1",)


@pytest.mark.parametrize(
    ("error", "expected_attempts", "expected_delay"),
    [
        pytest.param(
            ModelSafeguardRefusalError(message="Vertex Claude returned stop_reason=refusal"),
            1,
            False,
            id="unshrinkable-refusal",
        ),
        pytest.param(
            ModelProviderError(message="service unavailable", status_code=503),
            2,
            True,
            id="transient-provider-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_rewrite_bounds_retry_attempts(
    tmp_path: Path,
    error: ModelProviderError,
    expected_attempts: int,
    expected_delay: bool,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_mock = AsyncMock(side_effect=error)
    retry_sleep = AsyncMock()

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
        pytest.raises(type(error)),
    ):
        await _rewrite_single_run(storage=storage, working_session=working_session)

    assert summary_mock.await_count == expected_attempts
    if expected_delay:
        retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)
    else:
        retry_sleep.assert_not_awaited()
    persist_spy.assert_not_called()
    assert working_session.summary is None
    assert [run.run_id for run in working_session.runs or []] == ["run-1"]


@pytest.mark.asyncio
async def test_rewrite_propagates_non_retryable_provider_error_without_persisting(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_mock = AsyncMock(side_effect=ModelProviderError(message="invalid request", status_code=400))

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
        pytest.raises(ModelProviderError, match="invalid request"),
    ):
        await _rewrite_single_run(storage=storage, working_session=working_session)

    assert summary_mock.await_count == 1
    persist_spy.assert_not_called()


@pytest.mark.asyncio
async def test_rewrite_propagates_cancellation_without_retrying_or_persisting(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_mock = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
        pytest.raises(asyncio.CancelledError),
    ):
        await _rewrite_single_run(storage=storage, working_session=working_session)

    assert summary_mock.await_count == 1
    persist_spy.assert_not_called()


@pytest.mark.asyncio
async def test_rewrite_propagates_cancellation_during_transient_retry_delay(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_mock = AsyncMock(
        side_effect=[
            ModelRateLimitError(message="rate limited", status_code=429),
            SessionSummary(summary="must not run", updated_at=datetime.now(UTC)),
        ],
    )
    delay_started = asyncio.Event()

    async def wait_for_cancellation(_seconds: float) -> None:
        delay_started.set()
        await asyncio.Future()

    retry_sleep = AsyncMock(side_effect=wait_for_cancellation)
    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            wraps=record_compaction_chunk,
        ) as persist_spy,
    ):
        rewrite_task = asyncio.create_task(_rewrite_single_run(storage=storage, working_session=working_session))
        await delay_started.wait()
        rewrite_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rewrite_task

    assert summary_mock.await_count == 1
    retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)
    persist_spy.assert_not_called()
    assert working_session.summary is None
    assert [run.run_id for run in working_session.runs or []] == ["run-1"]


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
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=_SUMMARY_MODEL_BOUND,
            )[1],
        )
        == 1
        and len(
            _build_summary_input(
                previous_summary="merged summary",
                compacted_runs=[second_run],
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=_SUMMARY_MODEL_BOUND,
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
        target=MessageTarget(
            room_id="!room:localhost",
            source_thread_id="$thread",
            resolved_thread_id="$thread",
            reply_to_event_id=None,
            session_id="session-1",
        ),
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
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
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=_SUMMARY_MODEL_BOUND,
            )[1],
        )
        == 1
        and _build_summary_input(
            previous_summary=summary_text,
            compacted_runs=[remaining_run],
            max_input_tokens=budget,
            history_settings=_ALL_HISTORY_SETTINGS,
            token_estimator=_SUMMARY_MODEL_BOUND,
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
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=_SUMMARY_MODEL_BOUND,
            )[1],
        )
        == 1
        and _build_summary_input(
            previous_summary=summary_text,
            compacted_runs=[remaining_run],
            max_input_tokens=budget,
            history_settings=_ALL_HISTORY_SETTINGS,
            token_estimator=_SUMMARY_MODEL_BOUND,
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
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=_SUMMARY_MODEL_BOUND,
            )[1],
        )
        == 1
        and len(
            _build_summary_input(
                previous_summary="merged summary",
                compacted_runs=[second_run],
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=_SUMMARY_MODEL_BOUND,
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


# --- ISSUE-246 redesign: persisted-summary fit invariant and condensation backstop ---

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")


def _fake_summary_model_profile(budget: int = 2_100, model_id: str = "summary-model") -> str:
    """Return the serving-profile fingerprint the default test summary model resolves to."""
    return _resolve_compaction_sizing_context(
        FakeModel(id=model_id, provider="fake"),
        "summary-model",
        budget,
    ).serving_profile


def test_acceptance_limit_guarantees_next_build_includes_a_run() -> None:
    """I1 property test (redesign test 1), pinned to the rebuild path.

    Across the admissible budget range — including the planner floor B=2,001 —
    a summary whose wrapped block sizes at or below persistable_summary_limit
    must always leave room for at least one run, so the acceptance arithmetic
    and _build_summary_input cannot drift apart.
    """
    tiny_run = _completed_run("run-1", messages=[Message(role="user", content="tiny payload")])
    block_overhead = len(_previous_summary_block("").encode())
    for budget in (2_001, 2_100, 6_000, 24_000, 161_616):
        limit = persistable_summary_limit(budget)
        for block_size in (limit, limit - 1, max(block_overhead + 1, limit // 2)):
            summary = "s" * (block_size - block_overhead)
            assert len(_previous_summary_block(summary).encode()) == block_size
            summary_input, included_runs = _build_summary_input(
                previous_summary=summary,
                compacted_runs=[tiny_run],
                history_settings=_ALL_HISTORY_SETTINGS,
                max_input_tokens=budget,
                token_estimator=_SUMMARY_MODEL_BOUND,
            )
            assert included_runs, (budget, block_size)
            # The complete summary rides along untruncated (E1).
            assert summary in summary_input


@pytest.mark.asyncio
async def test_rewrite_rejects_oversized_merge_output_and_persists_nothing(tmp_path: Path) -> None:
    """Redesign test 2: a >L candidate raises typed, shrinks once, then propagates.

    Both attempts return a summary whose block exceeds the acceptance limit,
    so the second failure propagates and the persisted session is untouched.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="u" * 20_000)])],
    )
    storage.upsert_session(working_session)
    oversized_output = "x" * 5_000
    assert len(_previous_summary_block(oversized_output).encode()) > persistable_summary_limit(6_000)
    generate_summary = AsyncMock(
        side_effect=[
            SessionSummary(summary=oversized_output, updated_at=datetime.now(UTC)),
            SessionSummary(summary=oversized_output, updated_at=datetime.now(UTC)),
        ],
    )

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        pytest.raises(CompactionSummaryOversizedOutputError),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=6_000,
        )

    assert generate_summary.await_count == 2
    request_inputs = [call.kwargs["summary_input"] for call in generate_summary.await_args_list]
    assert len(request_inputs[1]) < len(request_inputs[0])
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE).carried_summary_unfit is None


@pytest.mark.asyncio
async def test_condensation_that_cannot_shrink_writes_marker_and_next_pass_is_free(tmp_path: Path) -> None:
    """B3 regression (redesign test 4): identical condensation output converges.

    The model echoes the carried summary byte-identically twice (initial call
    plus the strengthened numeric-target retry), so nothing persists, the
    unfit marker lands durably, the NEXT pass makes zero model calls, and a
    forced compaction bypasses the marker for exactly one fresh attempt-set.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    condense_inputs: list[str] = []

    async def echo_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        condense_inputs.append(summary_input)
        return SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=echo_summary),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert rewrite_result is None
    assert generate_summary.await_count == 2
    # The strengthened retry adds the explicit numeric size target.
    assert "MUST stay under" not in condense_inputs[0]
    assert "MUST stay under" in condense_inputs[1]
    assert stored_summary in condense_inputs[1]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == stored_summary
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    persisted_state = read_scope_state(persisted, _SCOPE)
    marker = persisted_state.carried_summary_unfit
    assert marker is not None
    assert marker.reason == "condensation_not_smaller"
    assert marker.summary_digest == _summary_digest(stored_summary)
    assert (
        marker.serving_profile
        == _resolve_compaction_sizing_context(
            FakeModel(id="summary-model", provider="fake"),
            "summary-model",
            2_100,
        ).serving_profile
    )
    assert marker.summary_input_budget == 2_100
    # The consumed force flag was cleared in the same durable write.
    assert persisted_state.force_compact_before_next_run is False

    # Next pass: the marker matches, so condensation costs zero model calls.
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(),
    ) as second_pass_summary:
        second_result = await _rewrite_single_run(
            storage=storage,
            working_session=persisted,
            state=persisted_state,
            summary_input_budget=2_100,
        )
    assert second_result is None
    second_pass_summary.assert_not_awaited()

    # Forced compaction bypasses the marker for exactly one fresh attempt-set.
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=echo_summary),
    ) as forced_summary:
        forced_result = await _rewrite_single_run(
            storage=storage,
            working_session=persisted,
            state=replace(persisted_state, force_compact_before_next_run=True),
            summary_input_budget=2_100,
        )
    assert forced_result is None
    assert forced_summary.await_count == 2


@pytest.mark.asyncio
async def test_condensation_context_rejection_is_terminal_with_marker_and_remedies(tmp_path: Path) -> None:
    """Redesign test 6: a typed provider context rejection is a durable one-shot verdict.

    Nothing persists, the marker lands with the terminal reason, the raised
    error names the remedies for the lifecycle failure notice, a distinct
    warning is logged, and the next pass makes zero model calls.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=ContextWindowExceededError("prompt is too long for this model")),
        ) as generate_summary,
        capture_logs() as logs,
        pytest.raises(_CarriedSummaryUnfitError, match="raise the compaction model's context_window"),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert generate_summary.await_count == 1
    rejection_warnings = [
        entry for entry in logs if entry["event"] == "Compaction condensation rejected by the provider context window"
    ]
    assert len(rejection_warnings) == 1
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == stored_summary
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    persisted_state = read_scope_state(persisted, _SCOPE)
    marker = persisted_state.carried_summary_unfit
    assert marker is not None
    assert marker.reason == "provider_context_window_rejection"
    assert marker.summary_digest == _summary_digest(stored_summary)

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(),
    ) as second_pass_summary:
        second_result = await _rewrite_single_run(
            storage=storage,
            working_session=persisted,
            state=persisted_state,
            summary_input_budget=2_100,
        )
    assert second_result is None
    second_pass_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_condensation_transient_failure_retries_same_budget_then_persists(tmp_path: Path) -> None:
    """Redesign test 7: a 429 on the backstop gets one delayed same-budget retry, never a shrink."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    summary_inputs: list[str] = []
    outputs = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        if len(summary_inputs) == 1:
            message = "rate limited"
            raise ModelRateLimitError(message, status_code=429)
        return outputs[len(summary_inputs) - 2]

    retry_sleep = AsyncMock()
    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ) as generate_summary,
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
    ):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert rewrite_result is not None
    assert generate_summary.await_count == 3
    retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)
    # The retried condensation request is byte-identical: no shrink decision
    # was ever granted for the complete-summary input.
    assert summary_inputs[1] == summary_inputs[0]
    assert stored_summary in summary_inputs[0]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"
    assert read_scope_state(persisted, _SCOPE).carried_summary_unfit is None


@pytest.mark.asyncio
async def test_condensation_smaller_but_unfit_output_persists_with_marker_on_new_digest(tmp_path: Path) -> None:
    """Redesign test 8: strictly smaller yet unfit output is durable progress plus a marker.

    Discarding it would re-buy the same call, so it persists as a summary-only
    chunk while the marker on the NEW digest prevents any automatic follow-up.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    condensed_summary = ("condensed word " * 300) + "TAIL-FACT-MUST-SURVIVE"
    assert len(_previous_summary_block(condensed_summary).encode()) > persistable_summary_limit(2_100)
    assert len(condensed_summary) < len(stored_summary)
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary=condensed_summary, updated_at=datetime.now(UTC))),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert rewrite_result is None
    assert generate_summary.await_count == 1
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == condensed_summary
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    persisted_state = read_scope_state(persisted, _SCOPE)
    marker = persisted_state.carried_summary_unfit
    assert marker is not None
    assert marker.reason == "condensed_summary_still_unfit"
    assert marker.summary_digest == _summary_digest(condensed_summary)

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(),
    ) as second_pass_summary:
        second_result = await _rewrite_single_run(
            storage=storage,
            working_session=persisted,
            state=persisted_state,
            summary_input_budget=2_100,
        )
    assert second_result is None
    second_pass_summary.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["digest", "profile", "budget"])
async def test_marker_invalidates_when_any_key_dimension_changes(tmp_path: Path, mismatch: str) -> None:
    """Redesign test 9: changing digest, serving profile, or budget re-enables one fresh attempt."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    marker_profile = (
        _fake_summary_model_profile(model_id="other-model") if mismatch == "profile" else _fake_summary_model_profile()
    )
    marker_budget = 4_000 if mismatch == "budget" else 2_100
    marker = CarriedSummaryUnfitMarker(
        summary_digest=_summary_digest("some other summary" if mismatch == "digest" else stored_summary),
        serving_profile=marker_profile,
        summary_input_budget=marker_budget,
        attempt_profiles=(f"{marker_profile}|budget={marker_budget}",),
        failed_at="2026-07-19T00:00:00Z",
        reason="condensation_not_smaller",
    )
    summaries = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=summaries),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            state=HistoryScopeState(carried_summary_unfit=marker),
            summary_input_budget=2_100,
        )

    assert rewrite_result is not None
    assert generate_summary.await_count == 2
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"


@pytest.mark.asyncio
async def test_matching_marker_skips_condensation_without_model_calls(tmp_path: Path) -> None:
    """Redesign test 9 control: a fully matching marker suppresses the attempt outright."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    marker = CarriedSummaryUnfitMarker(
        summary_digest=_summary_digest(stored_summary),
        serving_profile=_fake_summary_model_profile(),
        summary_input_budget=2_100,
        attempt_profiles=(f"{_fake_summary_model_profile()}|budget=2100",),
        failed_at="2026-07-19T00:00:00Z",
        reason="condensation_not_smaller",
    )

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            state=HistoryScopeState(carried_summary_unfit=marker),
            summary_input_budget=2_100,
        )

    assert rewrite_result is None
    generate_summary.assert_not_awaited()
    assert working_session.summary is not None
    assert working_session.summary.summary == stored_summary


@pytest.mark.asyncio
async def test_condensation_call_failure_propagates_with_persisted_state_untouched(tmp_path: Path) -> None:
    """Redesign test 11a: a generic condensation failure leaves everything as it was."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=RuntimeError("provider exploded")),
        ),
        pytest.raises(RuntimeError, match="provider exploded"),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == stored_summary
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE).carried_summary_unfit is None


@pytest.mark.asyncio
async def test_persisted_condensation_survives_a_later_chunk_failure(tmp_path: Path) -> None:
    """Redesign test 11c: summary-only progress is durable even when the next chunk fails (I5)."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(
                side_effect=[
                    SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
                    RuntimeError("provider exploded"),
                ],
            ),
        ),
        pytest.raises(RuntimeError, match="provider exploded"),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "condensed TAIL-FACT-MUST-SURVIVE"
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]


@pytest.mark.asyncio
async def test_inherited_unfit_summary_with_fitting_runs_heals_through_a_normal_chunk(tmp_path: Path) -> None:
    """Migration case (a) (redesign test 12): the common population needs no backstop.

    The stored summary violates the acceptance limit but still leaves room for
    a run, so one ordinary chunk cycle heals it with zero condensation calls.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = "s" * 6_500
    budget = 8_000
    assert len(_previous_summary_block(stored_summary).encode()) > persistable_summary_limit(budget)
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary="healed merged summary", updated_at=datetime.now(UTC))),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=budget,
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 1
    generate_summary.assert_awaited_once()
    merge_input = generate_summary.await_args.kwargs["summary_input"]
    assert "<note>" not in merge_input
    assert stored_summary in merge_input
    assert "RUN1-MARKER" in merge_input
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "healed merged summary"
    assert persisted.runs == []


@pytest.mark.asyncio
async def test_multi_profile_acceptance_rejects_candidate_failing_the_fallback_budget(tmp_path: Path) -> None:
    """Binding codex borrowing B1: acceptance must pass EVERY next-serving profile.

    A candidate under the primary's limit but over the configured fallback's
    smaller-budget limit must not persist; without the fallback profile the
    identical candidate persists, pinning the fallback profile as the rejector.
    """
    config, runtime_paths = _make_config(tmp_path)
    candidate = "y" * 5_000
    assert len(_previous_summary_block(candidate).encode()) <= persistable_summary_limit(12_000)
    assert len(_previous_summary_block(candidate).encode()) > persistable_summary_limit(4_000)

    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="u" * 30_000)])],
    )
    storage.upsert_session(working_session)
    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary=candidate, updated_at=datetime.now(UTC))),
        ) as generate_summary,
        pytest.raises(CompactionSummaryOversizedOutputError, match="fallback-model"),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=12_000,
            fallback_summary_model=FakeModel(id="fallback-model-id", provider="fake"),
            fallback_summary_model_name="fallback-model",
            fallback_summary_input_budget=4_000,
        )
    assert generate_summary.await_count == 2
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    storage.close()

    # Control: with no fallback profile the identical candidate is accepted.
    control_storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    control_session = _session(
        "session-2",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="u" * 30_000)])],
    )
    control_storage.upsert_session(control_session)
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary=candidate, updated_at=datetime.now(UTC))),
    ):
        control_result = await _rewrite_single_run(
            storage=control_storage,
            working_session=control_session,
            summary_input_budget=12_000,
        )
    assert control_result is not None
    control_persisted = get_agent_session(control_storage, "session-2")
    assert control_persisted is not None
    assert control_persisted.summary is not None
    assert control_persisted.summary.summary == candidate


# --- ISSUE-246 review round 6: fallback-budget, fallback condensation, marker fidelity ---


@pytest.mark.asyncio
async def test_fallback_served_chunks_use_the_fallback_budget_after_the_unchanged_resend(tmp_path: Path) -> None:
    """Round-6 F1: only the immediate post-refusal resend keeps the primary-sized bytes.

    Every build, request, and log after the switch is capped by the
    fallback's own resolved budget, asserted on the captured build budgets
    and the actual constructed request bytes.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER " + ("u" * 5_000))]),
            _completed_run("run-2", messages=[Message(role="user", content="RUN2-MARKER " + ("v" * 2_000))]),
        ],
    )
    storage.upsert_session(working_session)
    primary = FakeModel(id="summary-model", provider="fake")
    fallback = FakeModel(id="fallback-model-id", provider="fake")
    attempts: list[tuple[FakeModel, str]] = []

    async def flaky_summary(*, model: FakeModel, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append((model, summary_input))
        if len(attempts) == 1:
            message = "provider-specific refusal wording"
            raise ModelSafeguardRefusalError(message)
        return SessionSummary(summary=f"chunk summary {len(attempts)}", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ),
        patch(
            "mindroom.history.compaction._build_summary_input",
            wraps=_build_summary_input,
        ) as build_spy,
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            selected_run_ids=("run-1", "run-2"),
            summary_input_budget=6_000,
            summary_model=primary,
            fallback_summary_model=fallback,
            fallback_summary_model_name="fallback-model",
            fallback_summary_input_budget=3_000,
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 2
    assert rewrite_result.summary_model is fallback
    assert [model for model, _ in attempts] == [primary, fallback, fallback]
    # The immediate post-refusal resend is byte-identical (unchanged-input
    # contract), so it deliberately keeps the primary-sized request.
    assert attempts[1][1] == attempts[0][1]
    # Every build after the switch is capped by the fallback's own budget.
    assert [call.kwargs["max_input_tokens"] for call in build_spy.call_args_list] == [6_000, 3_000]
    assert len(attempts[2][1].encode("utf-8")) <= 3_000
    assert "RUN2-MARKER" in attempts[2][1]
    request_events = [entry for entry in logs if entry["event"] == "Compaction summary chunk request"]
    assert [entry["summary_input_budget_tokens"] for entry in request_events] == [6_000, 6_000, 3_000]
    assert [entry["model_name"] for entry in request_events] == ["summary-model", "fallback-model", "fallback-model"]


@pytest.mark.asyncio
async def test_condensation_switches_to_the_fallback_which_serves_the_rest_of_the_pass(tmp_path: Path) -> None:
    """Round-6 F2: the condensation backstop honors the configured safeguard fallback.

    A primary refusal resends the unchanged condensation request once to the
    fallback; its fitting output persists as a summary-only chunk, and the
    fallback then serves the remaining chunks, audit state, and outcome.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    primary = FakeModel(id="summary-model", provider="fake")
    fallback = FakeModel(id="fallback-model-id", provider="fake")
    attempts: list[tuple[FakeModel, str]] = []
    outputs = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    async def flaky_summary(*, model: FakeModel, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append((model, summary_input))
        if len(attempts) == 1:
            message = "provider-specific refusal wording"
            raise ModelSafeguardRefusalError(message)
        return outputs[len(attempts) - 2]

    persisted_chunks: list[tuple[str, ...]] = []

    def record_and_snapshot(**kwargs: object) -> None:
        record_compaction_chunk(**kwargs)  # type: ignore[arg-type]
        persisted_chunks.append(tuple(kwargs["compacted_run_ids"]))  # type: ignore[arg-type]

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ),
        patch(
            "mindroom.history.compaction.record_compaction_chunk",
            side_effect=record_and_snapshot,
        ),
    ):
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
            summary_model=primary,
            fallback_summary_model=fallback,
            fallback_summary_model_name="fallback-model",
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 1
    assert [model for model, _ in attempts] == [primary, fallback, fallback]
    # The refused condensation request is resent to the fallback unchanged.
    assert attempts[1][1] == attempts[0][1]
    assert stored_summary in attempts[0][1]
    # Summary-only persistence lands before the fallback-served run chunk.
    assert persisted_chunks == [(), ("run-1",)]
    assert rewrite_result.summary_model is fallback
    assert rewrite_result.summary_model_name == "fallback-model"
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"
    assert persisted.runs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["compat-to-genuine", "trust-flip", "same-profile-control"])
async def test_marker_matches_the_serving_profile_not_the_bare_model_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Round-6 F3: with the model ID and budget fixed, a profile change invalidates the marker.

    An OpenAI-compatible endpoint and genuine OpenAI can both expose gpt-4o;
    switching between them (or flipping endpoint trust via OPENAI_BASE_URL)
    changes the serving profile and must re-enable exactly one fresh attempt,
    while an unchanged profile keeps suppressing it.
    """
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    if scenario == "compat-to-genuine":
        marker_profile_model: FakeModel | OpenAIChat = FakeModel(id="gpt-4o", provider="fake")
    else:
        marker_profile_model = OpenAIChat(id="gpt-4o")
    marker_profile = _resolve_compaction_sizing_context(
        marker_profile_model,
        "summary-model",
        2_100,
    ).serving_profile
    marker = CarriedSummaryUnfitMarker(
        summary_digest=_summary_digest(stored_summary),
        serving_profile=marker_profile,
        summary_input_budget=2_100,
        attempt_profiles=(f"{marker_profile}|budget=2100",),
        failed_at="2026-07-19T00:00:00Z",
        reason="condensation_not_smaller",
    )
    if scenario == "trust-flip":
        # Same class, same ID, same budget — only the endpoint-trust
        # classification (and with it the estimator kind) changes.
        monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9292/v1")
    summaries = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=summaries),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            state=HistoryScopeState(carried_summary_unfit=marker),
            summary_input_budget=2_100,
            summary_model=OpenAIChat(id="gpt-4o"),
        )

    if scenario == "same-profile-control":
        assert rewrite_result is None
        generate_summary.assert_not_awaited()
        assert working_session.summary is not None
        assert working_session.summary.summary == stored_summary
    else:
        assert rewrite_result is not None
        assert generate_summary.await_count == 2
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.summary is not None
        assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"


@pytest.mark.asyncio
async def test_fresh_force_request_written_mid_attempt_survives_the_marker_write(tmp_path: Path) -> None:
    """Round-6 F4: the marker write consumes only the force request that started the attempt.

    A new force request recorded while the condensation call was in flight
    carries a newer generation and must survive the marker's force clear.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    forced_state = set_force_compaction_state(
        working_session,
        _SCOPE,
        read_scope_state(working_session, _SCOPE),
        force=True,
    )
    storage.upsert_session(working_session)
    call_count = 0

    async def echo_and_force_again(**_kwargs: object) -> SessionSummary:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # A fresh manual request lands on the durable row while the
            # provider call is still in flight.
            latest = get_agent_session(storage, "session-1")
            assert latest is not None
            set_force_compaction_state(latest, _SCOPE, read_scope_state(latest, _SCOPE), force=True)
            storage.upsert_session(latest)
        return SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=echo_and_force_again),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            state=forced_state,
            summary_input_budget=2_100,
        )

    assert rewrite_result is None
    assert generate_summary.await_count == 2
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    persisted_state = read_scope_state(persisted, _SCOPE)
    assert persisted_state.carried_summary_unfit is not None
    # The fresh request survives; only the consumed generation was cleared.
    assert persisted_state.force_compact_before_next_run is True
    assert persisted_state.force_compact_generation == forced_state.force_compact_generation + 1


@pytest.mark.asyncio
async def test_untyped_context_window_rejection_is_terminal_for_condensation(tmp_path: Path) -> None:
    """Round-6 F5: fragment-matched context rejections are terminal like the typed error.

    A provider that surfaces the rejection as an untyped message must still
    produce the durable marker, so the doomed request is never re-purchased.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    untyped_rejection = ModelProviderError(
        "Input validation failed: prompt exceeds the maximum context length of this model",
        status_code=400,
    )

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=untyped_rejection),
        ) as generate_summary,
        pytest.raises(_CarriedSummaryUnfitError, match="raise the compaction model's context_window"),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert generate_summary.await_count == 1
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == stored_summary
    persisted_state = read_scope_state(persisted, _SCOPE)
    marker = persisted_state.carried_summary_unfit
    assert marker is not None
    assert marker.reason == "provider_context_window_rejection"

    # The verdict is durable: the next pass spends nothing on the same state.
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(),
    ) as second_pass_summary:
        second_result = await _rewrite_single_run(
            storage=storage,
            working_session=persisted,
            state=persisted_state,
            summary_input_budget=2_100,
        )
    assert second_result is None
    second_pass_summary.assert_not_awaited()


# --- ISSUE-246 review round 7: fingerprint routing identity + ordered attempt-set match ---


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["endpoint-change", "same-endpoint-control"])
async def test_marker_distinguishes_custom_endpoints_with_the_same_model_id(
    tmp_path: Path,
    scenario: str,
) -> None:
    """Round-7 G2: two custom endpoints serving the same model id never share a fingerprint.

    A marker recorded against endpoint A must not suppress the promised fresh
    attempt after switching to endpoint B, while an unchanged endpoint keeps
    suppressing it.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    marker_profile = _resolve_compaction_sizing_context(
        OpenAIChat(id="gpt-4o", base_url="https://endpoint-a.example/v1"),
        "summary-model",
        2_100,
    ).serving_profile
    marker = CarriedSummaryUnfitMarker(
        summary_digest=_summary_digest(stored_summary),
        serving_profile=marker_profile,
        summary_input_budget=2_100,
        attempt_profiles=(f"{marker_profile}|budget=2100",),
        failed_at="2026-07-19T00:00:00Z",
        reason="condensation_not_smaller",
    )
    current_base_url = (
        "https://endpoint-b.example/v1" if scenario == "endpoint-change" else "https://endpoint-a.example/v1"
    )
    summaries = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=summaries),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            state=HistoryScopeState(carried_summary_unfit=marker),
            summary_input_budget=2_100,
            summary_model=OpenAIChat(id="gpt-4o", base_url=current_base_url),
        )

    if scenario == "same-endpoint-control":
        assert rewrite_result is None
        generate_summary.assert_not_awaited()
    else:
        assert rewrite_result is not None
        assert generate_summary.await_count == 2
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.summary is not None
        assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"


def test_serving_profile_fingerprint_covers_client_params_and_prebuilt_client_routing() -> None:
    """Round-7 G2: routing via client_params or a prebuilt client changes the fingerprint.

    Identical routing on fresh instances must produce the identical
    fingerprint (marker matching depends on cross-process stability), while
    any routing change must produce a different one.
    """

    def profile_of(model: OpenAIChat) -> str:
        return _resolve_compaction_sizing_context(model, "summary-model", 2_100).serving_profile

    params_a = profile_of(OpenAIChat(id="gpt-4o", client_params={"base_url": "https://endpoint-a.example/v1"}))
    params_a_again = profile_of(OpenAIChat(id="gpt-4o", client_params={"base_url": "https://endpoint-a.example/v1"}))
    params_b = profile_of(OpenAIChat(id="gpt-4o", client_params={"base_url": "https://endpoint-b.example/v1"}))
    assert params_a == params_a_again
    assert params_a != params_b

    client_a = profile_of(OpenAIChat(id="gpt-4o", http_client=httpx.Client(base_url="https://endpoint-a.example/v1")))
    client_a_again = profile_of(
        OpenAIChat(id="gpt-4o", http_client=httpx.Client(base_url="https://endpoint-a.example/v1")),
    )
    client_b = profile_of(OpenAIChat(id="gpt-4o", http_client=httpx.Client(base_url="https://endpoint-b.example/v1")))
    assert client_a == client_a_again
    assert client_a != client_b

    default_profile = profile_of(OpenAIChat(id="gpt-4o"))
    assert default_profile.endswith("|endpoint=default")
    assert default_profile not in {params_a, client_a}


def test_serving_profile_fingerprint_never_contains_endpoint_secrets() -> None:
    """Round-7 G2: credentials in URLs or client params never reach the persisted fingerprint."""
    model = OpenAIChat(
        id="gpt-4o",
        base_url="https://user:hunter2@secret-endpoint.example/v1?api-key=sk-url-secret",
        client_params={"api_key": "sk-param-secret", "default_headers": {"Authorization": "Bearer sk-header-secret"}},
    )
    fingerprint = _resolve_compaction_sizing_context(model, "summary-model", 2_100).serving_profile
    for secret in ("hunter2", "sk-url-secret", "sk-param-secret", "sk-header-secret", "user:"):
        assert secret not in fingerprint
    # The URL itself is hashed, not embedded, and the identity is stable.
    assert "secret-endpoint.example" not in fingerprint
    again = _resolve_compaction_sizing_context(model, "summary-model", 2_100).serving_profile
    assert fingerprint == again


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["changed-primary", "unchanged-control"])
async def test_changed_primary_with_retained_fallback_gets_its_fresh_attempt(
    tmp_path: Path,
    scenario: str,
) -> None:
    """Round-7 G2 (B2): suppression requires the FULL ordered attempt set unchanged.

    A marker recorded under (old primary, fallback) must not suppress the
    attempt after the primary changes, even though the retained fallback still
    matches; the unchanged pair keeps suppressing it.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="RUN1-MARKER payload")])],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    old_primary_profile = _fake_summary_model_profile(model_id="old-primary")
    fallback = FakeModel(id="fallback-model-id", provider="fake")
    fallback_profile = _resolve_compaction_sizing_context(fallback, "fallback-model", 2_100).serving_profile
    marker = CarriedSummaryUnfitMarker(
        summary_digest=_summary_digest(stored_summary),
        serving_profile=fallback_profile,
        summary_input_budget=2_100,
        attempt_profiles=(f"{old_primary_profile}|budget=2100", f"{fallback_profile}|budget=2100"),
        failed_at="2026-07-19T00:00:00Z",
        reason="provider_context_window_rejection",
    )
    current_primary_id = "new-primary" if scenario == "changed-primary" else "old-primary"
    summaries = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=summaries),
    ) as generate_summary:
        rewrite_result = await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            state=HistoryScopeState(carried_summary_unfit=marker),
            summary_input_budget=2_100,
            summary_model=FakeModel(id=current_primary_id, provider="fake"),
            fallback_summary_model=fallback,
            fallback_summary_model_name="fallback-model",
        )

    if scenario == "unchanged-control":
        assert rewrite_result is None
        generate_summary.assert_not_awaited()
    else:
        assert rewrite_result is not None
        assert generate_summary.await_count == 2
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.summary is not None
        assert persisted.summary.summary == "merged TAIL-FACT-MUST-SURVIVE"


# --- ISSUE-246 review round 7 G3: transient rejections never mint a durable verdict ---


@pytest.mark.asyncio
async def test_condensation_tpm_rate_limit_is_transient_not_terminal(tmp_path: Path) -> None:
    """Round-7 G3 (C's live repro): a TPM 429 saying "request too large" writes NO marker.

    The rate limit gets its one delayed same-budget retry, then propagates as
    itself — not as the terminal unfit error — leaving no marker, so the next
    turn's attempt runs and succeeds.
    """
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stored_summary = ("word " * 2_600) + "TAIL-FACT-MUST-SURVIVE"
    working_session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=stored_summary, updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(working_session)
    tpm_rejection = ModelRateLimitError(
        "Request too large for gpt-4o in organization org-x on tokens per min (TPM): Limit 30000, Requested 62051.",
        status_code=429,
    )

    retry_sleep = AsyncMock()
    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=tpm_rejection),
        ) as generate_summary,
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        pytest.raises(ModelRateLimitError, match="tokens per min"),
    ):
        await _rewrite_single_run(
            storage=storage,
            working_session=working_session,
            summary_input_budget=2_100,
        )

    assert generate_summary.await_count == 2
    retry_sleep.assert_awaited_once()
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == stored_summary
    persisted_state = read_scope_state(persisted, _SCOPE)
    assert persisted_state.carried_summary_unfit is None

    # The verdict was NOT durable: the next turn's attempt runs and succeeds.
    summaries = [
        SessionSummary(summary="condensed TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
        SessionSummary(summary="merged TAIL-FACT-MUST-SURVIVE", updated_at=datetime.now(UTC)),
    ]
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=summaries),
    ) as second_pass_summary:
        second_result = await _rewrite_single_run(
            storage=storage,
            working_session=persisted,
            state=persisted_state,
            summary_input_budget=2_100,
        )
    assert second_result is not None
    assert second_pass_summary.await_count == 2


# --- ISSUE-246 review round 7: force consumption is one compare-by-generation transition ---


def _write_fresh_force_request(storage: BaseDb, session_id: str = "session-1") -> int:
    """Land a fresh manual force request on the durable row mid-attempt."""
    latest = get_agent_session(storage, session_id)
    assert latest is not None
    bumped = set_force_compaction_state(latest, _SCOPE, read_scope_state(latest, _SCOPE), force=True)
    storage.upsert_session(latest)
    return bumped.force_compact_generation


@pytest.mark.asyncio
async def test_fresh_force_request_survives_successful_compaction_finalization(tmp_path: Path) -> None:
    """Round-7 G1a: finalization must not merge stale control fields over a fresh force.

    A fresh manual request lands on the durable row while the summary call is
    in flight; the success-path chunk finalization and force consume must
    leave that request (and its newer generation) intact while the run's own
    audit fields still land.
    """
    session = _session("session-1", runs=[_completed_run("run-1"), _completed_run("run-2")])
    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(tmp_path, session=session)
    fresh_generation: int | None = None

    async def summarize_and_force_again(**_kwargs: object) -> SessionSummary:
        nonlocal fresh_generation
        if fresh_generation is None:
            fresh_generation = _write_fresh_force_request(storage)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=summarize_and_force_again),
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

    assert prepared.compaction_reply_outcome == "success"
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.runs == []
    persisted_state = read_scope_state(persisted, _SCOPE)
    assert persisted_state.force_compact_before_next_run is True
    assert persisted_state.force_compact_generation == fresh_generation
    assert persisted_state.last_compacted_run_count == 2


@pytest.mark.asyncio
async def test_fresh_force_request_survives_an_ordinary_provider_failure(tmp_path: Path) -> None:
    """Round-7 G1b: the failure-path clear consumes only the generation it read."""
    session = _session("session-1", runs=[_completed_run("run-1")])
    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(tmp_path, session=session)
    fresh_generation: int | None = None

    async def fail_after_forcing_again(**_kwargs: object) -> SessionSummary:
        nonlocal fresh_generation
        if fresh_generation is None:
            fresh_generation = _write_fresh_force_request(storage)
        msg = "provider exploded"
        raise RuntimeError(msg)

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=fail_after_forcing_again),
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

    assert prepared.compaction_reply_outcome == "failed"
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    persisted_state = read_scope_state(persisted, _SCOPE)
    assert persisted_state.force_compact_before_next_run is True
    assert persisted_state.force_compact_generation == fresh_generation


@pytest.mark.asyncio
async def test_fresh_force_request_survives_terminal_context_rejection_end_to_end(tmp_path: Path) -> None:
    """Round-7 G1c (C2's gap): the lifecycle failure frame must not wipe what the marker preserved.

    The marker write's compare-by-generation clear keeps the fresh request,
    and the one-frame-up failure clear in the lifecycle wrapper must apply the
    exact same policy instead of wiping the flag unconditionally.
    """
    oversized_summary = ("word " * 15_000) + "TAIL-FACT-MUST-SURVIVE"
    session = _session(
        "session-1",
        runs=[_completed_run("run-1")],
        summary=SessionSummary(summary=oversized_summary, updated_at=datetime.now(UTC)),
    )
    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(tmp_path, session=session)
    fresh_generation: int | None = None

    async def reject_after_forcing_again(**_kwargs: object) -> SessionSummary:
        nonlocal fresh_generation
        if fresh_generation is None:
            fresh_generation = _write_fresh_force_request(storage)
        msg = "prompt is too long for this model"
        raise ContextWindowExceededError(msg)

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=reject_after_forcing_again),
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

    assert prepared.compaction_reply_outcome == "failed"
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    persisted_state = read_scope_state(persisted, _SCOPE)
    marker = persisted_state.carried_summary_unfit
    assert marker is not None
    assert marker.reason == "provider_context_window_rejection"
    assert persisted_state.force_compact_before_next_run is True
    assert persisted_state.force_compact_generation == fresh_generation
