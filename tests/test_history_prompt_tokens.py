"""Tests for static prompt-token and history token estimation."""
# ruff: noqa: D103

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from mindroom.history.prompt_tokens import _StaticTokenEstimator, build_static_token_estimator
from mindroom.history.replay import (
    compaction_summary_message,
    estimate_prompt_visible_history_tokens,
    estimate_request_messages_tokens,
    estimate_summary_tokens,
)
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    ResolvedHistorySettings,
    ScopeSummary,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ToolSpec
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from tests.history_helpers import _completed_run, _session

_SYSTEM_MESSAGE = Message(role="system", content="You are a careful engineer.\n\nStay concise.")
_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["query"],
}
_EXPORT_PARAMETERS = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
_SEARCH_DOCS = ToolSpec(
    name="search_docs",
    description="Search the engineering docs for a matching answer.",
    parameters=_SEARCH_PARAMETERS,
)
_EXPORT_NOTES = ToolSpec(
    name="export_notes",
    description="Export the current working notes as markdown.",
    parameters=_EXPORT_PARAMETERS,
    strict=True,
    requires_confirmation=True,
)


def test_static_estimate_counts_the_system_message_and_the_prompt() -> None:
    estimator = build_static_token_estimator(_SYSTEM_MESSAGE, (), frozenset())

    assert estimator.non_prompt_tokens() == estimate_text_tokens(str(_SYSTEM_MESSAGE.content))
    assert estimator.estimate("Current prompt") == estimate_text_tokens("Current prompt") + estimate_text_tokens(
        str(_SYSTEM_MESSAGE.content),
    )


def test_static_estimate_counts_each_offered_tool_definition_as_the_request_sends_it() -> None:
    """Tool definitions count with every field the tool set; fields it left unset are not sent, so they do not count."""
    estimator = build_static_token_estimator(_SYSTEM_MESSAGE, (_SEARCH_DOCS, _EXPORT_NOTES), frozenset())

    sent_definitions = [
        {
            "name": "search_docs",
            "description": "Search the engineering docs for a matching answer.",
            "parameters": _SEARCH_PARAMETERS,
        },
        {
            "name": "export_notes",
            "description": "Export the current working notes as markdown.",
            "parameters": _EXPORT_PARAMETERS,
            "strict": True,
            "requires_confirmation": True,
        },
    ]
    assert estimator.non_prompt_tokens() == (
        estimate_text_tokens(str(_SYSTEM_MESSAGE.content)) + len(stable_serialize(sent_definitions)) // 4
    )


def test_static_estimate_leaves_deferred_tools_out() -> None:
    """A tool that tool search keeps off the request until it is found costs nothing up front."""
    tools = (_SEARCH_DOCS, _EXPORT_NOTES)
    nothing_deferred = build_static_token_estimator(_SYSTEM_MESSAGE, tools, frozenset())
    export_deferred = build_static_token_estimator(_SYSTEM_MESSAGE, tools, frozenset({"export_notes"}))
    all_deferred = build_static_token_estimator(_SYSTEM_MESSAGE, tools, frozenset({"search_docs", "export_notes"}))

    assert export_deferred.non_prompt_tokens() < nothing_deferred.non_prompt_tokens()
    assert export_deferred.estimate("Current prompt") == build_static_token_estimator(
        _SYSTEM_MESSAGE,
        (_SEARCH_DOCS,),
        frozenset(),
    ).estimate("Current prompt")
    assert all_deferred.non_prompt_tokens() == estimate_text_tokens(str(_SYSTEM_MESSAGE.content))


def test_static_token_estimator_cache_fields_are_not_constructor_inputs() -> None:
    assert "_non_prompt_tokens" not in inspect.signature(_StaticTokenEstimator).parameters


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
    original_snapshots = [message.to_dict() for run in session.runs for message in run.messages]
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
    assert [message.to_dict() for run in session.runs for message in run.messages] == original_snapshots


def test_estimate_prompt_visible_history_tokens_excludes_legacy_persisted_prompt_roles() -> None:
    conversation_messages = [
        Message(role="user", content="user request"),
        Message(role="assistant", content="assistant answer"),
        Message(role="tool", content="tool result"),
    ]
    history_settings = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
    contaminated_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="system", content="legacy system prompt " * 20),
                    Message(role="developer", content="legacy developer prompt " * 20),
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
    assert estimated_tokens == estimate_request_messages_tokens(expected_messages, replay_model=None)


def test_estimate_prompt_visible_history_tokens_counts_summary_after_compaction_removes_all_runs() -> None:
    session = _session(
        "session-1",
        summary=ScopeSummary(text="merged summary", updated_at=datetime.now(UTC)),
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

    expected_wrapper = str(compaction_summary_message("merged summary", run_local=False).content)

    assert estimate_summary_tokens(session) == estimate_text_tokens(expected_wrapper)
    assert estimated_tokens == estimate_text_tokens(expected_wrapper)
    assert estimated_tokens > 0


def test_summary_tokens_without_a_summary_are_zero() -> None:
    assert estimate_summary_tokens(_session("session-1")) == 0
    for text in ("", "   "):
        assert (
            estimate_summary_tokens(
                _session("session-1", summary=ScopeSummary(text=text, updated_at=datetime.now(UTC))),
            )
            == 0
        )
