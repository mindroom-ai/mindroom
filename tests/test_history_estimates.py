"""Tests for history token estimation as a route that sends messages by role sizes it."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.history.replay import (
    estimate_prompt_visible_history_tokens,
    estimate_summary_tokens,
)
from mindroom.history.summary_message import compaction_summary_message
from mindroom.history.types import (
    HistoryPolicy,
    ScopeSummary,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.session_storage.schema import TableNames
from tests.fakes.scripted_model import ScriptedModel
from tests.fakes.token_counter import quarter_tokens
from tests.history_helpers import _completed_run, _session, replay_settings
from tests.session_storage.agno_layout import SESSION_ID, converted_store

if TYPE_CHECKING:
    from pathlib import Path


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
        model=ScriptedModel([]),
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
        model=ScriptedModel([]),
    ) == estimate_prompt_visible_history_tokens(
        session=clean_session,
        history_settings=history_settings,
        model=ScriptedModel([]),
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
        model=ScriptedModel([]),
    )

    expected_messages = [
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == ScriptedModel([]).request_tokens(ModelRequest(messages=expected_messages))


def test_estimate_prompt_visible_history_tokens_counts_summary_after_compaction_removes_all_runs() -> None:
    session = _session(
        "session-1",
        summary=ScopeSummary(text="merged summary", updated_at=datetime.now(UTC)),
    )
    history_settings = replay_settings(HistoryPolicy(mode="messages", limit=3))

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=history_settings,
        model=ScriptedModel([]),
    )

    expected_wrapper = str(compaction_summary_message("merged summary", run_local=False).content)

    assert estimate_summary_tokens(session, ScriptedModel([])) == quarter_tokens(expected_wrapper)
    assert estimated_tokens == quarter_tokens(expected_wrapper)
    assert estimated_tokens > 0


def test_summary_tokens_without_a_summary_are_zero() -> None:
    assert estimate_summary_tokens(_session("session-1"), ScriptedModel([])) == 0
    for text in ("", "   "):
        assert (
            estimate_summary_tokens(
                _session("session-1", summary=ScopeSummary(text=text, updated_at=datetime.now(UTC))),
                ScriptedModel([]),
            )
            == 0
        )
