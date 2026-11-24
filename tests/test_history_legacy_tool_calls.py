"""Tool calls earlier releases stored in other shapes convert to the current shape, which every reader then reads.

Each run here is an Agno-layout row converted to storage v2 and read back, as replay, compaction, a resume, and the
skill review transcript read it.
"""
# ruff: noqa: D103

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS
from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request, scope_history_messages
from mindroom.history.summary_input import build_summary_input
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.schema import TableNames
from mindroom.skill_learning.transcript import conversation_messages
from tests.agent_loop.helpers import collect, context, gated, spec
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.fakes.token_counter import quarter_tokens
from tests.history_helpers import replay_settings, summary_tool_work
from tests.session_storage.agno_layout import SESSION_ID, converted_store

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore

_SCOPE = HistoryScope(kind="agent", scope_id="code")
_CALL = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
# An id-only placeholder streamed Chat history kept through v2026.7.172, stored with an orphan result.
_PLACEHOLDER = {"id": "call_0"}
# A completed Anthropic call that v2026.9.43 stored without its arguments field.
_BARE_CALL = {"id": "call-0", "type": "function", "function": {"name": "list_rooms"}}


def _converted(tmp_path: Path, *messages: dict[str, Any], status: str = "COMPLETED") -> ConversationStore:
    row = {"run_id": "run-1", "agent_id": "code", "status": status, "messages": list(messages)}
    return converted_store(TableNames("code").database_path(tmp_path), row)


def _stored_run(tmp_path: Path, *messages: dict[str, Any]) -> RunRecord:
    run = _converted(tmp_path, *messages).get_run("run-1")
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


def test_a_stored_call_without_arguments_replays_with_empty_arguments(tmp_path: Path) -> None:
    run = _stored_run(
        tmp_path,
        {"role": "user", "content": "Rooms?"},
        {"role": "assistant", "tool_calls": [_BARE_CALL]},
        {"role": "tool", "tool_call_id": "call-0", "content": "[]"},
    )
    stored = copy.deepcopy(run.messages)

    calls, results = _calls_and_results(_history(run))

    assert calls == [{**_BARE_CALL, "function": {"name": "list_rooms", "arguments": "{}"}}]
    assert results == ["call-0"]
    assert run.messages == stored


def test_placeholders_and_their_results_never_replay(tmp_path: Path) -> None:
    run = _stored_run(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    )

    assert _calls_and_results(_history(run)) == ([_CALL], ["call_1"])


def test_the_tool_call_window_counts_only_calls_that_replay(tmp_path: Path) -> None:
    run = _stored_run(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_CALL, _PLACEHOLDER]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
    )

    assert _calls_and_results(_history(run, max_tool_calls=1)) == ([_CALL], ["call_1"])


def test_the_message_window_counts_only_messages_that_replay(tmp_path: Path) -> None:
    run = _stored_run(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        {"role": "assistant", "content": "It is sunny."},
    )

    assert [message.role for message in _history(run, max_messages=4)] == ["user", "assistant", "tool", "assistant"]


def test_a_message_left_without_calls_or_content_takes_no_message_slot(tmp_path: Path) -> None:
    run = _stored_run(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_PLACEHOLDER]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        {"role": "assistant", "content": "It is sunny."},
    )

    history = _history(run, max_messages=2)

    assert [(message.role, message.content) for message in history] == [
        ("user", "Weather?"),
        ("assistant", "It is sunny."),
    ]


def test_a_message_left_with_content_but_no_calls_holds_none(tmp_path: Path) -> None:
    run = _stored_run(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [_PLACEHOLDER]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
    )

    _user, assistant = _history(run)

    assert (assistant.content, assistant.tool_calls) == ("Checking.", None)


def _compacted_run(tmp_path: Path) -> RunRecord:
    return _stored_run(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_BARE_CALL, _PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call-0", "content": "[]"},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
    )


def test_compaction_reads_stored_calls_as_a_request_replays_them(tmp_path: Path) -> None:
    settings = replay_settings(max_tool_calls_from_history=2)
    run = _compacted_run(tmp_path)
    stored = copy.deepcopy(run.messages)

    calls, results = summary_tool_work([run], settings)

    assert calls == [{**_BARE_CALL, "function": {"name": "list_rooms", "arguments": "{}"}}, _CALL]
    assert results == [("call-0", "[]"), ("call_1", "sunny")]
    assert run.messages == stored


def test_the_summary_input_leaves_out_placeholders_and_their_results(tmp_path: Path) -> None:
    settings = replay_settings()

    text, included = build_summary_input(
        previous_summary=None,
        compacted_runs=[_compacted_run(tmp_path)],
        max_input_tokens=100_000,
        history_settings=settings,
        token_estimator=quarter_tokens,
    )

    assert len(included) == 1
    assert "sunny" in text
    assert "orphan" not in text
    assert "call_0" not in text


def test_a_paused_run_resumes_without_its_placeholders(tmp_path: Path) -> None:
    store = _converted(
        tmp_path,
        {"role": "system", "content": "You are Code."},
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        status="PAUSED",
    )
    paused = store.get_run("run-1")
    assert paused is not None

    request = build_resume_request(paused=paused.messages, summary=None, history=[])

    assert _calls_and_results(request) == ([_CALL], [])
    assert request[0] is paused.messages[0]
    assert request[0].role == "system"


def test_a_paused_team_runs_embedded_members_are_repaired(tmp_path: Path) -> None:
    """The members a paused team run embedded, which its resume reads, convert repaired like every other run."""
    member = {
        "run_id": "member-1",
        "agent_id": "code",
        "status": "PAUSED",
        "messages": [
            {"role": "user", "content": "Weather?"},
            {"role": "assistant", "tool_calls": [_PLACEHOLDER, _BARE_CALL]},
            {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
            {"role": "tool", "tool_call_id": "call-0", "content": "[]"},
        ],
    }
    team = {"run_id": "team-1", "team_id": "squad", "status": "PAUSED", "messages": [], "member_responses": [member]}

    store = converted_store(TableNames("squad").database_path(tmp_path), team, kind="team", owner_id="squad")

    stored = store.get_run("member-1")
    assert stored is not None
    repaired = {**_BARE_CALL, "function": {"name": "list_rooms", "arguments": "{}"}}
    assert _calls_and_results(stored.messages) == ([repaired], ["call-0"])


def test_a_turns_transcript_reads_converted_calls_as_replay_does(tmp_path: Path) -> None:
    """The skill review transcript, like the other background readers, reads the calls in the shape replay sends."""
    store = _converted(
        tmp_path,
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "tool_calls": [_BARE_CALL, _PLACEHOLDER, _CALL]},
        {"role": "tool", "tool_call_id": "call-0", "content": "[]"},
        {"role": "tool", "tool_call_id": "call_0", "content": "orphan"},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    )
    session = store.load_session(SESSION_ID)
    assert session is not None

    said = [utterance for parts in conversation_messages(session) for utterance in parts]

    assert [utterance.unredacted for utterance in said if utterance.kind == "tool_result"] == ["[]", "sunny"]
    reply = next(utterance.message for utterance in said if utterance.kind == "reply")
    assert reply.tool_calls == [{**_BARE_CALL, "function": {"name": "list_rooms", "arguments": "{}"}}, _CALL]


@pytest.mark.asyncio
async def test_a_converted_paused_run_resumes_and_is_stored_in_the_current_shape(tmp_path: Path) -> None:
    """A run paused by a release that stored legacy calls converts repaired, so its resume sends and stores that shape.

    The paused run also holds a completed call that lacked its arguments, which the conversion supplies as ``{}``.
    """

    def run_shell_command(args: list[str]) -> str:
        """Run one command.

        Args:
            args: The command and its arguments.

        """
        return " ".join(args)

    model = ScriptedModel([calls(tool_call("run_shell_command", "call-1", args=["echo", "hi"])), answer("Done.")])
    agent = spec(model, gated(run_shell_command))
    _events, paused = await collect(
        run_agent(
            agent,
            [Message(role="user", content="Run it")],
            ctx=context(),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    assert paused.status is RunStatus.paused
    call_message = next(message for message in paused.messages if message.tool_calls)
    call_message.tool_calls = [_PLACEHOLDER, _BARE_CALL, *(call_message.tool_calls or [])]
    paused.messages.extend(
        [
            Message(role="tool", tool_call_id="call_0", content="orphan"),
            Message(role="tool", tool_call_id="call-0", content="[]"),
        ],
    )
    converted = converted_store(TableNames("code").database_path(tmp_path), paused.to_dict()).get_run(paused.run_id)
    assert isinstance(converted, RunRecord)
    approved = ApprovalCall(
        tool_call_id="call-1",
        tool_name="run_shell_command",
        invoking_agent="code",
        expires_at_ns=2**62,
        toolkit_name="gated",
        arguments_digest=approval_arguments_digest({"args": ["echo", "hi"]}),
    )

    _events, record = await collect(
        resume_agent(
            agent,
            converted,
            build_resume_request(paused=converted.messages, summary=None, history=[]),
            approved=[approved],
            denied={},
            external_results={},
            context=lambda run_id: context(run_id=run_id),
            hooks=NO_HOOKS,
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )

    assert record.status is RunStatus.completed
    stored_calls, stored_results = _calls_and_results(record.messages)
    assert [call["id"] for call in stored_calls] == ["call-0", "call-1"]
    assert stored_calls[0]["function"] == {"name": "list_rooms", "arguments": "{}"}
    assert stored_results == ["call-0", "call-1"]
