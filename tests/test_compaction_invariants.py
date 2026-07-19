"""Tests for the four history-compaction invariants.

1. Compacted runs never reappear (``mindroom.history.storage``).
2. Chunk progress survives interruption (``mindroom.history.storage``).
3. Summary calls get exactly one model configuration path (``mindroom.history.summary_call``).
4. Retry on provider failure is deterministic (``mindroom.history.summary_call``).

These tests exercise the owning interfaces directly, not the bot runtime.
"""
# ruff: noqa: D103, TC003

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agno.agent import Agent
from agno.exceptions import ContextWindowExceededError, ModelProviderError
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from anthropic import APIConnectionError as AnthropicAPIConnectionError
from openai import APIConnectionError as OpenAIAPIConnectionError

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
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.history.compaction import (
    _build_summary_input,
    _generate_compaction_summary_with_retry,
    compact_scope_history,
)
from mindroom.history.storage import (
    compacted_run_ids_with,
    prune_reintroduced_runs,
    read_scope_state,
    remove_runs_by_id,
    update_scope_state_on_latest,
    write_scope_state,
)
from mindroom.history.summary_call import (
    DEFAULT_SUMMARY_RETRY_POLICY,
    CompactionSummaryOutputLimitError,
    SummaryRetryDecision,
    SummaryRetryPolicy,
    _CompactionSummaryEmptyResultError,
    build_summary_request_messages,
    configure_summary_model,
    generate_compaction_summary,
)
from mindroom.history.types import (
    COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.token_budget import estimate_compaction_input_tokens
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


def _provider_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.test/v1/responses")


def _provider_error_with_cause(cause: BaseException) -> ModelProviderError:
    provider_error = ModelProviderError(str(cause))
    provider_error.__cause__ = cause
    return provider_error


def _provider_error_with_context(
    context: BaseException,
    *,
    suppress_context: bool = False,
) -> ModelProviderError:
    provider_error = ModelProviderError(str(context))
    provider_error.__context__ = context
    provider_error.__suppress_context__ = suppress_context
    return provider_error


def _provider_error_with_cyclic_cause() -> ModelProviderError:
    provider_error = ModelProviderError("cyclic provider failure")
    wrapper = RuntimeError("cyclic wrapper")
    provider_error.__cause__ = wrapper
    wrapper.__cause__ = provider_error
    return provider_error


def _connection_provider_error() -> ModelProviderError:
    sdk_error = OpenAIAPIConnectionError(request=_provider_request())
    return _provider_error_with_cause(sdk_error)


def _nested_connection_provider_error() -> ModelProviderError:
    request = _provider_request()
    sdk_error = OpenAIAPIConnectionError(request=request)
    sdk_error.__cause__ = httpx.ConnectError("connection reset", request=request)
    return _provider_error_with_cause(sdk_error)


def _retry_budget_value(
    policy: SummaryRetryPolicy,
    *,
    attempt: int,
    budget: int,
    input_tokens: int,
    minimum_input_tokens: int = 0,
    error: Exception,
) -> int | None:
    decision = policy.retry_budget(
        attempt=attempt,
        budget=budget,
        input_tokens=input_tokens,
        minimum_input_tokens=minimum_input_tokens,
        error=error,
    )
    return None if decision is None else decision.budget


def _chars_per_token_estimator(value: str) -> int:
    return len(value) // 4


def _make_config(
    tmp_path: Path,
    *,
    compaction: CompactionConfig | None = None,
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


def test_update_scope_state_on_latest_applies_update_to_freshest_row(tmp_path: Path) -> None:
    """The update callable sees the latest persisted state, and the write lands on that row."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stale_session = _session([_completed_run("run-1")])
    write_scope_state(stale_session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(stale_session)

    concurrent_session = get_agent_session(storage, "session-1")
    assert concurrent_session is not None
    concurrent_state = HistoryScopeState(force_compact_before_next_run=True, compacted_run_ids=("older-run",))
    write_scope_state(concurrent_session, _SCOPE, concurrent_state)
    concurrent_session.runs = [*(concurrent_session.runs or []), _completed_run("run-2")]
    storage.upsert_session(concurrent_session)

    seen_states: list[HistoryScopeState] = []

    def clear_force(latest: HistoryScopeState) -> HistoryScopeState:
        seen_states.append(latest)
        return HistoryScopeState(force_compact_before_next_run=False, compacted_run_ids=latest.compacted_run_ids)

    returned_state = update_scope_state_on_latest(storage, stale_session, _SCOPE, clear_force)

    assert seen_states == [concurrent_state]
    assert returned_state.force_compact_before_next_run is False
    assert returned_state.compacted_run_ids == ("older-run",)
    persisted_session = get_agent_session(storage, "session-1")
    assert persisted_session is not None
    assert read_scope_state(persisted_session, _SCOPE) == returned_state
    assert [run.run_id for run in stale_session.runs or []] == ["run-1", "run-2"]


def test_update_scope_state_on_latest_skips_write_when_update_is_a_no_op(tmp_path: Path) -> None:
    """A no-op update must not upsert but still syncs the session from the freshest row."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    stale_session = _session([_completed_run("run-1")])
    persisted_state = HistoryScopeState(compacted_run_ids=("older-run",))
    write_scope_state(stale_session, _SCOPE, persisted_state)
    storage.upsert_session(stale_session)

    concurrent_session = get_agent_session(storage, "session-1")
    assert concurrent_session is not None
    concurrent_session.runs = [*(concurrent_session.runs or []), _completed_run("run-2")]
    storage.upsert_session(concurrent_session)

    with patch.object(storage, "upsert_session", wraps=storage.upsert_session) as upsert_spy:
        returned_state = update_scope_state_on_latest(storage, stale_session, _SCOPE, lambda latest: latest)

    upsert_spy.assert_not_called()
    assert returned_state == persisted_state
    assert [run.run_id for run in stale_session.runs or []] == ["run-1", "run-2"]


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
            replay_window_tokens=64_000,
            threshold_tokens=None,
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
        id="claude-sonnet-5",
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
    assert model.max_tokens == 64_000
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0, "custom": "keep"}


def test_configure_summary_model_tunes_vertexai_claude() -> None:
    model = MindroomVertexAIClaude(
        id="claude-sonnet-5",
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
    assert model.max_tokens == 8192
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0}


def test_configure_summary_model_preserves_authored_output_cap() -> None:
    model = Claude(id="claude-sonnet-5", max_tokens=1024, timeout=30.0)

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
        id="claude-sonnet-5",
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
    assert model.max_tokens == 64_000
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0}
    assert [(message.role, message.content) for message in model.seen_messages] == [
        ("system", "Summarize the conversation."),
        ("user", "conversation payload"),
    ]


@pytest.mark.asyncio
async def test_generate_compaction_summary_rejects_output_cap_truncation() -> None:
    with pytest.raises(RuntimeError, match="output token limit"):
        await generate_compaction_summary(
            model=_RecordingClaude(
                id="claude-sonnet-5",
                max_tokens=64_000,
                response=ModelResponse(
                    content="durable summary ended cleanly.",
                    output_tokens=64_000,
                ),
            ),
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
        )


@pytest.mark.asyncio
async def test_generate_compaction_summary_uses_configured_output_cap() -> None:
    with pytest.raises(RuntimeError, match="output token limit"):
        await generate_compaction_summary(
            model=_RecordingClaude(
                id="claude-sonnet-5",
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
            id="claude-sonnet-5",
            max_tokens=64_000,
            response=ModelResponse(
                content="durable summary ended cleanly.",
                output_tokens=63_999,
            ),
        ),
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
    )

    assert summary.summary == "durable summary ended cleanly."


@pytest.mark.asyncio
async def test_generate_compaction_summary_allows_full_history_summary_above_four_k() -> None:
    summary = await generate_compaction_summary(
        model=_RecordingClaude(
            id="claude-sonnet-5",
            max_tokens=8192,
            response=ModelResponse(
                content="durable full-history summary ended cleanly.",
                output_tokens=4_097,
            ),
        ),
        summary_input="conversation payload",
        summary_prompt="Summarize the conversation.",
    )

    assert summary.summary == "durable full-history summary ended cleanly."


@pytest.mark.asyncio
async def test_generate_compaction_summary_uses_claude_default_output_cap() -> None:
    model = _RecordingClaude(id="claude-sonnet-5")
    default_output_cap = model.max_tokens
    assert default_output_cap is not None
    model.response = ModelResponse(
        content="durable summary ended cleanly.",
        output_tokens=default_output_cap,
    )

    with pytest.raises(RuntimeError, match="output token limit"):
        await generate_compaction_summary(
            model=model,
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
        )


@pytest.mark.asyncio
async def test_generate_compaction_summary_allows_unknown_provider_without_output_cap() -> None:
    class _UncappedSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            return ModelResponse(content="durable summary ended cleanly.", output_tokens=64_001)

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
    error = ContextWindowExceededError(message="provider-specific wording does not matter")

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
    error = ContextWindowExceededError(message="provider-specific wording does not matter")

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


def test_retry_policy_shrinks_budget_for_safeguard_refusal() -> None:
    error = ModelSafeguardRefusalError("provider-specific refusal wording")

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
            budget=8_000,
            input_tokens=8_000,
            error=error,
        )
        is None
    )


def test_retry_policy_should_shrink_for_refusal_and_empty_result() -> None:
    policy = DEFAULT_SUMMARY_RETRY_POLICY

    assert policy.should_shrink(ModelSafeguardRefusalError("provider-specific refusal wording")) is True
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


def test_retry_policy_clamps_shrink_target_to_minimum_lossless_input() -> None:
    """A durable summary above half the failed input raises the shrink target instead of cancelling it."""
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=10_000,
        input_tokens=8_573,
        minimum_input_tokens=6_230,
        error=ModelSafeguardRefusalError("provider-specific refusal wording"),
    )

    assert decision == SummaryRetryDecision(budget=6_230, kind="shrink")


def test_retry_policy_declines_shrink_when_no_smaller_lossless_input_exists() -> None:
    """An input already at the lossless minimum gets no shrink retry (bounded degrade)."""
    assert (
        DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
            attempt=1,
            budget=10_000,
            input_tokens=6_230,
            minimum_input_tokens=6_230,
            error=TimeoutError(),
        )
        is None
    )


def test_retry_policy_shrink_classification_precedes_transient_status() -> None:
    error = ModelProviderError("Request too large", status_code=429)
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=6_000,
        minimum_input_tokens=0,
        error=error,
    )

    assert decision is not None
    assert decision.budget == 3_000
    assert decision.kind == "shrink"


def test_retry_policy_falls_through_to_transient_retry_when_input_cannot_shrink() -> None:
    error = ModelProviderError("upstream connection timed out", status_code=503)
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=1_000,
        minimum_input_tokens=0,
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
            error=ModelSafeguardRefusalError("provider-specific refusal wording"),
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


@pytest.mark.parametrize("status_code", [429, 503, 529])
def test_retry_policy_retries_selected_transient_provider_errors_at_same_budget(status_code: int) -> None:
    error = ModelProviderError("temporary provider failure", status_code=status_code)
    decision = DEFAULT_SUMMARY_RETRY_POLICY.retry_budget(
        attempt=1,
        budget=16_000,
        input_tokens=4_000,
        minimum_input_tokens=0,
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


@pytest.mark.parametrize("status_code", [400, 401, 408, 500, 502, 504])
def test_retry_policy_does_not_retry_non_transient_provider_statuses(status_code: int) -> None:
    error = ModelProviderError("provider failure", status_code=status_code)

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
    ("error", "expected_budget"),
    [
        pytest.param(
            _provider_error_with_cause(ConnectionError("connection refused")),
            16_000,
            id="builtin-connection-error",
        ),
        pytest.param(
            _provider_error_with_cause(httpx.ConnectError("connection reset", request=_provider_request())),
            16_000,
            id="httpx-network-error",
        ),
        pytest.param(
            _provider_error_with_cause(AnthropicAPIConnectionError(request=_provider_request())),
            16_000,
            id="anthropic-api-connection-error",
        ),
        pytest.param(
            _nested_connection_provider_error(),
            16_000,
            id="nested-sdk-httpx-chain",
        ),
        pytest.param(
            _provider_error_with_context(ConnectionError("connection reset")),
            16_000,
            id="implicit-connection-context",
        ),
        pytest.param(
            _provider_error_with_context(
                ConnectionError("connection reset"),
                suppress_context=True,
            ),
            None,
            id="suppressed-connection-context",
        ),
        pytest.param(
            _provider_error_with_cyclic_cause(),
            None,
            id="cyclic-cause-chain",
        ),
        pytest.param(
            _provider_error_with_cause(ValueError("invalid provider response")),
            None,
            id="non-network-cause",
        ),
    ],
)
def test_retry_policy_classifies_default_status_by_typed_network_chain(
    error: ModelProviderError,
    expected_budget: int | None,
) -> None:
    assert error.status_code == 502
    assert (
        _retry_budget_value(
            DEFAULT_SUMMARY_RETRY_POLICY,
            attempt=1,
            budget=16_000,
            input_tokens=4_000,
            error=error,
        )
        == expected_budget
    )


def test_retry_policy_does_not_retry_default_provider_error_status() -> None:
    error = ModelProviderError("unclassified provider failure")

    assert error.status_code == 502
    assert error.__cause__ is None
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


def test_retry_policy_preserves_context_error_fragment_matches() -> None:
    policy = DEFAULT_SUMMARY_RETRY_POLICY

    assert (
        _retry_budget_value(
            policy,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=RuntimeError("context_length_exceeded"),
        )
        == 8_000
    )
    assert (
        _retry_budget_value(
            policy,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=RuntimeError("request too large"),
        )
        == 8_000
    )
    assert (
        _retry_budget_value(
            policy,
            attempt=1,
            budget=16_000,
            input_tokens=16_000,
            error=RuntimeError(f"compaction summary timed out after {MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS}s"),
        )
        == 8_000
    )


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
async def test_retry_helper_propagates_original_error_when_rebuilt_input_is_not_smaller() -> None:
    """A defensive estimate guard prevents a shrink retry from resending equal-size input."""
    run = _completed_run("run-1")
    original_error = ModelSafeguardRefusalError("provider-specific refusal wording")
    generate_summary = AsyncMock(side_effect=original_error)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=generate_summary,
        ),
        patch(
            "mindroom.history.compaction._build_summary_input",
            return_value=("rebuilt request with the same estimate", [run]),
        ),
        pytest.raises(ModelSafeguardRefusalError) as raised,
    ):
        await _generate_compaction_summary_with_retry(
            model=FakeModel(id="summary-model", provider="fake"),
            previous_summary=None,
            compactable_runs=[run],
            initial_summary_input="original request",
            initial_included_runs=[run],
            summary_input_budget=4_000,
            session_id="session-1",
            scope=_SCOPE,
            history_settings=_HISTORY_SETTINGS,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            token_estimator=lambda _value: 2_000,
        )

    assert raised.value is original_error
    generate_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_helper_honors_transient_fallthrough_for_shrink_message_at_floor() -> None:
    """A transient policy decision remains same-budget even when its message is shrinkable."""
    run = _completed_run("run-1")
    recovered_summary = SessionSummary(summary="recovered summary", updated_at=datetime.now(UTC))
    generate_summary = AsyncMock(
        side_effect=[
            ModelProviderError("upstream connection timed out", status_code=503),
            recovered_summary,
        ],
    )

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=generate_summary,
        ),
        patch(
            "mindroom.history.compaction._build_summary_input",
            return_value=("same-budget rebuilt request", [run]),
        ) as build_summary_input,
    ):
        generated = await _generate_compaction_summary_with_retry(
            model=FakeModel(id="summary-model", provider="fake"),
            previous_summary=None,
            compactable_runs=[run],
            initial_summary_input="original request",
            initial_included_runs=[run],
            summary_input_budget=4_000,
            session_id="session-1",
            scope=_SCOPE,
            history_settings=_HISTORY_SETTINGS,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            token_estimator=lambda _value: COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
        )

    assert generated.summary is recovered_summary
    assert generated.included_runs == [run]
    assert generate_summary.await_count == 2
    assert [call.kwargs["summary_input"] for call in generate_summary.await_args_list] == [
        "original request",
        "same-budget rebuilt request",
    ]
    assert build_summary_input.call_args.kwargs["max_input_tokens"] == 4_000


@pytest.mark.asyncio
async def test_retry_helper_shrinks_around_a_large_durable_summary() -> None:
    """A durable summary above half the failed input still gets a strictly smaller second attempt.

    Regression: the halved shrink target used to fall below the previous-summary
    block, the rebuild came back run-less, and the original error propagated
    without any smaller attempt — so the next turn reselected required
    compaction and resent the identical refusal-prone request.
    """
    previous_summary = "s" * 24_000
    runs = [_completed_run(f"run-{index}", padding=2_000) for index in range(3)]
    summary_input_budget = 10_000
    initial_input, initial_runs = _build_summary_input(
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
    recovered_summary = SessionSummary(summary="recovered summary", updated_at=datetime.now(UTC))
    generate_summary = AsyncMock(
        side_effect=[ModelSafeguardRefusalError("provider-specific refusal wording"), recovered_summary],
    )

    with patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary):
        generated = await _generate_compaction_summary_with_retry(
            model=FakeModel(id="summary-model", provider="fake"),
            previous_summary=previous_summary,
            compactable_runs=runs,
            initial_summary_input=initial_input,
            initial_included_runs=initial_runs,
            summary_input_budget=summary_input_budget,
            session_id="session-1",
            scope=_SCOPE,
            history_settings=_HISTORY_SETTINGS,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            token_estimator=_chars_per_token_estimator,
        )

    assert generated.summary is recovered_summary
    assert generate_summary.await_count == 2
    retry_input = generate_summary.await_args_list[1].kwargs["summary_input"]
    assert _chars_per_token_estimator(retry_input) < initial_tokens
    assert previous_summary in retry_input
    assert "<run " in retry_input.split("</previous_summary>")[1]
    assert [run.run_id for run in generated.included_runs] == ["run-0"]


@pytest.mark.asyncio
async def test_retry_helper_propagates_error_when_no_smaller_lossless_input_exists() -> None:
    """An input already at the lossless minimum fails after one call without a run-less request."""
    previous_summary = "s" * 24_000
    runs = [_completed_run("run-1", padding=2_000)]
    summary_input_budget = 6_100
    initial_input, initial_runs = _build_summary_input(
        previous_summary=previous_summary,
        compacted_runs=runs,
        history_settings=_HISTORY_SETTINGS,
        max_input_tokens=summary_input_budget,
        token_estimator=_chars_per_token_estimator,
    )
    assert [run.run_id for run in initial_runs] == ["run-1"]
    original_error = ModelSafeguardRefusalError("provider-specific refusal wording")
    generate_summary = AsyncMock(side_effect=original_error)

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=generate_summary),
        pytest.raises(ModelSafeguardRefusalError) as raised,
    ):
        await _generate_compaction_summary_with_retry(
            model=FakeModel(id="summary-model", provider="fake"),
            previous_summary=previous_summary,
            compactable_runs=runs,
            initial_summary_input=initial_input,
            initial_included_runs=initial_runs,
            summary_input_budget=summary_input_budget,
            session_id="session-1",
            scope=_SCOPE,
            history_settings=_HISTORY_SETTINGS,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
            token_estimator=_chars_per_token_estimator,
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
            model=_RecordingClaude(
                id="claude-sonnet-5",
                max_tokens=64_000,
                response=ModelResponse(content="", output_tokens=0),
            ),
            summary_input="conversation payload",
            summary_prompt="Summarize the conversation.",
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
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    summary_inputs: list[str] = []

    async def record_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        return SessionSummary(summary=f"summary chunk {len(summary_inputs)}", updated_at=datetime.now(UTC))

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
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=summary_input_limit,
            summary_model=_RecordingClaude(id="claude-sonnet-5"),
            summary_model_name="summary-model",
            replay_window_tokens=200_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
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
    storage.close()


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
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append(summary_input)
        if len(attempts) == 1:
            msg = "summary generation returned no result (output_tokens=0, has_reasoning=False)"
            raise _CompactionSummaryEmptyResultError(msg)
        return SessionSummary(summary="recovered summary", updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=flaky_summary),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=10_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    # Chunk 1 fails empty, is rebuilt smaller, then chunk 2 compacts run-2.
    assert len(attempts) == 3
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    assert attempts[2] != attempts[1]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "recovered summary"
    storage.close()


@pytest.mark.asyncio
async def test_compaction_shrinks_input_after_safeguard_refusal(tmp_path: Path) -> None:
    """A safeguard refusal retries with a smaller input and persists the result."""
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

    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append(summary_input)
        if len(attempts) == 1:
            msg = "provider-specific refusal wording"
            raise ModelSafeguardRefusalError(msg)
        return SessionSummary(summary="recovered summary", updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=flaky_summary),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=10_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    assert len(attempts) == 3
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    assert attempts[2] != attempts[1]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "recovered summary"
    storage.close()


@pytest.mark.asyncio
async def test_small_refused_summary_request_fails_without_identical_retry_or_persistent_state(
    tmp_path: Path,
) -> None:
    """A request below the retry floor fails only its current attempt without being resent."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER")])
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    attempts: list[str] = []

    async def refuse_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append(summary_input)
        message = "provider-specific refusal wording"
        raise ModelSafeguardRefusalError(message)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=refuse_summary),
        ),
        pytest.raises(ModelSafeguardRefusalError, match="provider-specific refusal wording"),
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
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert len(attempts) == 1
    assert estimate_compaction_input_tokens(attempts[0]) < 1_000
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE) == HistoryScopeState(force_compact_before_next_run=True)
    storage.close()


@pytest.mark.asyncio
async def test_minimum_available_budget_can_issue_smaller_degradation_retry(tmp_path: Path) -> None:
    """The smallest available plan can rebuild and issue its promised smaller retry."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=4_000)])
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    summary_input_budget = 2 * COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS + 1
    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append(summary_input)
        if len(attempts) == 1:
            message = "provider-specific refusal wording"
            raise ModelSafeguardRefusalError(message)
        return SessionSummary(summary="recovered summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ),
        patch(
            "mindroom.history.compaction._build_summary_input",
            wraps=_build_summary_input,
        ) as build_summary_input_spy,
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=summary_input_budget,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    assert len(attempts) == 2
    build_budgets = [call.kwargs["max_input_tokens"] for call in build_summary_input_spy.call_args_list]
    assert build_budgets[0] == summary_input_budget
    assert build_budgets[1] < build_budgets[0]
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "recovered summary"
    assert persisted.runs == []
    storage.close()


@pytest.mark.asyncio
async def test_near_cap_durable_summary_with_tiny_budget_is_unavailable_without_fact_loss(tmp_path: Path) -> None:
    """A degenerate plan is unavailable and cannot repeatedly select a destructive rewrite."""
    summary_input_budget = COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS + 1
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionConfig(replay_window_tokens=summary_input_budget),
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    previous_summary = ("word " * 975) + "TAIL-FACT-MUST-SURVIVE"
    session = _session([_completed_run("run-1", marker="RUN1-MARKER")])
    session.summary = SessionSummary(summary=previous_summary, updated_at=datetime.now(UTC))
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
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
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=summary_input_budget,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="default",
            replay_window_tokens=summary_input_budget,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is None
    generate_summary.assert_not_awaited()
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == previous_summary
    assert "TAIL-FACT-MUST-SURVIVE" in persisted.summary.summary
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE) == HistoryScopeState()

    runtime_generate_summary = AsyncMock()
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=runtime_generate_summary,
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
            session=persisted,
            static_prompt_tokens=0,
        )

    runtime_generate_summary.assert_not_awaited()
    assert prepared.compaction_decision.reason == "compaction_unavailable"
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.compaction_outcomes == []
    repeated = get_agent_session(storage, "session-1")
    assert repeated is not None
    assert repeated.summary is not None
    assert repeated.summary.summary == previous_summary
    assert [run.run_id for run in repeated.runs or []] == ["run-1"]
    assert read_scope_state(repeated, _SCOPE) == HistoryScopeState()
    storage.close()


@pytest.mark.asyncio
async def test_two_timeouts_exhaust_current_attempt_without_persisting_suppression(tmp_path: Path) -> None:
    """Retry exhaustion after a smaller timeout request leaves no durable policy state."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=16_000)])
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    attempts: list[str] = []

    async def time_out_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append(summary_input)
        raise TimeoutError

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=time_out_summary),
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

    assert len(attempts) == 2
    assert estimate_compaction_input_tokens(attempts[1]) < estimate_compaction_input_tokens(attempts[0])
    assert prepared.compaction_reply_outcome == "timeout"
    assert prepared.compaction_outcomes == []
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs or []] == ["run-1"]
    assert read_scope_state(persisted, _SCOPE) == HistoryScopeState()
    storage.close()


@pytest.mark.asyncio
async def test_successful_compaction_drops_legacy_persisted_block_fields(tmp_path: Path) -> None:
    """A successful rewrite removes block metadata written by the abandoned round-five design."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=4_000)])
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    metadata = session.metadata or {}
    raw_compaction = metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert isinstance(raw_compaction, dict)
    raw_states = raw_compaction["states"]
    assert isinstance(raw_states, dict)
    raw_state = raw_states[_SCOPE.key]
    assert isinstance(raw_state, dict)
    raw_state.update(
        {
            "blocked_compaction_reason": "summary_input_cannot_include_run",
            "blocked_compaction_model": "old-summary-model",
            "blocked_summary_input_budget": 1_001,
        },
    )
    storage.upsert_session(session)

    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary="new summary", updated_at=datetime.now(UTC))),
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=10_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    persisted_metadata = persisted.metadata or {}
    persisted_compaction = persisted_metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert isinstance(persisted_compaction, dict)
    persisted_states = persisted_compaction["states"]
    assert isinstance(persisted_states, dict)
    persisted_state = persisted_states[_SCOPE.key]
    assert isinstance(persisted_state, dict)
    assert not any(key.startswith("blocked_") for key in persisted_state)
    storage.close()


@pytest.mark.parametrize(
    "first_error",
    [
        pytest.param(ModelProviderError("temporary provider failure", status_code=503), id="status-503"),
        pytest.param(_connection_provider_error(), id="typed-connection-cause"),
    ],
)
@pytest.mark.asyncio
async def test_compaction_retries_transient_provider_error_at_same_budget(
    tmp_path: Path,
    first_error: ModelProviderError,
) -> None:
    """A selected typed transient failure retries the same chunk and persists the result."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", marker="RUN1-MARKER", padding=4_000)])
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    attempts: list[str] = []

    async def flaky_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        attempts.append(summary_input)
        if len(attempts) == 1:
            raise first_error
        return SessionSummary(summary="recovered summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=flaky_summary),
        ),
        patch(
            "mindroom.history.compaction._build_summary_input",
            wraps=_build_summary_input,
        ) as build_summary_input_spy,
    ):
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=_SCOPE,
            state=read_scope_state(session, _SCOPE),
            history_settings=_HISTORY_SETTINGS,
            available_history_budget=None,
            summary_input_budget=10_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            replay_window_tokens=64_000,
            threshold_tokens=None,
            summary_prompt=COMPACTION_SUMMARY_PROMPT,
        )

    assert outcome is not None
    assert outcome.compacted_run_count == 1
    assert len(attempts) == 2
    assert attempts[1] == attempts[0]
    assert [call.kwargs["max_input_tokens"] for call in build_summary_input_spy.call_args_list] == [10_000, 10_000]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "recovered summary"
    storage.close()
