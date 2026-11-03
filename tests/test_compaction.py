"""Tests for compaction policy, outcome notices, and AI run metadata."""
# ruff: noqa: D102

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.history.policy import classify_compaction_decision
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import read_scope_state, with_force_compaction
from mindroom.history.types import (
    CompactionOutcome,
    HistoryScope,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedReplayPlan,
)
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, seed_session, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outcome(**overrides: object) -> CompactionOutcome:
    """Create a CompactionOutcome with sensible defaults for tests."""
    defaults: dict[str, object] = {
        "mode": "auto",
        "session_id": "test-session",
        "scope": "agent:test",
        "summary": "test summary",
        "summary_model": "test-model",
        "before_tokens": 30_000,
        "after_tokens": 12_000,
        "window_tokens": 100_000,
        "history_budget_tokens": 100_000,
        "threshold_tokens": 80_000,
        "runs_before": 20,
        "runs_after": 8,
        "compacted_run_count": 12,
        "compacted_at": "2026-01-01T00:00:00Z",
    }
    if "window_tokens" in overrides and "history_budget_tokens" not in overrides:
        defaults["history_budget_tokens"] = overrides["window_tokens"]
    defaults.update(overrides)
    return CompactionOutcome(**defaults)


def _make_execution_plan() -> ResolvedHistoryExecutionPlan:
    return ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
        text_compaction_available=True,
        explicit_compaction_model=False,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=10_000,
        reserve_tokens=4_096,
        trigger_tokens=10_000,
        limit_tokens=59_904,
        summary_input_budget_tokens=20_000,
        compaction_timeout_seconds=600.0,
    )


def _make_prepare_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    """Create a runtime-bound config for compaction enrichment tests."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
            },
        ),
        runtime_paths,
    )
    return config, runtime_paths


@pytest.mark.parametrize(
    ("plan_changes", "force", "request_tokens", "summary_request_tokens", "allow_native", "expected"),
    [
        ({}, True, 70_000, 0, True, ("required", "forced")),
        ({"text_compaction_available": False}, True, 5_000, 0, True, ("none", "forced_unavailable")),
        ({"automatic_compaction": False}, False, 60_000, 0, True, ("none", "auto_disabled")),
        ({"text_compaction_available": False}, False, 60_000, 0, True, ("none", "compaction_unavailable")),
        ({"trigger_tokens": None}, False, 60_000, 0, True, ("none", "missing_budget")),
        ({}, False, 10_000, 0, True, ("none", "under_trigger")),
        ({}, False, 60_000, 0, True, ("required", "request_exceeds_limit")),
        # A request exactly at the limit still fits it, and one exactly at the trigger has not passed it.
        ({}, False, 59_904, 0, True, ("none", "within_limit")),
        ({}, False, 10_000, 0, False, ("none", "under_trigger")),
        ({}, False, 12_001, 9_999, False, ("required", "over_trigger_native_disabled")),
        # The request would still reach the trigger with the summary as its only history.
        ({}, False, 12_001, 10_000, False, ("none", "trigger_unreachable")),
        ({}, False, 12_001, 0, True, ("none", "within_limit")),
    ],
)
def test_compaction_policy_classifies_every_branch(
    plan_changes: dict[str, object],
    *,
    force: bool,
    request_tokens: int,
    summary_request_tokens: int,
    allow_native: bool,
    expected: tuple[str, str],
) -> None:
    """Each decision keeps its precedence and reason, and reports the request's size and the plan's totals."""
    plan = replace(_make_execution_plan(), **plan_changes)

    decision = classify_compaction_decision(
        plan=plan,
        force_compact_before_next_run=force,
        request_tokens=request_tokens,
        summary_request_tokens=summary_request_tokens,
        allow_native_compaction=allow_native,
    )

    assert (decision.mode, decision.reason) == expected
    assert decision.request_tokens == request_tokens
    assert decision.trigger_tokens == plan.trigger_tokens
    assert decision.limit_tokens == plan.limit_tokens


# ---------------------------------------------------------------------------
# CompactionOutcome tests
# ---------------------------------------------------------------------------


class TestCompactionOutcome:
    """Tests for CompactionOutcome dataclass."""

    def test_format_notice_keeps_basic_format_without_breakdown(self) -> None:
        outcome = _make_outcome(window_tokens=128_000)
        notice = outcome.format_notice()
        assert notice == "\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000 / 128,000 history budget"

    def test_format_notice_keeps_exact_history_counts_near_rounding_boundary(self) -> None:
        outcome = _make_outcome(before_tokens=1_500, after_tokens=1_499, window_tokens=2_000)
        notice = outcome.format_notice()
        assert notice == "\U0001f4e6 Compacted 12 runs: 1,500 \u2192 1,499 / 2,000 history budget"

    def test_format_notice_omits_unknown_history_budget(self) -> None:
        outcome = _make_outcome(history_budget_tokens=None)
        notice = outcome.format_notice()
        assert notice == "\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000"

    def test_to_notice_metadata_basic(self) -> None:
        outcome = _make_outcome()
        meta = outcome.to_notice_metadata()
        assert meta["version"] == 3
        assert meta["before_tokens"] == 30_000
        assert meta["after_tokens"] == 12_000
        assert meta["history_budget_tokens"] == 100_000
        assert meta["threshold_tokens"] == 80_000
        assert meta["compacted_run_count"] == 12

    def test_to_notice_metadata_keeps_window_tokens_when_history_budget_unknown(self) -> None:
        outcome = _make_outcome(history_budget_tokens=None)
        meta = outcome.to_notice_metadata()
        assert meta["version"] == 3
        assert meta["window_tokens"] == 100_000


def test_ai_run_metadata_separates_compaction_and_prepared_context_tokens(tmp_path: Path) -> None:
    """Run metadata should expose prepared estimates without mixing them into provider usage."""
    config, _runtime_paths = _make_prepare_config(tmp_path)
    prepared_history = PreparedHistoryState(
        compaction_decision=classify_compaction_decision(
            plan=_make_execution_plan(),
            force_compact_before_next_run=False,
            request_tokens=12_001,
            summary_request_tokens=0,
            allow_native_compaction=True,
        ),
        compaction_reply_outcome="none",
        replay_plan=ResolvedReplayPlan(
            mode="configured",
            estimated_tokens=12_001,
            add_history_to_context=True,
        ),
        prepared_context_tokens=20_000,
        native_history=None,
    )

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name="default",
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        model="test-model",
        model_provider="openai",
        tool_count=0,
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=123, output_tokens=45, total_tokens=168)),
        prepared_history=prepared_history,
    )

    payload = metadata[AI_RUN_METADATA_KEY]
    assert payload["usage"]["input_tokens"] == 123
    assert payload["prepared_context"]["tokens"] == 20_000
    assert payload["compaction"] == {
        "decision": "none",
        "outcome": "none",
        "reason": "within_limit",
        "request_tokens": 12_001,
        "trigger_tokens": 10_000,
        "limit_tokens": 59_904,
        "replay_plan": {
            "mode": "configured",
            "estimated_tokens": 12_001,
        },
    }


def test_ai_run_metadata_bounds_context_cache_split_to_displayed_context(tmp_path: Path) -> None:
    """Context cache counters should not exceed the context size exposed to clients.

    An OpenAI-compatible server can report more cached tokens than prompt tokens.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=200_000)},
        ),
        runtime_paths,
    )
    counts = TokenCounts(input_tokens=153_294, cache_read_tokens=281_264, cache_write_tokens=500)
    answer = Message(
        role="assistant",
        content="ok",
        metrics=MessageMetrics(tokens=counts),
        provider_data=model_provenance("test-model", "OpenAI Chat"),
    )

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name="default",
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        model="test-model",
        model_provider="OpenAI",
        tool_count=0,
        metrics=RunMetrics(tokens=counts),
        context_run=RunRecord(run_id="run-1", agent_id="test_agent", status=RunStatus.completed, messages=[answer]),
    )

    context = metadata[AI_RUN_METADATA_KEY]["context"]
    assert context["input_tokens"] == 153_294
    assert context["window_tokens"] == 200_000
    assert context["cache_read_input_tokens"] == 153_294
    assert context["uncached_input_tokens"] == 0
    assert "cache_write_input_tokens" not in context


def test_team_scope_storage_is_shared_across_requesters(tmp_path: Path) -> None:
    """Team history storage is scoped to the shared conversation, not the sender."""
    config, runtime_paths = _make_prepare_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="super_team")
    first_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id="tenant-a",
        account_id=None,
    )
    second_identity = replace(first_identity, requester_id="@bob:localhost")

    first_storage = create_scope_session_storage(
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=first_identity,
    )
    second_storage = create_scope_session_storage(
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=second_identity,
    )
    session = ConversationSession(
        session_id="session-1",
        kind="team",
        owner_id="super_team",
        metadata={"source": "alice"},
        created_at=1,
    )
    session.metadata = with_force_compaction(session.metadata)
    seed_session(first_storage, session)

    persisted = second_storage.load_session("session-1")

    assert first_storage.path == second_storage.path
    assert persisted is not None
    assert persisted.metadata["source"] == "alice"
    state = read_scope_state(persisted)
    assert state.force_compact_before_next_run is True
