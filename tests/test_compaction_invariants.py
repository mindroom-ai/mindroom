"""Tests for the four history-compaction invariants.

1. Compacted runs never reappear (``mindroom.history.storage``).
2. Chunk progress survives interruption (``mindroom.history.storage``).
3. Summary calls get exactly one model configuration path (``mindroom.history.summary_call``).
4. Budget shrinks deterministically on provider failure (``mindroom.history.summary_call``).

These tests exercise the owning interfaces directly, not the bot runtime.
"""
# ruff: noqa: D103, TC003

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import (
    MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
    MINDROOM_COMPACTION_METADATA_KEY,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history.compaction import compact_scope_history
from mindroom.history.storage import (
    compacted_run_ids_with,
    prune_reintroduced_runs,
    read_scope_state,
    remove_runs_by_id,
    write_scope_state,
)
from mindroom.history.summary_call import (
    DEFAULT_SUMMARY_RETRY_POLICY,
    SUMMARY_MAX_OUTPUT_TOKENS,
    SummaryRetryPolicy,
    build_summary_request_messages,
    configure_summary_model,
    generate_compaction_summary,
)
from mindroom.history.types import HistoryPolicy, HistoryScope, HistoryScopeState, ResolvedHistorySettings
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.conftest import FakeModel, bind_runtime_paths, prepare_history_for_run_for_test

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_HISTORY_SETTINGS = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)


class _RecordingClaude(Claude):
    """Claude double that records the request instead of calling Anthropic."""

    def __init__(self, *, response: ModelResponse | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.response = response or ModelResponse(content="recorded summary")
        self.seen_messages: list[Message] = []

    async def aresponse(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return self.response


def _make_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
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
            defaults=DefaultsConfig(tools=[], compaction=CompactionConfig()),
            models={
                "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            },
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _completed_run(run_id: str, *, marker: str | None = None, padding: int = 0) -> RunOutput:
    content = marker or f"{run_id} content"
    return RunOutput(
        run_id=run_id,
        agent_id="test_agent",
        status=RunStatus.completed,
        messages=[
            Message(role="user", content=f"{content} question {'u' * padding}"),
            Message(role="assistant", content=f"{content} answer {'a' * padding}"),
        ],
    )


def _session(runs: list[RunOutput]) -> AgentSession:
    return AgentSession(
        session_id="session-1",
        agent_id="test_agent",
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


# --- Invariant 1: compacted runs never reappear ------------------------------


@pytest.mark.asyncio
async def test_prepare_history_for_run_prunes_reintroduced_compacted_runs(tmp_path: Path) -> None:
    """Compacted-run tombstones win over a later stale session write (#1094)."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1"), _completed_run("run-2")])
    write_scope_state(
        session,
        _SCOPE,
        HistoryScopeState(
            last_compacted_at="2026-01-01T00:00:00Z",
            last_summary_model="summary-model",
            last_compacted_run_count=1,
            compacted_run_ids=("run-1",),
        ),
    )
    storage.upsert_session(session)

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
    assert [run.run_id for run in persisted.runs or []] == ["run-2"]
    assert prepared.replay_plan is not None
    storage.close()


def test_prune_reintroduced_runs_removes_tombstoned_runs_and_descendants() -> None:
    session = _session(
        [
            _completed_run("kept"),
            _completed_run("compacted"),
            RunOutput(run_id="child", parent_run_id="compacted", status=RunStatus.completed),
        ],
    )
    state = HistoryScopeState(compacted_run_ids=("compacted",))

    assert prune_reintroduced_runs(session, state) is True
    assert [run.run_id for run in session.runs or []] == ["kept"]


def test_prune_reintroduced_runs_is_a_no_op_without_resurrected_runs() -> None:
    session = _session([_completed_run("kept")])

    assert prune_reintroduced_runs(session, HistoryScopeState(compacted_run_ids=("gone",))) is False
    assert prune_reintroduced_runs(session, HistoryScopeState()) is False
    assert [run.run_id for run in session.runs or []] == ["kept"]


def test_remove_runs_by_id_removes_descendants() -> None:
    runs = [
        RunOutput(run_id="unrelated", status=RunStatus.completed),
        RunOutput(run_id="child", parent_run_id="root", status=RunStatus.completed),
        RunOutput(run_id="grandchild", parent_run_id="child", status=RunStatus.completed),
        RunOutput(run_id="root", status=RunStatus.completed),
    ]

    pruned_runs = remove_runs_by_id(runs, ["root"])

    assert [run.run_id for run in pruned_runs] == ["unrelated"]


def test_compacted_run_ids_with_caps_tombstones_to_newest_ids() -> None:
    existing_ids = tuple(f"old-{index}" for index in range(1_024))

    compacted_run_ids = compacted_run_ids_with(
        HistoryScopeState(compacted_run_ids=existing_ids),
        ["new-1", "new-2"],
    )

    assert len(compacted_run_ids) == 1_024
    assert compacted_run_ids[:2] == ("old-2", "old-3")
    assert compacted_run_ids[-2:] == ("new-1", "new-2")
    assert "old-0" not in compacted_run_ids


def test_write_scope_state_round_trips_capped_tombstones() -> None:
    session = _session([])

    write_scope_state(
        session,
        _SCOPE,
        HistoryScopeState(compacted_run_ids=tuple(f"run-{index}" for index in range(1_026))),
    )

    metadata = session.metadata or {}
    raw_compaction = metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert isinstance(raw_compaction, dict)
    raw_state = raw_compaction["states"][_SCOPE.key]
    assert isinstance(raw_state, dict)
    serialized_run_ids = raw_state["compacted_run_ids"]
    assert isinstance(serialized_run_ids, list)
    assert len(serialized_run_ids) == 1_024
    assert serialized_run_ids[-2:] == ["run-1024", "run-1025"]
    assert read_scope_state(session, _SCOPE).compacted_run_ids == tuple(serialized_run_ids)


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
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    first_pass_inputs: list[str] = []

    async def interrupted_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        first_pass_inputs.append(summary_input)
        if len(first_pass_inputs) > 1:
            msg = "provider exploded"
            raise RuntimeError(msg)
        return SessionSummary(summary="summary chunk 1", updated_at=datetime.now(UTC))

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
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=10_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            active_context_window=64_000,
            replay_window_tokens=64_000,
            threshold_tokens=None,
            reserve_tokens=0,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    interrupted = get_agent_session(storage, "session-1")
    assert interrupted is not None
    assert interrupted.summary is not None
    assert interrupted.summary.summary == "summary chunk 1"
    assert [run.run_id for run in interrupted.runs or []] == ["run-2"]
    interrupted_state = read_scope_state(interrupted, _SCOPE)
    assert interrupted_state.compacted_run_ids == ("run-1",)
    assert interrupted_state.force_compact_before_next_run is True
    assert "RUN1-MARKER" in first_pass_inputs[0]

    # A stale writer resurrects the already-compacted run before the restart.
    stale_session = deepcopy(interrupted)
    stale_session.runs = [_completed_run("run-1", marker="RUN1-MARKER", padding=16_000), *(interrupted.runs or [])]
    storage.upsert_session(stale_session)

    restart_inputs: list[str] = []

    async def restart_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        restart_inputs.append(summary_input)
        return SessionSummary(summary="summary chunk 2", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=restart_summary),
        ),
    ):
        await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "summary chunk 2"
    assert persisted.runs == []
    final_state = read_scope_state(persisted, _SCOPE)
    assert set(final_state.compacted_run_ids) == {"run-1", "run-2"}
    assert final_state.force_compact_before_next_run is False
    # The restart consumed the partial summary and never re-summarized run-1.
    assert len(restart_inputs) == 1
    assert "summary chunk 1" in restart_inputs[0]
    assert "RUN2-MARKER" in restart_inputs[0]
    assert "RUN1-MARKER" not in restart_inputs[0]
    storage.close()


# --- Invariant 3: one summary model configuration path ------------------------


def test_configure_summary_model_tunes_claude_in_one_place() -> None:
    model = Claude(
        id="claude-sonnet-4-6",
        cache_system_prompt=True,
        extended_cache_time=True,
        thinking={"type": "enabled", "budget_tokens": 8192},
        max_tokens=64_000,
        timeout=3600.0,
        client_params={"max_retries": 2, "custom": "keep"},
    )

    configured = configure_summary_model(model)

    assert configured is model
    assert model.cache_system_prompt is False
    assert model.extended_cache_time is False
    assert model.thinking is None
    assert model.max_tokens == SUMMARY_MAX_OUTPUT_TOKENS
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0, "custom": "keep"}


def test_configure_summary_model_tunes_vertexai_claude() -> None:
    model = MindroomVertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=True,
        extended_cache_time=True,
        max_tokens=8192,
        timeout=300.0,
    )

    configure_summary_model(model)

    assert model.cache_system_prompt is False
    assert model.extended_cache_time is False
    assert model.thinking is None
    assert model.max_tokens == SUMMARY_MAX_OUTPUT_TOKENS
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0}


def test_configure_summary_model_only_caps_downward() -> None:
    model = Claude(id="claude-sonnet-4-6", max_tokens=1024, timeout=30.0)

    configure_summary_model(model)

    assert model.max_tokens == 1024
    assert model.timeout == 30.0


def test_configure_summary_model_leaves_unknown_providers_untouched() -> None:
    model = FakeModel(id="test-model", provider="fake")

    configured = configure_summary_model(model)

    assert configured is model
    assert model == FakeModel(id="test-model", provider="fake")


@pytest.mark.asyncio
async def test_generate_compaction_summary_applies_tuning_and_request_shape() -> None:
    model = _RecordingClaude(
        id="claude-sonnet-4-6",
        cache_system_prompt=True,
        extended_cache_time=True,
        thinking={"type": "enabled", "budget_tokens": 8192},
        max_tokens=64_000,
        client_params={"max_retries": 2},
    )

    summary = await generate_compaction_summary(
        model=model,
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
    )

    assert summary.summary == "recorded summary"
    assert model.cache_system_prompt is False
    assert model.extended_cache_time is False
    assert model.thinking is None
    assert model.max_tokens == SUMMARY_MAX_OUTPUT_TOKENS
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0}
    assert [(message.role, message.content) for message in model.seen_messages] == [
        ("system", "Summarize the conversation."),
        ("user", "conversation payload"),
    ]


@pytest.mark.asyncio
async def test_generate_compaction_summary_rejects_output_cap_truncation() -> None:
    with pytest.raises(RuntimeError, match="likely truncated"):
        await generate_compaction_summary(
            model=_RecordingClaude(
                id="claude-sonnet-4-6",
                max_tokens=64_000,
                response=ModelResponse(
                    content="durable summary ended cleanly.",
                    output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
                ),
            ),
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
        )


@pytest.mark.asyncio
async def test_generate_compaction_summary_uses_configured_output_cap() -> None:
    with pytest.raises(RuntimeError, match="likely truncated"):
        await generate_compaction_summary(
            model=_RecordingClaude(
                id="claude-sonnet-4-6",
                max_tokens=1_024,
                response=ModelResponse(content="durable summary ended cleanly.", output_tokens=1_024),
            ),
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
        )


@pytest.mark.asyncio
async def test_generate_compaction_summary_allows_claude_summary_below_output_cap() -> None:
    summary = await generate_compaction_summary(
        model=_RecordingClaude(
            id="claude-sonnet-4-6",
            max_tokens=64_000,
            response=ModelResponse(
                content="durable summary ended cleanly.",
                output_tokens=SUMMARY_MAX_OUTPUT_TOKENS - 1,
            ),
        ),
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
    )

    assert summary.summary == "durable summary ended cleanly."


@pytest.mark.asyncio
async def test_generate_compaction_summary_allows_unknown_provider_without_output_cap() -> None:
    class _UncappedSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            return ModelResponse(content="durable summary ended cleanly.", output_tokens=SUMMARY_MAX_OUTPUT_TOKENS + 1)

    summary = await generate_compaction_summary(
        model=_UncappedSummaryModel(id="summary-model", provider="fake"),
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
    )

    assert summary.summary == "durable summary ended cleanly."


def test_build_summary_request_messages_is_the_single_request_seam() -> None:
    messages = build_summary_request_messages(summary_prompt="prompt", summary_input="input")

    assert [(message.role, message.content) for message in messages] == [
        ("system", "prompt"),
        ("user", "input"),
    ]


# --- Invariant 4: deterministic budget shrink on provider failure --------------


def test_retry_policy_shrinks_on_timeout_and_context_length_errors() -> None:
    policy = DEFAULT_SUMMARY_RETRY_POLICY

    assert policy.retry_budget(attempt=1, budget=16_000, error=TimeoutError()) == 8_000
    assert policy.retry_budget(attempt=1, budget=16_000, error=RuntimeError("context_length_exceeded")) == 8_000
    assert policy.retry_budget(attempt=1, budget=16_000, error=RuntimeError("request too large")) == 8_000
    assert (
        policy.retry_budget(
            attempt=1,
            budget=16_000,
            error=RuntimeError(f"compaction summary timed out after {MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS}s"),
        )
        == 8_000
    )


def test_retry_policy_gives_up_on_non_retryable_errors() -> None:
    assert DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(attempt=1, budget=16_000, error=RuntimeError("boom")) is None
    assert DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(attempt=1, budget=16_000, error=ValueError("401")) is None


def test_retry_policy_gives_up_after_max_attempts() -> None:
    assert DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(attempt=2, budget=16_000, error=TimeoutError()) is None


def test_retry_policy_clamps_to_floor_and_stops_there() -> None:
    policy = SummaryRetryPolicy(max_attempts=10)

    assert policy.retry_budget(attempt=1, budget=1_500, error=TimeoutError()) == 1_000
    assert policy.retry_budget(attempt=1, budget=1_000, error=TimeoutError()) is None
    assert policy.retry_budget(attempt=1, budget=999, error=TimeoutError()) is None


def test_retry_schedule_halves_deterministically() -> None:
    policy = SummaryRetryPolicy(max_attempts=10)
    budgets = []
    budget = 16_000
    attempt = 1
    while (next_budget := policy.retry_budget(attempt=attempt, budget=budget, error=TimeoutError())) is not None:
        budgets.append(next_budget)
        budget = next_budget
        attempt += 1

    assert budgets == [8_000, 4_000, 2_000, 1_000]
