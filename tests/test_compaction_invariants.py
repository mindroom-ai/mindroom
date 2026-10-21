"""Tests for the four history-compaction invariants.

1. Compacted runs stay behind their summary row and never reappear in replay (``mindroom.history.storage``).
2. Chunk progress survives interruption (``mindroom.history.storage``).
3. Summary calls send one request shape and keep only finished summaries (``mindroom.history.summary_call``).
4. Retry on provider failure is deterministic (``mindroom.history.summary_call``).

These tests exercise the owning interfaces directly, not the bot runtime.
"""
# ruff: noqa: D103, TC003

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from agno.agent import Agent

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import (
    DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    MINDROOM_COMPACTION_METADATA_KEY,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history.compaction import SummaryModel, _summarize_chunk, compact_scope_history
from mindroom.history.storage import (
    read_scope_state,
    set_force_compaction_state,
    update_scope_state_on_latest,
)
from mindroom.history.summary_call import (
    DEFAULT_SUMMARY_RETRY_POLICY,
    CompactionSummaryIncompleteError,
    CompactionSummaryOutputLimitError,
    SummaryRetryDecision,
    SummaryRetryPolicy,
    _CompactionSummaryEmptyResultError,
    build_summary_request_messages,
    generate_compaction_summary,
)
from mindroom.history.summary_input import build_summary_input
from mindroom.history.types import (
    COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.llm.errors import ContextWindowExceeded, ModelSafeguardRefusal, ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.providers.errors import classify_provider_error, transport_error
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.token_budget import estimate_compaction_input_tokens
from tests.conftest import bind_runtime_paths, prepare_history_for_run_for_test, seed_session
from tests.fakes.summary_model import summary_model, summary_turn
from tests.history_helpers import (
    FakeModel,
    _reported,
    _summarizer,
    compact_runs,
    folded_run_ids,
    latest_summary_model,
    seed_summary,
)

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelProvider, StopReason

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_HISTORY_SETTINGS = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)


def _provider_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.test/v1/responses")


def _retry_budget_value(
    policy: SummaryRetryPolicy,
    *,
    attempt: int,
    budget: int,
    input_tokens: int,
    minimum_progress_input_tokens: int = 0,
    error: Exception,
) -> int | None:
    decision = policy.retry_budget(
        attempt=attempt,
        budget=budget,
        input_tokens=input_tokens,
        minimum_progress_input_tokens=minimum_progress_input_tokens,
        error=error,
    )
    return None if decision is None else decision.budget


def _chars_per_token_estimator(value: str) -> int:
    return len(value) // 4


def _make_config(
    tmp_path: Path,
    *,
    compaction: CompactionConfig | None = None,
    context_window: int = 64_000,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(enabled=True),
                ),
            },
            defaults=DefaultsConfig(tools=[], compaction=compaction or CompactionConfig()),
            models={
                "default": ModelConfig(provider="openai", id="test-model", context_window=context_window),
            },
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _completed_run(run_id: str, *, marker: str | None = None, padding: int = 0) -> RunRecord:
    content = marker or f"{run_id} content"
    return RunRecord(
        run_id=run_id,
        agent_id="test_agent",
        status=RunStatus.completed,
        messages=[
            Message(role="user", content=f"{content} question {'u' * padding}"),
            Message(role="assistant", content=f"{content} answer {'a' * padding}"),
        ],
    )


def _session(runs: list[RunRecord]) -> ConversationSession:
    return ConversationSession(
        session_id="session-1",
        kind="agent",
        owner_id="test_agent",
        runs=list(runs),
        metadata=None,
        created_at=1,
        updated_at=1,
    )


def _agent(db: object) -> Agent:
    return Agent(
        id="test_agent",
        name="Test Agent",
        model=FakeModel(id="fake-model", provider="fake"),
        db=db,
        add_history_to_context=True,
        store_history_messages=False,
    )


# --- Invariant 1: compacted runs stay behind their summary row and never reappear ---


@pytest.mark.asyncio
async def test_a_stale_save_of_a_folded_run_never_replays(tmp_path: Path) -> None:
    """A later stale write of a compacted run leaves it folded behind its summary row (#1094)."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = seed_session(storage, _session([_completed_run("run-1"), _completed_run("run-2")]))
    compact_runs(storage, session, _SCOPE, ["run-1"], "summary")
    seed_session(storage, _session([_completed_run("run-1"), _completed_run("run-2")]))

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
    )

    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs] == ["run-2"]
    assert folded_run_ids(storage) == ["run-1"]
    assert prepared.replay_plan is not None


def test_a_stale_save_of_a_folded_run_and_its_member_runs_changes_neither(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    runs = [
        _completed_run("compacted"),
        RunRecord(run_id="child", agent_id="test_agent", parent_run_id="compacted", status=RunStatus.completed),
        _completed_run("kept"),
    ]
    session = seed_session(storage, _session(runs))
    compact_runs(storage, session, _SCOPE, ["compacted"], "summary")
    seed_session(storage, _session(runs))

    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs] == ["kept"]


def test_update_scope_state_on_latest_applies_update_to_freshest_row(tmp_path: Path) -> None:
    """The update callable sees the latest persisted state, and the write lands on that row."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stale_session = _session([_completed_run("run-1")])
    seed_session(storage, stale_session)

    concurrent_session = storage.load_session("session-1", "agent")
    assert concurrent_session is not None
    concurrent_state = HistoryScopeState(force_compact_before_next_run=True)
    set_force_compaction_state(concurrent_session, _SCOPE, HistoryScopeState(), force=True)
    concurrent_session.runs = [*(concurrent_session.runs or []), _completed_run("run-2")]
    seed_session(storage, concurrent_session)

    seen_states: list[HistoryScopeState] = []

    def clear_force(latest: HistoryScopeState) -> HistoryScopeState:
        seen_states.append(latest)
        return HistoryScopeState(force_compact_before_next_run=False)

    returned_state = update_scope_state_on_latest(storage, stale_session, _SCOPE, clear_force)

    assert seen_states == [concurrent_state]
    assert returned_state.force_compact_before_next_run is False
    persisted_session = storage.load_session("session-1", "agent")
    assert persisted_session is not None
    assert read_scope_state(persisted_session, _SCOPE) == returned_state
    assert [run.run_id for run in stale_session.runs or []] == ["run-1", "run-2"]


def test_update_scope_state_on_latest_skips_write_when_update_is_a_no_op(tmp_path: Path) -> None:
    """A no-op update must not upsert but still syncs the session from the freshest row."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stale_session = _session([_completed_run("run-1")])
    persisted_state = HistoryScopeState(force_compact_before_next_run=True)
    set_force_compaction_state(stale_session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, stale_session)

    concurrent_session = storage.load_session("session-1", "agent")
    assert concurrent_session is not None
    concurrent_session.runs = [*(concurrent_session.runs or []), _completed_run("run-2")]
    seed_session(storage, concurrent_session)

    store_type = type(storage)
    with patch.object(
        store_type,
        "save_session_fields",
        autospec=True,
        side_effect=store_type.save_session_fields,
    ) as save_spy:
        returned_state = update_scope_state_on_latest(storage, stale_session, _SCOPE, lambda latest: latest)

    save_spy.assert_not_called()
    assert returned_state == persisted_state
    assert [run.run_id for run in stale_session.runs or []] == ["run-1", "run-2"]


def test_scope_state_writes_keep_other_scopes_verbatim() -> None:
    """Writing one scope must not rewrite state stored for another scope, including keys it does not model."""
    other_scope = HistoryScope(kind="team", scope_id="other")
    session = _session([])
    session.metadata = {
        MINDROOM_COMPACTION_METADATA_KEY: {
            "version": 2,
            "states": {other_scope.key: {"force_compact_before_next_run": True, "unmodeled": "kept"}},
        },
    }

    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)

    metadata = session.metadata or {}
    assert metadata[MINDROOM_COMPACTION_METADATA_KEY]["states"] == {
        other_scope.key: {"force_compact_before_next_run": True, "unmodeled": "kept"},
        _SCOPE.key: {"force_compact_before_next_run": True},
    }


# --- Invariant 2: chunk progress survives interruption ------------------------


@pytest.mark.asyncio
async def test_chunk_progress_survives_interruption_and_restart(tmp_path: Path) -> None:
    """An interrupted multi-chunk compaction keeps its partial summary and never re-compacts."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        [
            _completed_run("run-1", marker="RUN1-MARKER", padding=16_000),
            _completed_run("run-2", marker="RUN2-MARKER", padding=16_000),
        ],
    )
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    first_pass_inputs: list[str] = []

    async def interrupted_summary(*, summary_input: str, **_kwargs: object) -> str:
        first_pass_inputs.append(summary_input)
        if len(first_pass_inputs) > 1:
            msg = "provider exploded"
            raise RuntimeError(msg)
        return "summary chunk 1"

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=interrupted_summary),
        ),
        pytest.raises(RuntimeError, match="provider exploded"),
    ):
        await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(),
                10_000,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    interrupted = storage.load_session("session-1", "agent")
    assert interrupted is not None
    assert interrupted.summary is not None
    assert interrupted.summary.text == "summary chunk 1"
    assert [run.run_id for run in interrupted.runs or []] == ["run-2"]
    assert folded_run_ids(storage) == ["run-1"]
    assert read_scope_state(interrupted, _SCOPE).force_compact_before_next_run is True
    assert "RUN1-MARKER" in first_pass_inputs[0]

    # A stale writer saves the already-compacted run again before the restart.
    stale_session = deepcopy(interrupted)
    stale_session.runs = [_completed_run("run-1", marker="RUN1-MARKER", padding=16_000), *(interrupted.runs or [])]
    seed_session(storage, stale_session)

    restart_inputs: list[str] = []

    async def restart_summary(*, summary_input: str, **_kwargs: object) -> str:
        restart_inputs.append(summary_input)
        return "summary chunk 2"

    with (
        patch(
            "mindroom.history.compaction.build_model_provider",
            return_value=summary_model(),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=restart_summary),
        ),
    ):
        await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
        )

    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "summary chunk 2"
    assert persisted.runs == []
    assert folded_run_ids(storage) == ["run-1", "run-2"]
    assert read_scope_state(persisted, _SCOPE).force_compact_before_next_run is False
    # The restart consumed the partial summary and never re-summarized run-1.
    assert len(restart_inputs) == 1
    assert "summary chunk 1" in restart_inputs[0]
    assert "RUN2-MARKER" in restart_inputs[0]
    assert "RUN1-MARKER" not in restart_inputs[0]


# --- Invariant 3: one summary request shape ----------------------------------


@pytest.mark.asyncio
async def test_generate_compaction_summary_sends_the_prompt_and_input_as_one_request() -> None:
    model = summary_model("recorded summary")

    summary = await generate_compaction_summary(
        model=model,
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
        timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    )

    assert summary == "recorded summary"
    [request] = model.requests
    assert [(message.role, message.content) for message in request.messages] == [
        ("system", "Summarize the conversation."),
        ("user", "conversation payload"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "error"),
    [
        ("output_limit", CompactionSummaryOutputLimitError),
        ("incomplete", CompactionSummaryIncompleteError),
        ("content_filter", CompactionSummaryIncompleteError),
        ("tool_use", CompactionSummaryIncompleteError),
    ],
)
async def test_generate_compaction_summary_refuses_a_summary_that_did_not_finish(
    stop_reason: StopReason,
    error: type[Exception],
) -> None:
    """Text cut off by the output limit or left unfinished is never persisted as the summary."""
    with pytest.raises(error):
        await generate_compaction_summary(
            model=summary_model(summary_turn("durable summary ended cleanly.", stop_reason=stop_reason)),
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
            timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end", None])
async def test_generate_compaction_summary_accepts_a_finished_summary(stop_reason: StopReason | None) -> None:
    """A summary that ended on its own is kept; a route that reports no stop reason counts as finished."""
    summary = await generate_compaction_summary(
        model=summary_model(summary_turn("durable summary ended cleanly.", stop_reason=stop_reason)),
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
        timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    )

    assert summary == "durable summary ended cleanly."


def test_build_summary_request_messages_is_the_single_request_seam() -> None:
    messages = build_summary_request_messages(summary_prompt="prompt", summary_input="input")

    assert [(message.role, message.content) for message in messages] == [
        ("system", "prompt"),
        ("user", "input"),
    ]


# --- Invariant 4: deterministic retry on provider failure ----------------------


def test_retry_policy_shrinks_on_timeout_and_output_limit() -> None:
    policy = DEFAULT_SUMMARY_RETRY_POLICY

    assert (
        _retry_budget_value(
            policy,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=TimeoutError(),
        )
        == 8_000
    )
    assert (
        _retry_budget_value(
            policy,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=CompactionSummaryOutputLimitError("renamed owned output-limit signal"),
        )
        == 8_000
    )


def test_retry_policy_halves_budget_for_typed_context_window_error() -> None:
    error = ContextWindowExceeded("provider-specific wording does not matter", provider="fake")

    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=error,
        )
        == 8_000
    )


def test_retry_policy_propagates_second_typed_context_window_error() -> None:
    error = ContextWindowExceeded("provider-specific wording does not matter", provider="fake")

    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=2,
            budget=8_000,
            input_tokens=8_000,
            error=error,
        )
        is None
    )


def test_retry_policy_does_not_shrink_safeguard_refusal() -> None:
    """A safeguard refusal is a content decision, not a size problem: no shrink retry."""
    error = ModelSafeguardRefusal(provider="fake")

    assert DEFAULT_SUMMARY_RETRY_POLICY.should_shrink(error) is False
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=error,
        )
        is None
    )


def test_retry_policy_should_shrink_for_empty_result() -> None:
    policy = DEFAULT_SUMMARY_RETRY_POLICY

    assert policy.should_shrink(_CompactionSummaryEmptyResultError("summary generation returned no result")) is True


def test_retry_policy_shrinks_from_actual_serialized_input_size() -> None:
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=6_000,
            error=TimeoutError(),
        )
        == 3_000
    )


def test_retry_policy_clamps_shrink_target_to_minimum_progress_input() -> None:
    """A durable summary above half the failed input raises the shrink target instead of cancelling it."""
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=10_000,
        input_tokens=8_573,
        minimum_progress_input_tokens=6_230,
        error=CompactionSummaryOutputLimitError("renamed owned output-limit signal"),
    )

    assert decision == SummaryRetryDecision(budget=6_230, kind="shrink")


def test_retry_policy_declines_shrink_when_no_smaller_progress_input_exists() -> None:
    """An input already at the progress minimum gets no shrink retry."""
    assert (
        DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
            attempt=1,
            budget=10_000,
            input_tokens=6_230,
            minimum_progress_input_tokens=6_230,
            error=TimeoutError(),
        )
        is None
    )


def test_retry_policy_shrink_classification_precedes_transient_status() -> None:
    error = classify_provider_error("Request too large", provider="fake", status_code=429)
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=6_000,
        minimum_progress_input_tokens=0,
        error=error,
    )

    assert decision is not None
    assert decision.budget == 3_000
    assert decision.kind == "shrink"


def test_retry_policy_falls_through_to_transient_retry_when_input_cannot_shrink() -> None:
    error = classify_provider_error("upstream connection timed out", provider="fake", status_code=503)
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=1_000,
        minimum_progress_input_tokens=0,
        error=error,
    )

    assert decision is not None
    assert decision.budget == 16_000
    assert decision.kind == "same-budget-transient"
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=2,
            budget=16_000,
            input_tokens=1_000,
            error=error,
        )
        is None
    )


def test_retry_policy_does_not_resend_non_transient_shrink_errors_at_floor() -> None:
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=1_000,
            error=CompactionSummaryOutputLimitError("renamed owned output-limit signal"),
        )
        is None
    )
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=1_000,
            error=TimeoutError(),
        )
        is None
    )


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 502, 503, 504, 529])
def test_retry_policy_retries_transient_provider_errors_at_same_budget(status_code: int) -> None:
    error = classify_provider_error("temporary provider failure", provider="fake", status_code=status_code)
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=4_000,
        minimum_progress_input_tokens=0,
        error=error,
    )

    assert decision is not None
    assert decision.budget == 16_000
    assert decision.kind == "same-budget-transient"
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=2,
            budget=16_000,
            input_tokens=4_000,
            error=error,
        )
        is None
    )


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_retry_policy_does_not_retry_non_transient_provider_statuses(status_code: int) -> None:
    error = classify_provider_error("provider failure", provider="fake", status_code=status_code)

    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=4_000,
            error=error,
        )
        is None
    )


@pytest.mark.parametrize(
    "cause",
    [
        pytest.param(httpx.ConnectError("connection reset", request=_provider_request()), id="connection-reset"),
        pytest.param(httpx.ReadTimeout("The read operation timed out", request=_provider_request()), id="read-timeout"),
    ],
)
def test_retry_policy_retries_a_transport_failure_at_the_same_budget(cause: httpx.TransportError) -> None:
    """A network failure before the response completed may succeed unchanged, so the input is not shrunk."""
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=4_000,
        minimum_progress_input_tokens=0,
        error=transport_error(cause, provider="fake"),
    )

    assert decision == SummaryRetryDecision(budget=16_000, kind="same-budget-transient")


def test_retry_policy_does_not_retry_a_final_provider_error() -> None:
    error = ProviderError("unclassified provider failure", provider="fake")

    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=4_000,
            error=error,
        )
        is None
    )


def test_retry_policy_shrinks_on_typed_size_errors_not_on_wording() -> None:
    """Only typed size signals and a final timeout shrink the input; wording outside a provider error never does."""
    policy = DEFAULT_SUMMARY_RETRY_POLICY

    def retry_budget(error: Exception) -> int | None:
        return _retry_budget_value(policy, attempt=1, budget=16_000, input_tokens=16_000, error=error)

    assert retry_budget(classify_provider_error("context_length_exceeded", provider="fake", status_code=400)) == 8_000
    tokens_per_minute = "Request too large for summary-model on tokens per min (TPM): Limit 30000, Requested 50000."
    assert retry_budget(classify_provider_error(tokens_per_minute, provider="fake", status_code=429)) == 8_000
    assert retry_budget(ProviderError("upstream request timed out", provider="fake")) == 8_000
    untyped_timeout = RuntimeError(f"compaction summary timed out after {DEFAULT_COMPACTION_TIMEOUT_SECONDS}s")
    assert retry_budget(untyped_timeout) is None
    # A provider failure that mentions the context window is still the provider's to retry, not a size problem.
    assert retry_budget(classify_provider_error("context window", provider="fake", status_code=503)) == 16_000
    assert retry_budget(RuntimeError("context_length_exceeded")) is None
    assert retry_budget(RuntimeError("request too large")) is None
    assert retry_budget(classify_provider_error("input too large to cache", provider="fake", status_code=500)) == 16_000


def test_retry_policy_gives_up_on_non_retryable_errors() -> None:
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=4_000,
            error=RuntimeError("boom"),
        )
        is None
    )
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=4_000,
            error=ValueError("401"),
        )
        is None
    )


def test_retry_policy_gives_up_after_max_attempts() -> None:
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=2,
            budget=16_000,
            input_tokens=16_000,
            error=TimeoutError(),
        )
        is None
    )


def test_retry_policy_clamps_to_floor_and_stops_there() -> None:
    policy = SummaryRetryPolicy(max_attempts=10)

    assert _retry_budget_value(policy, attempt=1, budget=1_500, input_tokens=1_500, error=TimeoutError()) == 1_000
    assert _retry_budget_value(policy, attempt=1, budget=1_000, input_tokens=1_000, error=TimeoutError()) is None
    assert _retry_budget_value(policy, attempt=1, budget=999, input_tokens=999, error=TimeoutError()) is None


def test_retry_schedule_halves_deterministically() -> None:
    policy = SummaryRetryPolicy(max_attempts=10)
    budgets = []
    budget = 16_000
    attempt = 1
    while (
        next_budget := _retry_budget_value(
            policy,
            attempt=attempt,
            budget=budget,
            input_tokens=budget,
            error=TimeoutError(),
        )
    ) is not None:
        budgets.append(next_budget)
        budget = next_budget
        attempt += 1

    assert budgets == [8_000, 4_000, 2_000, 1_000]


@pytest.mark.asyncio
async def test_summary_chunk_propagates_original_error_when_rebuilt_input_is_not_smaller() -> None:
    """A defensive estimate guard prevents a shrink retry from resending equal-size input."""
    run = _completed_run("run-1")
    initial_input = "original request " * 1_000
    original_error = CompactionSummaryOutputLimitError("renamed owned output-limit signal")
    generate_summary = AsyncMock(side_effect=original_error)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=generate_summary,
        ),
        patch(
            "mindroom.history.compaction.build_summary_input",
            return_value=(initial_input, [run]),
        ) as rebuild_input,
        pytest.raises(CompactionSummaryOutputLimitError) as raised,
    ):
        await _summarize_chunk(
            _summarizer(
                summary_model(),
                4_000,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            None,
            [run],
        )

    assert raised.value is original_error
    # The first call builds the input; the shrink rebuild comes back no smaller.
    assert rebuild_input.call_count == 2
    generate_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_chunk_honors_transient_fallthrough_for_shrink_message_at_floor() -> None:
    """A transient policy decision remains same-budget even when its message is shrinkable."""
    run = _completed_run("run-1")
    recovered_summary = "recovered summary"
    generate_summary = AsyncMock(
        side_effect=[
            classify_provider_error("upstream connection timed out", provider="fake", status_code=503),
            recovered_summary,
        ],
    )
    retry_sleep = AsyncMock()

    with (
        patch("mindroom.history.compaction.build_summary_input", return_value=("original request", [run])),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=generate_summary,
        ),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
    ):
        generated = await _summarize_chunk(
            _summarizer(
                summary_model(),
                4_000,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            None,
            [run],
        )

    assert generated.summary is recovered_summary
    assert generated.included_runs == [run]
    assert generate_summary.await_count == 2
    assert [call.kwargs["summary_input"] for call in generate_summary.await_args_list] == [
        "original request",
        "original request",
    ]
    retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)


@pytest.mark.asyncio
async def test_summary_chunk_shrinks_around_a_large_durable_summary() -> None:
    """A durable summary above half the failed input still gets a strictly smaller second attempt.

    Regression: the halved shrink target used to fall below the previous-summary
    block, the rebuild came back run-less, and the original error propagated
    without any smaller attempt — so the next turn reselected required
    compaction and resent the identical failing request.
    """
    previous_summary = "s" * 24_000
    runs = [_completed_run(f"run-{index}", padding=2_000) for index in range(3)]
    summary_input_budget = 10_000
    initial_input, initial_runs = build_summary_input(
        previous_summary=previous_summary,
        compacted_runs=runs,
        history_settings=_HISTORY_SETTINGS,
        max_input_tokens=summary_input_budget,
        token_estimator=_chars_per_token_estimator,
    )
    initial_tokens = _chars_per_token_estimator(initial_input)
    assert len(initial_runs) == 3
    assert initial_tokens > 8_000
    assert _chars_per_token_estimator(previous_summary) > initial_tokens // 2
    recovered_summary = "recovered summary"
    generate_summary = AsyncMock(
        side_effect=[CompactionSummaryOutputLimitError("renamed owned output-limit signal"), recovered_summary],
    )

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        patch("mindroom.history.compaction._compaction_sizing", return_value=(_chars_per_token_estimator, "chars")),
    ):
        generated = await _summarize_chunk(
            _summarizer(
                summary_model(),
                summary_input_budget,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            previous_summary,
            runs,
        )

    assert generated.summary is recovered_summary
    assert generate_summary.await_count == 2
    retry_input = generate_summary.await_args_list[1].kwargs["summary_input"]
    assert _chars_per_token_estimator(retry_input) < initial_tokens
    assert previous_summary in retry_input
    assert "<run " in retry_input.split("</previous_summary>")[1]
    assert [run.run_id for run in generated.included_runs] == ["run-0"]


@pytest.mark.asyncio
async def test_summary_chunk_propagates_error_when_no_smaller_progress_input_exists() -> None:
    """An input at the progress minimum fails after one call without a run-less request."""
    previous_summary = "s" * 24_000
    runs = [_completed_run("run-1", padding=2_000)]
    summary_input_budget = 6_100
    _initial_input, initial_runs = build_summary_input(
        previous_summary=previous_summary,
        compacted_runs=runs,
        history_settings=_HISTORY_SETTINGS,
        max_input_tokens=summary_input_budget,
        token_estimator=_chars_per_token_estimator,
    )
    assert [run.run_id for run in initial_runs] == ["run-1"]
    original_error = CompactionSummaryOutputLimitError("renamed owned output-limit signal")
    generate_summary = AsyncMock(side_effect=original_error)

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        patch("mindroom.history.compaction._compaction_sizing", return_value=(_chars_per_token_estimator, "chars")),
        pytest.raises(CompactionSummaryOutputLimitError) as raised,
    ):
        await _summarize_chunk(
            _summarizer(
                summary_model(),
                summary_input_budget,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            previous_summary,
            runs,
        )

    assert raised.value is original_error
    generate_summary.assert_awaited_once()
    assert "<run " in generate_summary.await_args.kwargs["summary_input"]


@pytest.mark.asyncio
async def test_generate_compaction_summary_empty_result_raises_typed_error_with_diagnostics() -> None:
    """An empty summary response raises the typed error carrying response diagnostics."""
    with pytest.raises(
        _CompactionSummaryEmptyResultError,
        match=r"returned no result \(output_tokens=0, has_reasoning=False\)",
    ):
        await generate_compaction_summary(
            model=summary_model(""),
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
            timeout_seconds=DEFAULT_COMPACTION_TIMEOUT_SECONDS,
        )


def test_retry_policy_shrinks_budget_for_empty_result() -> None:
    """Empty results may be a provider's response to an oversized request."""
    error = _CompactionSummaryEmptyResultError("summary generation returned no result")
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=error,
        )
        == 8_000
    )
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=2,
            budget=16_000,
            input_tokens=16_000,
            error=error,
        )
        is None
    )


def test_compaction_input_estimate_uses_tiktoken() -> None:
    assert estimate_compaction_input_tokens("structured: true") == 3
    assert estimate_compaction_input_tokens("☃☃") == 4


def test_compaction_input_estimate_uses_conservative_claude_fallback() -> None:
    assert estimate_compaction_input_tokens(
        "structured: true",
        model_id="claude-sonnet-5",
        conservative_fallback=True,
    ) == len(b"structured: true")
    assert estimate_compaction_input_tokens("☃☃", model_id="claude-sonnet-5", conservative_fallback=True) == 6


def test_compaction_input_estimate_keeps_known_model_encoding() -> None:
    assert estimate_compaction_input_tokens("structured: true", model_id="gpt-4o", conservative_fallback=True) == 3


@pytest.mark.asyncio
async def test_claude_compaction_splits_dense_preserved_metadata_before_the_input_limit(tmp_path: Path) -> None:
    """Regression for the production request that tiktoken underestimated by 1.63x."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    runs = []
    for run_index in range(20):
        run = _completed_run(f"run-{run_index}")
        run.metadata = {
            "durable_outcomes": [
                {
                    "name": f"outcome_{run_index}_{outcome_index}",
                    "description": "".join(
                        f"{run_index * 100_000 + outcome_index * 1_000 + value:08x}" for value in range(50)
                    ),
                }
                for outcome_index in range(20)
            ],
        }
        runs.append(run)
    session = _session(runs)
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    summary_inputs: list[str] = []

    async def record_summary(*, summary_input: str, **_kwargs: object) -> str:
        summary_inputs.append(summary_input)
        return f"summary chunk {len(summary_inputs)}"

    summary_input_limit = 167_232
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=record_summary),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(model_id="claude-sonnet-5", claude=True),
                summary_input_limit,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=200_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert outcome is not None
    assert outcome.compacted_run_count == 20
    assert len(summary_inputs) == 2
    assert all(len(summary_input.encode("utf-8")) <= summary_input_limit for summary_input in summary_inputs)
    assert (
        estimate_compaction_input_tokens(
            summary_inputs[0],
            model_id="claude-sonnet-5",
        )
        < summary_input_limit
    )


@pytest.mark.asyncio
async def test_compaction_retries_empty_summary_result_with_smaller_input(tmp_path: Path) -> None:
    """An empty summary response retries with a smaller input."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        [
            _completed_run("run-1", marker="RUN1-MARKER", padding=16_000),
            _completed_run("run-2", marker="RUN2-MARKER", padding=16_000),
        ],
    )
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)

    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> str:
        attempts.append(summary_input)
        if len(attempts) == 1:
            msg = "summary generation returned no result (output_tokens=0, has_reasoning=False)"
            raise _CompactionSummaryEmptyResultError(msg)
        return "recovered summary"

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=flaky_summary),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(),
                10_000,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert outcome is not None
    # Chunk 1 fails empty, is rebuilt smaller, then chunk 2 compacts run-2.
    assert len(attempts) == 3
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    assert attempts[2] != attempts[1]
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "recovered summary"


@pytest.mark.asyncio
async def test_summary_chunk_switches_to_fallback_once_with_unchanged_prompt_and_input() -> None:
    """A primary refusal resends the unchanged prompt and input bytes once to the fallback model."""
    run = _completed_run("run-1")
    primary = summary_model()
    fallback = summary_model(model_id="fallback-model-id")
    recovered_summary = "recovered summary"
    generate_summary = AsyncMock(
        side_effect=[ModelSafeguardRefusal(provider="fake"), recovered_summary],
    )
    retry_sleep = AsyncMock()
    logger_mock = MagicMock()

    with (
        patch("mindroom.history.compaction.build_summary_input", return_value=("original request", [run])),
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        patch("mindroom.history.compaction.logger", logger_mock),
    ):
        generated = await _summarize_chunk(
            _summarizer(
                primary,
                4_000,
                fallback=SummaryModel(fallback, "fallback-model", 4_000),
                history_settings=_HISTORY_SETTINGS,
                timeout_seconds=420.0,
                scope=_SCOPE,
            ),
            None,
            [run],
        )

    assert generated.summary is recovered_summary
    assert generated.included_runs == [run]
    assert generated.summarizer.model.model is fallback
    assert generated.summarizer.model.name == "fallback-model"
    assert generated.summarizer.fallback is None
    assert generate_summary.await_count == 2
    assert [call.kwargs["model"] for call in generate_summary.await_args_list] == [primary, fallback]
    assert [call.kwargs["summary_input"] for call in generate_summary.await_args_list] == [
        "original request",
        "original request",
    ]
    assert [call.kwargs["summary_prompt"] for call in generate_summary.await_args_list] == [
        COMPACTION_SUMMARY_PROMPT,
        COMPACTION_SUMMARY_PROMPT,
    ]
    assert [call.kwargs["timeout_seconds"] for call in generate_summary.await_args_list] == [420.0, 420.0]
    retry_sleep.assert_not_awaited()
    # Structured request/failure/completion logs identify the actual serving model.
    assert [
        call.kwargs["model_name"]
        for call in logger_mock.info.call_args_list
        if call.args[0] == "Compaction summary chunk request"
    ] == ["summary-model", "fallback-model"]
    assert [
        call.kwargs["timeout_seconds"]
        for call in logger_mock.info.call_args_list
        if call.args[0] == "Compaction summary chunk request"
    ] == [420.0, 420.0]
    assert [
        call.kwargs["model_name"]
        for call in logger_mock.warning.call_args_list
        if call.args[0] == "Compaction summary chunk failed"
    ] == ["summary-model"]
    assert [
        call.kwargs["model_name"]
        for call in logger_mock.info.call_args_list
        if call.args[0] == "Compaction summary chunk completed"
    ] == ["fallback-model"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback_error",
    [
        pytest.param(ModelSafeguardRefusal(provider="fake"), id="fallback-refusal"),
        pytest.param(
            classify_provider_error("invalid request", provider="fake", status_code=400),
            id="fallback-failure",
        ),
    ],
)
async def test_summary_chunk_propagates_fallback_refusal_or_failure(fallback_error: Exception) -> None:
    """The fallback call is the one bounded second attempt; its errors propagate."""
    run = _completed_run("run-1")
    primary = summary_model()
    fallback = summary_model(model_id="fallback-model-id")
    generate_summary = AsyncMock(
        side_effect=[ModelSafeguardRefusal(provider="fake"), fallback_error],
    )

    with (
        patch("mindroom.history.compaction.build_summary_input", return_value=("original request", [run])),
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        pytest.raises(type(fallback_error)) as raised,
    ):
        await _summarize_chunk(
            _summarizer(
                primary,
                4_000,
                fallback=SummaryModel(fallback, "fallback-model", 4_000),
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            None,
            [run],
        )

    assert raised.value is fallback_error
    assert generate_summary.await_count == 2
    assert [call.kwargs["summary_input"] for call in generate_summary.await_args_list] == [
        "original request",
        "original request",
    ]


@pytest.mark.asyncio
async def test_summary_chunk_refusal_after_transient_retry_propagates_within_attempt_bound() -> None:
    """A refusal on the bounded second attempt propagates instead of issuing a third fallback call."""
    run = _completed_run("run-1")
    primary = summary_model()
    fallback = summary_model(model_id="fallback-model-id")
    refusal = ModelSafeguardRefusal(provider="fake")
    generate_summary = AsyncMock(
        side_effect=[classify_provider_error("temporary provider failure", provider="fake", status_code=503), refusal],
    )
    retry_sleep = AsyncMock()

    with (
        patch("mindroom.history.compaction.build_summary_input", return_value=("original request", [run])),
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
        pytest.raises(ModelSafeguardRefusal) as raised,
    ):
        await _summarize_chunk(
            _summarizer(
                primary,
                4_000,
                fallback=SummaryModel(fallback, "fallback-model", 4_000),
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            None,
            [run],
        )

    assert raised.value is refusal
    assert generate_summary.await_count == 2
    assert [call.kwargs["model"] for call in generate_summary.await_args_list] == [primary, primary]
    retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)


@pytest.mark.asyncio
async def test_compaction_fallback_serves_later_chunks_state_and_outcome(tmp_path: Path) -> None:
    """After a fallback switch, the fallback serves later chunks and is the reported summary model."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        [
            _completed_run("run-1", marker="RUN1-MARKER", padding=16_000),
            _completed_run("run-2", marker="RUN2-MARKER", padding=16_000),
        ],
    )
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    primary = summary_model()
    fallback = summary_model(model_id="fallback-model-id")
    attempts: list[tuple[str, str]] = []

    async def flaky_summary(*, model: ModelProvider, summary_input: str, **_kwargs: object) -> str:
        attempts.append((model.identity.id, summary_input))
        if len(attempts) == 1:
            raise ModelSafeguardRefusal(provider="fake")
        return "recovered summary"

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=flaky_summary),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                primary,
                100_000,
                fallback=SummaryModel(fallback, "fallback-model", 10_000),
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert outcome is not None
    # The primary sees both runs. Its smaller-context fallback rebuilds the
    # refused chunk with run 1, then keeps its own budget for run 2.
    assert [model_id for model_id, _ in attempts] == ["summary-model", "fallback-model-id", "fallback-model-id"]
    assert "RUN1-MARKER" in attempts[0][1]
    assert "RUN2-MARKER" in attempts[0][1]
    assert "RUN1-MARKER" in attempts[1][1]
    assert "RUN2-MARKER" not in attempts[1][1]
    assert "RUN2-MARKER" in attempts[2][1]
    assert outcome.summary_model == "fallback-model"
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "recovered summary"
    assert latest_summary_model(storage, _SCOPE) == "fallback-model-id"


@pytest.mark.asyncio
async def test_small_refused_summary_request_fails_without_identical_retry_or_persistent_state(
    tmp_path: Path,
) -> None:
    """A request below the retry floor fails only its current attempt without being resent."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER")])
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    attempts: list[str] = []

    async def refuse_summary(*, summary_input: str, **_kwargs: object) -> str:
        attempts.append(summary_input)
        raise ModelSafeguardRefusal(provider="fake")

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=refuse_summary),
        ),
        pytest.raises(ModelSafeguardRefusal),
    ):
        await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(),
                10_000,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert len(attempts) == 1
    assert estimate_compaction_input_tokens(attempts[0]) < 1_000
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE) == HistoryScopeState(force_compact_before_next_run=True)


@pytest.mark.asyncio
async def test_minimum_available_budget_can_issue_smaller_degradation_retry(tmp_path: Path) -> None:
    """The smallest available plan can rebuild and issue its promised smaller retry."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=4_000)])
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    summary_input_budget = 2 * COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS + 1
    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> str:
        attempts.append(summary_input)
        if len(attempts) == 1:
            message = "summary generation returned no result"
            raise _CompactionSummaryEmptyResultError(message)
        return "recovered summary"

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ),
        patch(
            "mindroom.history.compaction.build_summary_input",
            wraps=build_summary_input,
        ) as build_summary_input_spy,
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(),
                summary_input_budget,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert outcome is not None
    assert len(attempts) == 2
    build_budgets = [call.kwargs["max_input_tokens"] for call in build_summary_input_spy.call_args_list]
    assert build_budgets[0] == summary_input_budget
    assert build_budgets[1] < build_budgets[0]
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "recovered summary"
    assert persisted.runs == []


@pytest.mark.asyncio
async def test_near_cap_durable_summary_with_tiny_budget_is_unavailable_without_fact_loss(tmp_path: Path) -> None:
    """A degenerate plan is unavailable and cannot repeatedly select a compaction rewrite."""
    summary_input_budget = COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS + 1
    config, runtime_paths = _make_config(
        tmp_path,
        context_window=10_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    previous_summary = ("word " * 975) + "TAIL-FACT-MUST-SURVIVE"
    session = _session([_completed_run("run-1", marker="RUN1-MARKER")])
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    seed_summary(storage, session, _SCOPE, previous_summary)
    session = storage.load_session("session-1", "agent")
    assert session is not None
    generate_summary = AsyncMock()

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=generate_summary,
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(),
                summary_input_budget,
                name="default",
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=summary_input_budget,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert outcome is None
    generate_summary.assert_not_awaited()
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == previous_summary
    assert "TAIL-FACT-MUST-SURVIVE" in persisted.summary.text
    assert [run.run_id for run in persisted.runs] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE) == HistoryScopeState()

    runtime_generate_summary = AsyncMock()
    with (
        patch(
            "mindroom.history.compaction.build_model_provider",
            return_value=summary_model(),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=runtime_generate_summary,
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=persisted,
            static_prompt_tokens=0,
        )

    runtime_generate_summary.assert_not_awaited()
    assert prepared.compaction_decision.reason == "compaction_unavailable"
    assert prepared.compaction_reply_outcome == "none"
    repeated = storage.load_session("session-1", "agent")
    assert repeated is not None
    assert repeated.summary is not None
    assert repeated.summary.text == previous_summary
    assert [run.run_id for run in repeated.runs or []] == ["run-1"]
    assert read_scope_state(repeated, _SCOPE) == HistoryScopeState()


@pytest.mark.asyncio
async def test_two_timeouts_exhaust_current_attempt_without_persisting_suppression(tmp_path: Path) -> None:
    """Retry exhaustion after a smaller timeout request leaves no durable policy state."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=16_000)])
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)
    attempts: list[str] = []

    async def time_out_summary(*, summary_input: str, **_kwargs: object) -> str:
        attempts.append(summary_input)
        raise TimeoutError

    with (
        patch(
            "mindroom.history.compaction.build_model_provider",
            return_value=summary_model(),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=time_out_summary),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(attempts) == 2
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    assert prepared.compaction_reply_outcome == "timeout"
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE) == HistoryScopeState()


@pytest.mark.parametrize(
    "first_error",
    [
        pytest.param(
            classify_provider_error("temporary provider failure", provider="fake", status_code=503),
            id="status-503",
        ),
        pytest.param(
            transport_error(httpx.ConnectError("connection reset", request=_provider_request()), provider="fake"),
            id="transport-failure",
        ),
    ],
)
@pytest.mark.asyncio
async def test_compaction_retries_transient_provider_error_at_same_budget(
    tmp_path: Path,
    first_error: ProviderError,
) -> None:
    """A selected typed transient failure retries the same chunk and persists the result."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=4_000)])
    set_force_compaction_state(session, _SCOPE, HistoryScopeState(), force=True)
    seed_session(storage, session)

    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> str:
        attempts.append(summary_input)
        if len(attempts) == 1:
            raise first_error
        return "recovered summary"

    retry_sleep = AsyncMock()
    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ),
        patch("mindroom.history.compaction.asyncio.sleep", new=retry_sleep),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            summarizer=_summarizer(
                summary_model(),
                10_000,
                history_settings=_HISTORY_SETTINGS,
                scope=_SCOPE,
            ),
            available_history_budget=None,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    assert outcome is not None
    assert outcome.compacted_run_count == 1
    assert len(attempts) == 2
    assert attempts[1] == attempts[0]
    retry_sleep.assert_awaited_once_with(DEFAULT_SUMMARY_RETRY_POLICY.same_input_retry_delay_seconds)
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "recovered summary"
