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
from mindroom.history.storage import _with_scope_state, read_scope_state
from mindroom.history.types import (
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedReplayPlan,
)
from mindroom.llm.usage import RunMetrics, TokenCounts
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


def _make_execution_plan(**overrides: object) -> ResolvedHistoryExecutionPlan:
    defaults: dict[str, object] = {
        "authored_compaction_enabled": True,
        "text_compaction_available": True,
        "explicit_compaction_model": False,
        "compaction_model_name": "summary-model",
        "replay_window_tokens": 64_000,
        "trigger_threshold_tokens": 12_000,
        "reserve_tokens": 4_096,
        "static_prompt_tokens": 0,
        "replay_budget_tokens": 10_000,
        "summary_input_budget_tokens": 20_000,
        "compaction_timeout_seconds": 600.0,
        "hard_replay_budget_tokens": 59_904,
    }
    defaults.update(overrides)
    return ResolvedHistoryExecutionPlan(**defaults)


def _make_policy_plan() -> ResolvedHistoryExecutionPlan:
    return ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=False,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=12_000,
        reserve_tokens=4_096,
        static_prompt_tokens=0,
        replay_budget_tokens=10_000,
        summary_input_budget_tokens=20_000,
        compaction_timeout_seconds=600.0,
        hard_replay_budget_tokens=59_904,
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
    ("plan_changes", "force", "history_tokens", "allow_native", "expected"),
    [
        ({}, True, 70_000, True, ("required", "forced", 59_904)),
        ({}, True, None, True, ("required", "forced", 0)),
        ({"text_compaction_available": False}, True, 5_000, True, ("none", "forced_unavailable", None)),
        ({"authored_compaction_enabled": False}, False, 60_000, True, ("none", "auto_disabled", None)),
        ({"text_compaction_available": False}, False, 60_000, True, ("none", "compaction_unavailable", None)),
        ({"replay_budget_tokens": None}, False, 60_000, True, ("none", "missing_budget", None)),
        ({}, False, None, True, ("none", "missing_budget", None)),
        ({}, False, 10_000, True, ("none", "under_trigger", 10_000)),
        ({}, False, 60_000, True, ("required", "history_exceeds_hard_budget", 59_904)),
        ({}, False, 12_001, False, ("required", "over_trigger_native_disabled", 10_000)),
        ({}, False, 12_001, True, ("none", "within_hard_budget", 12_001)),
    ],
)
def test_compaction_policy_classifies_every_branch(
    plan_changes: dict[str, object],
    *,
    force: bool,
    history_tokens: int | None,
    allow_native: bool,
    expected: tuple[str, str, int | None],
) -> None:
    """Each decision keeps its precedence, reason, and fitted replay size, and reports the plan's budgets."""
    plan = replace(_make_policy_plan(), **plan_changes)

    decision = classify_compaction_decision(
        plan=plan,
        force_compact_before_next_run=force,
        current_history_tokens=history_tokens,
        allow_native_compaction=allow_native,
    )

    assert (decision.mode, decision.reason, decision.fitted_replay_tokens) == expected
    assert decision.current_history_tokens == history_tokens
    assert decision.trigger_budget_tokens == plan.replay_budget_tokens
    assert decision.hard_budget_tokens == plan.hard_replay_budget_tokens


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
            current_history_tokens=12_001,
        ),
        compaction_reply_outcome="none",
        replay_plan=ResolvedReplayPlan(
            mode="configured",
            estimated_tokens=12_001,
            add_history_to_context=True,
        ),
        prepared_context_tokens=20_000,
    )

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name="default",
        run_id="run-1",
        session_id="session-1",
        status="completed",
        model="test-model",
        model_provider="openai",
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=123, output_tokens=45, total_tokens=168)),
        prepared_history=prepared_history,
    )

    payload = metadata[AI_RUN_METADATA_KEY]
    assert payload["usage"]["input_tokens"] == 123
    assert payload["prepared_context"]["tokens"] == 20_000
    assert payload["compaction"] == {
        "decision": "none",
        "outcome": "none",
        "reason": "within_hard_budget",
        "current_history_tokens": 12_001,
        "trigger_budget_tokens": 10_000,
        "hard_budget_tokens": 59_904,
        "fitted_replay_tokens": 12_001,
        "replay_plan": {
            "mode": "configured",
            "estimated_tokens": 12_001,
        },
    }


def test_ai_run_metadata_bounds_context_cache_split_to_displayed_context(tmp_path: Path) -> None:
    """Context cache counters should not exceed the context size exposed to clients."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="vertexai_claude",
                    id="claude-opus-5",
                    context_window=200_000,
                ),
            },
        ),
        runtime_paths,
    )

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name="default",
        run_id="run-1",
        session_id="session-1",
        status="completed",
        model="claude-opus-5",
        model_provider="VertexAI",
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=153_294, cache_read_tokens=281_264)),
        context_input_tokens=153_294,
        context_cache_read_tokens=281_264,
        context_cache_write_tokens=500,
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
    second_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@bob:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id="tenant-a",
        account_id=None,
    )

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
        updated_at=1,
    )
    session.metadata = _with_scope_state(session.metadata, scope, HistoryScopeState(force_compact_before_next_run=True))
    seed_session(first_storage, session)

    persisted = second_storage.load_session("session-1", "team")

    assert first_storage.path == second_storage.path
    assert persisted is not None
    assert persisted.metadata["source"] == "alice"
    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is True
