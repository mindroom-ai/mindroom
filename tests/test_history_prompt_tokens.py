"""Tests for static prompt-token and history token estimation."""
# ruff: noqa: D103

from __future__ import annotations

import gc
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Thread
from typing import TYPE_CHECKING, Any, cast

from agno.agent import Agent
from agno.models.message import Message
from agno.run import RunContext
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.history.compaction import (
    _estimate_history_messages_tokens,
    estimate_prompt_visible_history_tokens,
    estimate_session_summary_tokens,
)
from mindroom.history.prompt_tokens import (
    _TOOL_SURFACE_CACHE,
    _prompt_tool_surface_for_tools,
    _StaticTokenEstimator,
    agent_static_token_estimator,
    agent_tool_definition_payloads_for_logging,
    estimate_agent_static_tokens,
)
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    ResolvedHistorySettings,
)
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from tests.conftest import (
    FakeModel,
)

if TYPE_CHECKING:
    import pytest
from tests.history_helpers import (  # noqa: F401
    _agent,
    _close_test_storages,
    _completed_run,
    _session,
)


def _tool_definition_tokens(tools: object) -> int:
    """Estimate model-visible tool schema and instruction tokens for one tool list."""
    surface = _prompt_tool_surface_for_tools(tools)
    return surface.definition_tokens + sum(
        estimate_text_tokens(instruction) for instruction in surface.tool_instructions
    )


def test_estimate_static_tokens_includes_tool_definitions() -> None:
    def search_docs(query: str, limit: int = 5) -> str:
        """Search the engineering docs for a matching answer."""
        return f"{query}:{limit}"

    def export_notes(title: str, include_metadata: bool = False) -> str:
        """Export the current working notes as markdown with full metadata attached."""
        return f"{title}:{include_metadata}"

    toolkit = Toolkit(
        name="docs",
        tools=[search_docs],
        instructions="Always cite the relevant document section when using search_docs.",
        add_instructions=True,
    )
    export_tool = Function(
        name="export_notes",
        entrypoint=export_notes,
    )
    agent_with_tools = _agent()
    agent_with_tools.role = "Engineer"
    agent_with_tools.instructions = ["Stay concise."]
    agent_with_tools.tools = [toolkit, export_tool]

    baseline_agent = _agent()
    baseline_agent.role = agent_with_tools.role
    baseline_agent.instructions = list(agent_with_tools.instructions)

    expected_export_tool = export_tool.model_copy(deep=True)
    expected_export_tool.process_entrypoint(strict=False)
    expected_payloads = [
        {
            "name": "search_docs",
            "description": "Search the engineering docs for a matching answer.",
            "parameters": Function.from_callable(search_docs).parameters,
        },
        {
            "name": "export_notes",
            "description": "Export the current working notes as markdown with full metadata attached.",
            "parameters": expected_export_tool.parameters,
        },
    ]
    tool_tokens = _tool_definition_tokens(agent_with_tools.tools)
    assert tool_tokens == (
        len(stable_serialize(expected_payloads)) // 4
        + estimate_text_tokens("Always cite the relevant document section when using search_docs.")
    )
    assert _tool_definition_tokens(baseline_agent.tools) == 0
    assert tool_tokens > 0


def test_static_token_estimator_cache_fields_are_not_constructor_inputs() -> None:
    assert "_non_prompt_tokens" not in inspect.signature(_StaticTokenEstimator).parameters


def test_estimate_agent_static_tokens_uses_real_system_message_builder() -> None:
    @dataclass
    class PromptAwareModel(FakeModel):
        def get_instructions_for_model(self, tools: list[Any] | None = None) -> list[str] | None:
            _ = tools
            return ["Follow provider guidance."]

        def get_system_message_for_model(self, tools: list[Any] | None = None) -> str | None:
            _ = tools
            return "Provider system message."

    agent = _agent(model=PromptAwareModel(id="fake-model", provider="fake"))
    agent.role = "Engineer"
    agent.instructions = ["Stay concise."]
    agent.markdown = True

    session = AgentSession(
        session_id="history-budget",
        agent_id=agent.id,
        user_id="history-budget-user",
    )
    run_context = RunContext(
        run_id="history-budget",
        session_id="history-budget",
        user_id="history-budget-user",
        session_state={},
    )
    system_message = agent.get_system_message(
        session=session,
        run_context=run_context,
        tools=None,
        add_session_state_to_context=False,
    )
    assert system_message is not None
    assert system_message.content is not None

    expected_tokens = estimate_text_tokens("Current prompt") + estimate_text_tokens(str(system_message.content))
    assert estimate_agent_static_tokens(agent, "Current prompt") == expected_tokens


def test_estimate_tool_definition_tokens_processes_functions_with_custom_parameters() -> None:
    def sync_calendar_event(title: str, include_attendees: bool = False) -> str:
        """Sync the current event draft into the shared calendar."""
        return f"{title}:{include_attendees}"

    custom_tool = Function(
        name="sync_calendar_event",
        entrypoint=sync_calendar_event,
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Calendar event title.",
                },
            },
            "required": ["title"],
        },
    )
    agent = _agent()
    agent.tools = [custom_tool]

    expected_tool = custom_tool.model_copy(deep=True)
    expected_tool.process_entrypoint(strict=False)

    assert expected_tool.description == "Sync the current event draft into the shared calendar."
    assert expected_tool.parameters["additionalProperties"] is False
    assert (
        _tool_definition_tokens(agent.tools)
        == len(
            stable_serialize(
                [
                    {
                        "name": "sync_calendar_event",
                        "description": expected_tool.description,
                        "parameters": expected_tool.parameters,
                    },
                ],
            ),
        )
        // 4
    )


def test_estimate_tool_definition_tokens_ignores_empty_toolkit() -> None:
    agent = _agent()
    agent.tools = [Toolkit(name="empty")]

    assert _tool_definition_tokens(agent.tools) == 0


def test_estimate_prompt_visible_history_tokens_never_mutates_session_messages() -> None:
    """The estimation path reads persisted messages without copying; pin that it never mutates them."""
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="use tools"),
                    Message(
                        role="assistant",
                        content="first tool",
                        tool_calls=[
                            {"id": "call-1", "type": "function", "function": {"name": "first", "arguments": "{}"}},
                        ],
                    ),
                    Message(role="tool", content="first result", tool_call_id="call-1"),
                    Message(
                        role="assistant",
                        content="second tool",
                        tool_calls=[
                            {"id": "call-2", "type": "function", "function": {"name": "second", "arguments": "{}"}},
                        ],
                    ),
                    Message(role="tool", content="second result", tool_call_id="call-2"),
                ],
            ),
        ],
    )
    original_snapshots = [message.model_dump() for run in session.runs or [] for message in run.messages or []]
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=1,
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    assert estimated_tokens > 0
    assert [message.model_dump() for run in session.runs or [] for message in run.messages or []] == original_snapshots


def test_estimate_prompt_visible_history_tokens_excludes_legacy_persisted_prompt_roles() -> None:
    conversation_messages = [
        Message(role="user", content="user request"),
        Message(role="assistant", content="assistant answer"),
        Message(role="tool", content="tool result"),
    ]
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
        system_message_role="instructions",
    )
    contaminated_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="system", content="legacy system prompt " * 20),
                    Message(role="developer", content="legacy developer prompt " * 20),
                    Message(role="instructions", content="legacy custom prompt " * 20),
                    *conversation_messages,
                ],
            ),
        ],
    )
    clean_session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=conversation_messages)],
    )

    assert estimate_prompt_visible_history_tokens(
        session=contaminated_session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    ) == estimate_prompt_visible_history_tokens(
        session=clean_session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )


def test_estimate_prompt_visible_history_tokens_uses_agno_message_limit_selection() -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="old user"),
                    Message(
                        role="assistant",
                        content="old assistant",
                        tool_calls=[
                            {"id": "call-1", "type": "function", "function": {"name": "tool", "arguments": "{}"}},
                        ],
                    ),
                    Message(role="tool", content="old tool"),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="new user"),
                    Message(role="assistant", content="new assistant"),
                ],
            ),
        ],
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="messages", limit=3),
        max_tool_calls_from_history=None,
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_messages = [
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == _estimate_history_messages_tokens(expected_messages)


def test_estimate_prompt_visible_history_tokens_counts_summary_after_compaction_removes_all_runs() -> None:
    session = _session(
        "session-1",
        summary=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="messages", limit=3),
        max_tool_calls_from_history=None,
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_wrapper = (
        "Here is a brief summary of your previous interactions:\n\n"
        "<summary_of_previous_interactions>\n"
        "merged summary\n"
        "</summary_of_previous_interactions>\n\n"
        "Note: this information is from previous interactions and may be outdated. "
        "You should ALWAYS prefer information from this conversation over the past summary.\n\n"
    )

    assert estimate_session_summary_tokens("merged summary") == estimate_text_tokens(expected_wrapper)
    assert estimated_tokens == estimate_text_tokens(expected_wrapper)
    assert estimated_tokens > 0


def test_estimate_session_summary_tokens_none() -> None:
    assert estimate_session_summary_tokens(None) == 0


def test_estimate_session_summary_tokens_empty() -> None:
    assert estimate_session_summary_tokens("") == 0
    assert estimate_session_summary_tokens("   ") == 0


def _docs_toolkit() -> Toolkit:
    def search_docs(query: str, limit: int = 5) -> str:
        """Search the engineering docs for a matching answer."""
        return f"{query}:{limit}"

    return Toolkit(
        name="docs",
        tools=[search_docs],
        instructions="Always cite the relevant document section when using search_docs.",
        add_instructions=True,
    )


def test_static_budgeting_and_metadata_reuse_one_tool_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget estimators and run-metadata payloads must share one prepared surface per agent."""
    agent = _agent()
    agent.tools = [_docs_toolkit()]

    get_tools_calls = 0
    original_get_tools = Agent.get_tools

    def _counting_get_tools(self: Agent, *args: object, **kwargs: object) -> list[object]:
        nonlocal get_tools_calls
        get_tools_calls += 1
        return original_get_tools(self, *args, **kwargs)

    monkeypatch.setattr(Agent, "get_tools", _counting_get_tools)

    estimator = agent_static_token_estimator(agent)
    first_estimate = estimator.estimate("Current prompt")
    re_estimate = estimate_agent_static_tokens(agent, "Current prompt")
    payloads = agent_tool_definition_payloads_for_logging(agent)

    assert get_tools_calls == 1
    assert first_estimate == re_estimate
    assert [payload["name"] for payload in payloads] == ["search_docs"]


def test_tool_definition_payloads_cached_equal_ordered_and_isolated() -> None:
    """Cached payloads must deeply equal fresh ones and resist mutation through returns."""

    def export_notes(title: str) -> str:
        """Export the current working notes."""
        return title

    def make_agent() -> Agent:
        agent = _agent()
        agent.tools = [_docs_toolkit(), Function(name="export_notes", entrypoint=export_notes)]
        return agent

    warm_agent = make_agent()
    first = agent_tool_definition_payloads_for_logging(warm_agent)
    cached = agent_tool_definition_payloads_for_logging(warm_agent)
    fresh = agent_tool_definition_payloads_for_logging(make_agent())

    assert first == cached == fresh
    assert [payload["name"] for payload in first] == ["search_docs", "export_notes"]

    first[0]["name"] = "mutated"
    cast("dict[str, object]", first[1]["parameters"])["injected"] = True

    assert agent_tool_definition_payloads_for_logging(warm_agent) == cached


def test_tool_surfaces_are_keyed_per_agent_instance() -> None:
    """Two live agents with different tools must not share a cached surface."""

    def other_tool(note: str) -> str:
        """Record one note."""
        return note

    docs_agent = _agent()
    docs_agent.tools = [_docs_toolkit()]
    notes_agent = _agent()
    notes_agent.tools = [Function(name="record_note", entrypoint=other_tool)]

    docs_payloads = agent_tool_definition_payloads_for_logging(docs_agent)
    notes_payloads = agent_tool_definition_payloads_for_logging(notes_agent)

    assert [payload["name"] for payload in docs_payloads] == ["search_docs"]
    assert [payload["name"] for payload in notes_payloads] == ["record_note"]


def test_tool_surface_cache_evicts_entries_when_agent_is_collected() -> None:
    """Surface cache entries must die with their agent instance."""
    agent = _agent()
    agent.tools = [_docs_toolkit()]
    agent_tool_definition_payloads_for_logging(agent)
    cache_key = id(agent)
    assert cache_key in _TOOL_SURFACE_CACHE

    del agent
    gc.collect()

    assert cache_key not in _TOOL_SURFACE_CACHE


def test_prompt_payloads_distinguish_strict_functions() -> None:
    """Function strictness must reach the shared schema cache key and the payload."""

    def sync_event(title: str, include_attendees: bool = False) -> str:
        """Sync one event."""
        return f"{title}:{include_attendees}"

    lax_surface = _prompt_tool_surface_for_tools(
        [Function(name="sync_event", entrypoint=sync_event, strict=False)],
    )
    strict_surface = _prompt_tool_surface_for_tools(
        [Function(name="sync_event", entrypoint=sync_event, strict=True)],
    )

    lax_parameters = cast("dict[str, object]", lax_surface.payloads[0]["parameters"])
    strict_parameters = cast("dict[str, object]", strict_surface.payloads[0]["parameters"])
    assert lax_parameters["required"] == ["title"]
    assert strict_parameters["required"] == ["title", "include_attendees"]


def test_concurrent_surface_preparation_stays_consistent() -> None:
    """Concurrent estimator and payload calls across agents must not corrupt the cache."""
    agents = []
    for _ in range(8):
        agent = _agent()
        agent.tools = [_docs_toolkit()]
        agents.append(agent)

    errors: list[BaseException] = []

    def _exercise(agent: Agent) -> None:
        try:
            for _ in range(10):
                payloads = agent_tool_definition_payloads_for_logging(agent)
                assert [payload["name"] for payload in payloads] == ["search_docs"]
                assert estimate_agent_static_tokens(agent, "Current prompt") > 0
        except BaseException as exc:
            errors.append(exc)

    threads = [Thread(target=_exercise, args=(agent,)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
