"""Tests for history compaction token breakdown (ISSUE-074)."""
# ruff: noqa: D102

from __future__ import annotations

from unittest.mock import MagicMock

from agno.agent import Agent
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.history.compaction import (
    compute_prompt_token_breakdown,
    estimate_static_tokens,
    estimate_tool_definition_tokens,
)
from mindroom.history.types import CompactionOutcome

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
        "threshold_tokens": 80_000,
        "reserve_tokens": 4_096,
        "runs_before": 20,
        "runs_after": 8,
        "compacted_run_count": 12,
        "compacted_at": "2026-01-01T00:00:00Z",
        "notify": True,
    }
    defaults.update(overrides)
    return CompactionOutcome(**defaults)


# ---------------------------------------------------------------------------
# CompactionOutcome tests
# ---------------------------------------------------------------------------


class TestCompactionOutcome:
    """Tests for CompactionOutcome dataclass."""

    def test_format_notice_keeps_basic_format_without_breakdown(self) -> None:
        outcome = _make_outcome(window_tokens=128_000)
        notice = outcome.format_notice()
        assert notice == "Conversation compacted (~30,000 → ~12,000 / 128,000 tokens; 12 runs summarized)."

    def test_format_notice_uses_enriched_token_breakdown(self) -> None:
        outcome = _make_outcome(
            role_instructions_tokens=35_000,
            tool_definition_tokens=15_000,
            current_prompt_tokens=62_000,
            window_tokens=128_000,
        )
        notice = outcome.format_notice()
        assert notice == (
            "Conversation compacted (~30,000 → ~12,000 / 128,000 tokens; "
            "role/instructions ~35,000; tools ~15,000; current prompt ~62,000; "
            "12 runs summarized)."
        )

    def test_format_notice_with_partial_breakdown(self) -> None:
        outcome = _make_outcome(role_instructions_tokens=8_000)
        notice = outcome.format_notice()
        assert "role/instructions ~8,000" in notice
        assert "tools" not in notice

    def test_to_notice_metadata_basic(self) -> None:
        outcome = _make_outcome()
        meta = outcome.to_notice_metadata()
        assert meta["version"] == 1
        assert meta["before_tokens"] == 30_000
        assert meta["after_tokens"] == 12_000
        assert meta["compacted_run_count"] == 12
        assert "role_instructions_tokens" not in meta
        assert "tool_definition_tokens" not in meta
        assert "current_prompt_tokens" not in meta

    def test_to_notice_metadata_with_breakdown(self) -> None:
        outcome = _make_outcome(
            role_instructions_tokens=2_000,
            tool_definition_tokens=1_500,
            current_prompt_tokens=100,
        )
        meta = outcome.to_notice_metadata()
        assert meta["role_instructions_tokens"] == 2_000
        assert meta["tool_definition_tokens"] == 1_500
        assert meta["current_prompt_tokens"] == 100


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
