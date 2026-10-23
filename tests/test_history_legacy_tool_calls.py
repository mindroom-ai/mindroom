"""Stored tool calls in the shapes earlier releases wrote replay in the current shape, from history and paused runs."""
# ruff: noqa: D103

from __future__ import annotations

import copy
import json
from typing import Any

from mindroom.history.request_messages import build_resume_request, scope_history_messages
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.session_storage.codec import run_from_row

_SCOPE = HistoryScope(kind="agent", scope_id="code")
_CALL = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
# An id-only placeholder streamed Chat history kept through v2026.7.172, stored with an orphan result.
_PLACEHOLDER = {"id": "call_0"}


def _stored_run(*messages: dict[str, Any]) -> RunRecord:
    row = {"run_id": "run-1", "agent_id": "code", "status": "COMPLETED", "messages": list(messages)}
    run = run_from_row("agent", json.dumps(row))
    assert isinstance(run, RunRecord)
    return run


def _history(
    run: RunRecord,
    *,
    max_messages: int | None = None,
    max_tool_calls: int | None = None,
) -> list[Message]:
    return scope_history_messages(
        [run],
        scope=_SCOPE,
        num_history_runs=None,
        num_history_messages=max_messages,
        max_tool_calls_from_history=max_tool_calls,
    )


def _calls_and_results(messages: list[Message]) -> tuple[list[dict[str, Any]], list[str | None]]:
    calls = [call for message in messages for call in message.tool_calls or ()]
    return calls, [message.tool_call_id for message in messages if message.role == "tool"]


def test_a_stored_call_without_arguments_replays_with_empty_arguments() -> None:
    bare = {"id": "call_1", "type": "function", "function": {"name": "list_rooms"}}
    run = _stored_run(
        {"role": "user", "content": "Rooms?"},
        {"role": "assistant", "tool_calls": [bare]},
        {"role": "tool", "tool_call_id": "call_1", "content": "[]"},
    )
    stored = copy.deepcopy(run.messages)

    calls, results = _calls_and_results(_history(run))

    assert calls == [{**bare, "function": {"name": "list_rooms", "arguments": "{}"}}]
    assert results == ["call_1"]
    assert run.messages == stored


def test_placeholders_and_their_results_never_replay() -> None:
    run = _stored_run(
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    )

    assert _calls_and_results(_history(run)) == ([_CALL], ["call_1"])


def test_the_tool_call_window_counts_only_calls_that_replay() -> None:
    run = _stored_run(
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_CALL, _PLACEHOLDER]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
    )

    assert _calls_and_results(_history(run, max_tool_calls=1)) == ([_CALL], ["call_1"])


def test_the_message_window_counts_only_messages_that_replay() -> None:
    run = _stored_run(
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        {"role": "assistant", "content": "It is sunny."},
    )

    assert [message.role for message in _history(run, max_messages=4)] == ["user", "assistant", "tool", "assistant"]


def test_a_paused_run_resumes_without_its_placeholders() -> None:
    paused = [
        Message(role="system", content="You are Code."),
        Message(role="user", content="Weather?"),
        Message(role="assistant", tool_calls=[_PLACEHOLDER, _CALL]),
        Message(role="tool", tool_call_id="call_0", content="orphan"),
    ]

    request = build_resume_request(paused=paused, summary=None, history=[])

    assert _calls_and_results(request) == ([_CALL], [])
    assert request[0] is paused[0]
