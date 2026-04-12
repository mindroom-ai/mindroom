"""Tests for history compaction token breakdown (ISSUE-074)."""
# ruff: noqa: D102

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.ai import _prepare_agent_and_prompt
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.execution_preparation import PreparedExecutionContext
from mindroom.history.compaction import (
    compute_prompt_token_breakdown,
    estimate_static_tokens,
    estimate_tool_definition_tokens,
)
from mindroom.history.types import CompactionOutcome, _to_k
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    role: str = "Short role.",
    instructions: list[str] | None = None,
) -> MagicMock:
    agent = MagicMock(spec=Agent)
    agent.role = role
    agent.instructions = instructions or []
    agent.tools = None
    return agent


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
        "reserve_tokens": 4_096,
        "runs_before": 20,
        "runs_after": 8,
        "compacted_run_count": 12,
        "compacted_at": "2026-01-01T00:00:00Z",
        "notify": True,
    }
    if "window_tokens" in overrides and "history_budget_tokens" not in overrides:
        defaults["history_budget_tokens"] = overrides["window_tokens"]
    defaults.update(overrides)
    return CompactionOutcome(**defaults)


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


# ---------------------------------------------------------------------------
# _to_k helper tests
# ---------------------------------------------------------------------------


class TestToK:
    """Tests for _to_k floor-rounding helper."""

    def test_boundary_values(self) -> None:
        assert _to_k(0) == "0"
        assert _to_k(999) == "999"
        assert _to_k(1000) == "~1K"
        assert _to_k(1499) == "~1K"
        assert _to_k(1500) == "~1K"
        assert _to_k(1999) == "~1K"
        assert _to_k(2000) == "~2K"
        assert _to_k(2500) == "~2K"
        assert _to_k(3500) == "~3K"
        assert _to_k(145826) == "~145K"

    def test_no_fabricated_savings_at_boundary(self) -> None:
        """before=1500, after=1499 must not show different K buckets."""
        assert _to_k(1500) == _to_k(1499)


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

    def test_format_notice_uses_enriched_token_breakdown(self) -> None:
        outcome = _make_outcome(
            role_instructions_tokens=35_000,
            tool_definition_tokens=15_000,
            current_prompt_tokens=62_000,
            window_tokens=128_000,
        )
        notice = outcome.format_notice()
        assert notice == (
            "\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000 / 128,000 history budget\n"
            "   Overhead: ~35K instructions + ~15K tools + ~62K prompt"
        )

    def test_format_notice_with_partial_breakdown(self) -> None:
        outcome = _make_outcome(role_instructions_tokens=8_000)
        notice = outcome.format_notice()
        assert "~8K instructions" in notice
        assert "tools" not in notice

    def test_format_notice_suppresses_zero_valued_breakdown(self) -> None:
        outcome = _make_outcome(
            role_instructions_tokens=0,
            tool_definition_tokens=0,
            current_prompt_tokens=62_000,
        )
        notice = outcome.format_notice()
        assert "instructions" not in notice
        assert "tools" not in notice
        assert "~62K prompt" in notice

    def test_format_notice_all_zero_breakdown_omits_overhead_line(self) -> None:
        outcome = _make_outcome(
            role_instructions_tokens=0,
            tool_definition_tokens=0,
            current_prompt_tokens=0,
        )
        notice = outcome.format_notice()
        assert "Overhead" not in notice

    def test_format_notice_omits_unknown_history_budget(self) -> None:
        outcome = _make_outcome(history_budget_tokens=None)
        notice = outcome.format_notice()
        assert notice == "\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000"

    def test_to_notice_metadata_basic(self) -> None:
        outcome = _make_outcome()
        meta = outcome.to_notice_metadata()
        assert meta["version"] == 2
        assert meta["before_tokens"] == 30_000
        assert meta["after_tokens"] == 12_000
        assert meta["history_budget_tokens"] == 100_000
        assert meta["compacted_run_count"] == 12
        assert "role_instructions_tokens" not in meta
        assert "tool_definition_tokens" not in meta
        assert "current_prompt_tokens" not in meta

    def test_to_notice_metadata_keeps_v1_window_tokens_when_history_budget_unknown(self) -> None:
        outcome = _make_outcome(history_budget_tokens=None)
        meta = outcome.to_notice_metadata()
        assert meta["version"] == 1
        assert meta["window_tokens"] == 100_000

    def test_to_notice_metadata_with_breakdown(self) -> None:
        outcome = _make_outcome(
            role_instructions_tokens=2_000,
            tool_definition_tokens=1_500,
            current_prompt_tokens=100,
        )
        meta = outcome.to_notice_metadata()
        assert meta["version"] == 2
        assert meta["history_budget_tokens"] == 100_000
        assert meta["role_instructions_tokens"] == 2_000
        assert meta["tool_definition_tokens"] == 1_500
        assert meta["current_prompt_tokens"] == 100


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_omits_zero_breakdown_segments_in_notice(tmp_path: Path) -> None:
    """Compaction notice enrichment should hide zero-valued overhead segments."""
    config, runtime_paths = _make_prepare_config(tmp_path)
    live_agent = _make_agent(role="", instructions=[])

    prepared_execution = PreparedExecutionContext(
        final_prompt="x" * 248,
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[_make_outcome()],
    )

    with (
        patch("mindroom.ai.build_memory_enhanced_prompt", new=AsyncMock(return_value="x" * 248)),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        _prepared_agent, _full_prompt, _unseen_event_ids, prepared = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            compaction_outcomes_collector=None,
        )

    outcome = prepared.compaction_outcomes[0]
    assert outcome.role_instructions_tokens == 0
    assert outcome.tool_definition_tokens == 0
    assert outcome.current_prompt_tokens == 62
    assert outcome.format_notice() == (
        "\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000 / 100,000 history budget\n   Overhead: 62 prompt"
    )


# ---------------------------------------------------------------------------
# Token estimation tests
# ---------------------------------------------------------------------------


class TestEstimateStaticTokens:
    """Tests for estimate_static_tokens."""

    def test_basic_estimation(self) -> None:
        agent = _make_agent(role="x" * 40, instructions=["y" * 20])
        tokens = estimate_static_tokens(agent, "z" * 80)
        # (40 + 20 + 80) / 4 = 35, no tools → + 0
        assert tokens == 35

    def test_string_instructions(self) -> None:
        agent = _make_agent(role="x" * 40)
        agent.instructions = "y" * 60
        tokens = estimate_static_tokens(agent, "z" * 100)
        # (40 + 60 + 100) / 4 = 50, no tools → + 0
        assert tokens == 50

    def test_none_role(self) -> None:
        agent = _make_agent()
        agent.role = None
        tokens = estimate_static_tokens(agent, "hello")
        assert tokens == len("hello") // 4


class TestEstimateToolDefinitionTokens:
    """Tests for estimate_tool_definition_tokens."""

    def test_no_tools(self) -> None:
        agent = _make_agent()
        assert estimate_tool_definition_tokens(agent) == 0

    def test_with_toolkit(self) -> None:
        func = Function(
            name="test_func",
            description="A test function",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        toolkit = Toolkit(name="test_toolkit")
        toolkit.functions = {"test_func": func}
        agent = _make_agent()
        agent.tools = [toolkit]
        tokens = estimate_tool_definition_tokens(agent)
        assert tokens > 0

    def test_with_function(self) -> None:
        func = Function(
            name="calculator",
            description="Does math",
            parameters={"type": "object"},
        )
        agent = _make_agent()
        agent.tools = [func]
        tokens = estimate_tool_definition_tokens(agent)
        assert tokens > 0


class TestComputePromptTokenBreakdown:
    """Tests for compute_prompt_token_breakdown."""

    def test_returns_all_keys(self) -> None:
        agent = _make_agent(role="x" * 100, instructions=["y" * 50])
        breakdown = compute_prompt_token_breakdown(agent=agent, full_prompt="z" * 200)
        assert "role_instructions_tokens" in breakdown
        assert "tool_definition_tokens" in breakdown
        assert "current_prompt_tokens" in breakdown

    def test_role_instructions_tokens_value(self) -> None:
        agent = _make_agent(role="x" * 40, instructions=["y" * 60])
        breakdown = compute_prompt_token_breakdown(agent=agent, full_prompt="prompt")
        # (40 + 60) / 4 = 25
        assert breakdown["role_instructions_tokens"] == 25

    def test_current_prompt_tokens_value(self) -> None:
        agent = _make_agent()
        breakdown = compute_prompt_token_breakdown(agent=agent, full_prompt="x" * 120)
        assert breakdown["current_prompt_tokens"] == 30

    def test_team_tools_are_included(self) -> None:
        team = MagicMock()
        team.tools = [Function.from_callable(lambda value: value)]

        breakdown = compute_prompt_token_breakdown(team=team, full_prompt="z" * 200)

        assert breakdown["tool_definition_tokens"] > 0
        assert breakdown["current_prompt_tokens"] == 50

    def test_no_prompt(self) -> None:
        agent = _make_agent()
        breakdown = compute_prompt_token_breakdown(agent=agent)
        assert "current_prompt_tokens" not in breakdown
