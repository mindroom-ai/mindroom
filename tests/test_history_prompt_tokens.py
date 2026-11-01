"""Tests for static prompt-token and history token estimation."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.history.prompt_tokens import build_static_token_estimator
from mindroom.history.replay import (
    estimate_prompt_visible_history_tokens,
    estimate_request_messages_tokens,
    estimate_summary_tokens,
)
from mindroom.history.summary_message import compaction_summary_message
from mindroom.history.types import (
    HistoryPolicy,
    ScopeSummary,
)
from mindroom.llm.messages import Message, stable_serialize
from mindroom.llm.provider import ToolSpec
from mindroom.session_storage.schema import TableNames
from tests.fakes.token_counter import fake_token_counter, quarter_tokens
from tests.history_helpers import _completed_run, _session, replay_settings
from tests.session_storage.agno_layout import SESSION_ID, converted_store

if TYPE_CHECKING:
    from pathlib import Path


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
    strict=None,
)
_EXPORT_NOTES = ToolSpec(
    name="export_notes",
    description="Export the current working notes as markdown.",
    parameters=_EXPORT_PARAMETERS,
    strict=True,
)


def test_static_estimate_counts_the_system_message_and_the_prompt() -> None:
    estimator = build_static_token_estimator(fake_token_counter(), (_SYSTEM_MESSAGE,), (), frozenset())

    assert estimator.non_prompt_tokens == quarter_tokens(str(_SYSTEM_MESSAGE.content))
    assert estimator.estimate("Current prompt") == quarter_tokens("Current prompt") + quarter_tokens(
        str(_SYSTEM_MESSAGE.content),
    )


def test_static_estimate_counts_each_offered_tool_definition_as_the_request_sends_it() -> None:
    """Tool definitions count with every field the tool set; fields it left unset are not sent, so they do not count."""
    estimator = build_static_token_estimator(
        fake_token_counter(),
        (_SYSTEM_MESSAGE,),
        (_SEARCH_DOCS, _EXPORT_NOTES),
        frozenset(),
    )

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
        },
    ]
    assert estimator.non_prompt_tokens == (
        quarter_tokens(str(_SYSTEM_MESSAGE.content)) + quarter_tokens(stable_serialize(sent_definitions))
    )


def test_static_estimate_leaves_deferred_tools_out() -> None:
    """A tool that tool search keeps off the request until it is found costs nothing up front."""
    counter = fake_token_counter()
    tools = (_SEARCH_DOCS, _EXPORT_NOTES)
    nothing_deferred = build_static_token_estimator(counter, (_SYSTEM_MESSAGE,), tools, frozenset())
    export_deferred = build_static_token_estimator(counter, (_SYSTEM_MESSAGE,), tools, frozenset({"export_notes"}))
    all_deferred = build_static_token_estimator(
        counter,
        (_SYSTEM_MESSAGE,),
        tools,
        frozenset({"search_docs", "export_notes"}),
    )

    assert export_deferred.non_prompt_tokens < nothing_deferred.non_prompt_tokens
    assert export_deferred.estimate("Current prompt") == build_static_token_estimator(
        counter,
        (_SYSTEM_MESSAGE,),
        (_SEARCH_DOCS,),
        frozenset(),
    ).estimate("Current prompt")
    assert all_deferred.non_prompt_tokens == quarter_tokens(str(_SYSTEM_MESSAGE.content))


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
    history_settings = replay_settings(max_tool_calls_from_history=1)

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )

    assert estimated_tokens > 0
    assert [message.to_dict() for run in session.runs for message in run.messages] == original_snapshots


def test_estimate_prompt_visible_history_tokens_excludes_legacy_persisted_prompt_roles(tmp_path: Path) -> None:
    """A run an earlier release stored with its system prompt converts without it, so no estimate counts it."""
    conversation_messages = [
        Message(role="user", content="user request"),
        Message(role="assistant", content="assistant answer"),
        Message(role="tool", content="tool result"),
    ]
    history_settings = replay_settings()
    stored = _completed_run(
        "run-1",
        messages=[Message(role="system", content="legacy system prompt " * 20), *conversation_messages],
    ).to_dict()
    store = converted_store(TableNames("test_agent").database_path(tmp_path), stored, owner_id="test_agent")
    contaminated_session = store.load_session(SESSION_ID)
    assert contaminated_session is not None
    clean_session = _session(
        SESSION_ID,
        runs=[_completed_run("run-1", messages=conversation_messages)],
    )

    assert estimate_prompt_visible_history_tokens(
        session=contaminated_session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    ) == estimate_prompt_visible_history_tokens(
        session=clean_session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )


def test_estimate_prompt_visible_history_tokens_drops_a_tool_result_whose_call_the_message_limit_cut() -> None:
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
    history_settings = replay_settings(HistoryPolicy(mode="messages", limit=3))

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )

    expected_messages = [
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == estimate_request_messages_tokens(expected_messages, counter=fake_token_counter())


def test_estimate_prompt_visible_history_tokens_counts_summary_after_compaction_removes_all_runs() -> None:
    session = _session(
        "session-1",
        summary=ScopeSummary(text="merged summary", updated_at=datetime.now(UTC)),
    )
    history_settings = replay_settings(HistoryPolicy(mode="messages", limit=3))

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )

    expected_wrapper = str(compaction_summary_message("merged summary", run_local=False).content)

    assert estimate_summary_tokens(session, fake_token_counter()) == quarter_tokens(expected_wrapper)
    assert estimated_tokens == quarter_tokens(expected_wrapper)
    assert estimated_tokens > 0


def test_summary_tokens_without_a_summary_are_zero() -> None:
    assert estimate_summary_tokens(_session("session-1"), fake_token_counter()) == 0
    for text in ("", "   "):
        assert (
            estimate_summary_tokens(
                _session("session-1", summary=ScopeSummary(text=text, updated_at=datetime.now(UTC))),
                fake_token_counter(),
            )
            == 0
        )
