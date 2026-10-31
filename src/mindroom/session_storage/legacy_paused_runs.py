"""Paused runs earlier releases stored in shapes no reader reads any more, rewritten once at layout version 6.

A paused run resumes from its stored row: its system messages, its tool calls, and, for a team, the member runs its
delegation calls wait on. Earlier releases stored a paused notice as a run's content, Agno's summary block inside a
paused run's system message, and team delegation calls that do not name the member run they wait on.

``rewrite_paused_runs`` rewrites those rows once, in the write transaction that moves a database to layout version 6,
after the delegation snapshots' version 5, or that ends its Agno-layout conversion, so the resume, the team leader, and
a delegation's settlement read them as a current release's rows. Rows of every age are rewritten, so a paused run keeps
the approval window it paused with. The step keeps no backup: a release at layout version 4 or 5 opens a version-6
database as current, converting nothing, and reads each rewritten row as this one does. Worker code can write
conversation databases, so a row the rewrite cannot decode stays as stored, for its reader to reject as before.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message, is_own_model_message
from mindroom.llm.runs import RunStatus
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import decoded_mapping

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from mindroom.session_storage.schema import TableNames

# Every wording of an agent run's paused notice Agno's runtime wrote; from v2026.10.499 only the confirmation one.
_AGENT_PAUSED_NOTICES = frozenset(
    {
        "I have tools to execute, but I need confirmation.",
        "I have tools to execute, but I need confirmation, user input, or external execution.",
        "I have tools to execute, but I need confirmation or user input.",
        "I have tools to execute, but I need confirmation or external execution.",
        "I have tools to execute, but I need user input or external execution.",
        "I have tools to execute, but I need user input.",
        "I have tools to execute, but it needs external execution.",
    },
)
# A team run's paused notice: the bare one, or this header and one line per waiting call.
_TEAM_PAUSED_NOTICE = "Team run paused."
_TEAM_PAUSED_NOTICE_HEADER = "Team run paused. The following require input:\n"
# Agno's summary block, as its agent and team message builders rendered ``session.summary`` into the system message,
# which they then stripped, so a block that ended it ends with its note.
_SUMMARY_TAG = "<summary_of_previous_interactions>"
_SUMMARY_BLOCK = re.compile(
    r"Here is a brief summary of your previous interactions:\n\n<summary_of_previous_interactions>\n.*?\n"
    r"</summary_of_previous_interactions>\n\n"
    r"(?:Note: this information is from previous interactions[^\n]*(?:\n\n|\Z))?",
    re.DOTALL,
)
# The coordinate delegation call, and the result it holds while its member's run waits, as every release wrote them.
_COORDINATE_CALL = "delegate_task_to_member"
_WAITING_RESULT = re.compile(r"Member '.*' requires human input before continuing\.", re.DOTALL)


def rewrite_paused_runs(connection: sqlite3.Connection, names: TableNames) -> None:
    """Rewrite every legacy paused-run shape in the caller's write transaction; a current row stays as it is."""
    _drop_paused_notices(connection, names)
    _strip_summary_blocks(connection, names)
    _link_waiting_delegations(connection, names)


def _decoded_objects(texts: Iterable[object]) -> list[dict[str, Any]] | None:
    """Return each stored JSON object of one run's rows, or None when one of them does not decode to an object."""
    decoded: list[dict[str, Any]] = []
    for text in texts:
        value = decoded_mapping(text)
        if value is None:
            return None
        decoded.append(value)
    return decoded


# LEGACY_COMPAT: Runs that store a paused notice as their content.
# Legacy format: a run row whose ``content`` is the notice a run that paused without text of its own got: "I have tools
#   to execute, but I need confirmation." for an agent run and "Team run paused. The following require input:"
#   followed by one line per call waiting for confirmation for a team run, or another wording Agno's runtime wrote for
#   both. A paused run held it, and so did the run's later row when its resume wrote no text.
# Last legacy release: v2026.10.661, whose ``paused_content`` (``agent_loop/run.py``) and ``team_paused_content``
#   (``teams/runtime.py``) wrote it, as the Agno runtime did before them; replacement: v2026.10.662, whose paused runs
#   store only the text their model wrote.
# Handling: the content leaves the row, so neither a resume nor a delegation's settlement hands the notice to a team
#   leader or a delegating parent. A run whose own model answer reads like a notice wrote it itself, since a notice
#   stood in only for text the model never wrote, so it keeps its content; so does a run with a message that does not
#   decode, since its own answers cannot be read.
# Coverage: tests/session_storage/test_legacy_paused_runs.py::test_an_agent_row_that_stored_the_paused_notice_hands_over_none,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_team_row_and_its_member_row_lose_their_paused_notices,
#   tests/session_storage/test_legacy_paused_runs.py::test_every_other_wording_agno_wrote_leaves_the_content,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_reply_that_only_opens_like_a_notice_keeps_its_content,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_reply_the_model_wrote_that_matches_a_notice_keeps_its_content,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_requester_quoting_a_notice_leaves_the_content_without_it,
#   tests/session_storage/test_legacy_paused_runs.py::test_an_earlier_reply_the_input_carried_is_not_the_run_s_answer,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_row_the_rewrite_cannot_decode_stays_as_stored,
#   tests/test_legacy_delegation_snapshots.py::test_a_database_from_any_older_layout_takes_the_paused_run_rewrite_too,
#   tests/test_legacy_delegation_snapshots.py::test_a_database_at_version_5_takes_only_the_paused_run_rewrite
def _drop_paused_notices(connection: sqlite3.Connection, names: TableNames) -> None:
    runs, messages = quote_identifier(names.log_runs), quote_identifier(names.log_messages)
    candidates = connection.execute(
        f"SELECT run_id, record FROM {runs} WHERE instr(record, ?) OR instr(record, ?)",  # noqa: S608
        ("I have tools to execute, but ", _TEAM_PAUSED_NOTICE),
    ).fetchall()
    for run_id, raw_record in candidates:
        record = decoded_mapping(raw_record)
        if record is None or not _is_paused_notice(record.get("content")):
            continue
        rows = connection.execute(f"SELECT data FROM {messages} WHERE run_id = ?", (run_id,)).fetchall()  # noqa: S608
        answers = _own_answers(row for (row,) in rows)
        if answers is None or any(_is_paused_notice(answer) for answer in answers):
            continue
        del record["content"]
        connection.execute(f"UPDATE {runs} SET record = ? WHERE run_id = ?", (json.dumps(record), run_id))  # noqa: S608


def _is_paused_notice(text: object) -> bool:
    return isinstance(text, str) and (
        text in _AGENT_PAUSED_NOTICES or text == _TEAM_PAUSED_NOTICE or text.startswith(_TEAM_PAUSED_NOTICE_HEADER)
    )


def _own_answers(texts: Iterable[object]) -> list[str] | None:
    """Return the text of each answer a model wrote among a run's stored messages, or None when one does not decode."""
    stored = _decoded_objects(texts)
    if stored is None:
        return None
    try:
        messages = [Message.from_dict(data) for data in stored]
        return [message.get_content_string().strip() for message in messages if is_own_model_message(message)]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


# LEGACY_COMPAT: Paused runs whose saved system message embeds the session summary.
# Legacy format: a paused run's stored system message that Agno's message builder rendered with ``session.summary``
#   inside a ``<summary_of_previous_interactions>`` block, followed by its "Note: this information is from previous
#   interactions ..." line, when ``add_session_summary_to_context`` was on (configured agents, team leaders, and
#   authored subagents). Every Agno release MindRoom pinned, 2.4.7, 2.5.13, 2.6.12, 3.0.5, and 3.0.9, renders the
#   block in these words and strips the message, so a block that ends the message ends with its note.
# Last legacy release: v2026.10.346, whose agents (``persist_runtime_state``) and team leaders (``True``) kept
#   ``add_session_summary_to_context`` on; replacement: v2026.10.347, which renders the summary as a history message.
# Handling: the block and its note leave the system message, which then reads as Agno rendered it without a summary, so
#   the resume replays the scope's summary as a history message, as it does for a run a current release paused, and
#   the model reads it once; that one resumed request misses the provider's prompt cache. Any other message keeps
#   what it holds.
# Coverage: tests/session_storage/test_legacy_paused_runs.py::test_agno_s_summary_block_leaves_the_system_message_a_paused_run_stored,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_requester_s_message_holding_the_summary_block_stays_as_stored,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_row_the_rewrite_cannot_decode_stays_as_stored,
#   tests/test_history_summary_message.py::test_resuming_a_pre_release_team_pause_replays_the_scope_s_summary_once
def _strip_summary_blocks(connection: sqlite3.Connection, names: TableNames) -> None:
    messages = quote_identifier(names.log_messages)
    candidates = connection.execute(
        f"SELECT run_id, message_id, data FROM {messages} WHERE instr(data, ?)",  # noqa: S608
        (_SUMMARY_TAG,),
    ).fetchall()
    for run_id, message_id, raw_data in candidates:
        data = decoded_mapping(raw_data)
        if data is None or data.get("role") != "system":
            continue
        content = data.get("content")
        if not isinstance(content, str) or not _SUMMARY_BLOCK.search(content):
            continue
        # A block that ended the message leaves the separator before it, which Agno would have stripped.
        data["content"] = _SUMMARY_BLOCK.sub("", content).rstrip()
        connection.execute(
            f"UPDATE {messages} SET data = ? WHERE run_id = ? AND message_id = ?",  # noqa: S608
            (json.dumps(data), run_id, message_id),
        )


# LEGACY_COMPAT: Coordinate calls of a paused team run that do not name the member run they wait on.
# Legacy format: a paused team row's waiting ``delegate_task_to_member`` entry, one that still holds "Member '<name>'
#   requires human input before continuing.", whose ``child_run_id`` is null, or, in a row a streamed turn paused on
#   the Agno runtime, names the member run whose delegation ended last, which may be a sibling's or a finished one.
#   The two are one rule: one link the current reader requires, which one pass restores.
# Last legacy release: v2026.10.574 for the null link, whose team runs stored ``child_run_id`` null on every
#   delegation call, as blocking turns on the Agno runtime did; v2026.10.498, the last release on the Agno runtime,
#   for the last-ended link; replacement: v2026.10.575, which names the paused member's run on the call that waits on
#   it.
# Handling: each paused member run, in the order the team's resume runs them, its paused member rows in log order,
#   takes the waiting call to its member that names it, else the first such call not yet taken, which is the call the
#   earlier readers answered with it when every member finishes, so an approval saved before the upgrade answers the
#   same call with the member's result. A call that no longer waits keeps what it stored, and so does a team run whose
#   tool rows do not decode.
# Coverage: tests/session_storage/test_legacy_paused_runs.py::test_a_waiting_call_stored_without_its_member_run_names_it,
#   tests/session_storage/test_legacy_paused_runs.py::test_each_waiting_call_names_the_run_of_the_member_it_names,
#   tests/session_storage/test_legacy_paused_runs.py::test_two_waiting_calls_to_one_member_take_its_runs_in_the_order_they_resume,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_call_a_streamed_agno_turn_linked_to_the_run_that_ended_last_names_its_own,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_waiting_call_a_streamed_agno_turn_linked_to_a_sibling_s_run_takes_its_own,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_call_whose_member_finished_keeps_what_it_stored,
#   tests/session_storage/test_legacy_paused_runs.py::test_calls_that_name_their_member_runs_stay_as_stored,
#   tests/session_storage/test_legacy_paused_runs.py::test_a_row_the_rewrite_cannot_decode_stays_as_stored,
#   tests/teams/test_continue_team.py::test_an_approved_member_call_resumes_the_member_and_then_the_leader,
#   tests/teams/test_continue_team.py::test_a_row_a_streamed_agno_turn_paused_answers_each_call_with_its_own_member_s_result
def _link_waiting_delegations(connection: sqlite3.Connection, names: TableNames) -> None:
    runs, tools = quote_identifier(names.log_runs), quote_identifier(names.log_tools)
    paused_teams = connection.execute(
        f"SELECT run_id FROM {runs} WHERE kind = 'team' AND status = ? AND parent_run_id IS NULL",  # noqa: S608
        (RunStatus.paused.value,),
    ).fetchall()
    for (run_id,) in paused_teams:
        member_runs = [
            (scope_key.removeprefix("agent:"), member_run_id)
            for member_run_id, scope_key in connection.execute(
                f"SELECT run_id, scope_key FROM {runs} WHERE parent_run_id = ? AND status = ? ORDER BY seq",  # noqa: S608
                (run_id, RunStatus.paused.value),
            )
        ]
        rows = connection.execute(
            f"SELECT ord, data FROM {tools} WHERE run_id = ? ORDER BY ord",  # noqa: S608
            (run_id,),
        ).fetchall()
        calls = _decoded_objects(data for _, data in rows)
        if calls is None:
            continue
        waiting = [
            (position, call)
            for (position, _), call in zip(rows, calls, strict=True)
            if call.get("tool_name") == _COORDINATE_CALL
            and isinstance(call.get("result"), str)
            and _WAITING_RESULT.fullmatch(call["result"])
        ]
        for member_id, member_run_id in member_runs:
            mine = [entry for entry in waiting if _called_member(entry[1]) == member_id]
            linked = next((entry for entry in mine if entry[1].get("child_run_id") == member_run_id), None)
            taken = linked or next(iter(mine), None)
            if taken is None:
                continue
            waiting.remove(taken)
            position, call = taken
            if call.get("child_run_id") != member_run_id:
                call["child_run_id"] = member_run_id
                connection.execute(
                    f"UPDATE {tools} SET data = ? WHERE run_id = ? AND ord = ?",  # noqa: S608
                    (json.dumps(call), run_id, position),
                )


def _called_member(call: dict[str, Any]) -> object:
    arguments = call.get("tool_args")
    return arguments.get("member_id") if isinstance(arguments, dict) else None


__all__ = ["rewrite_paused_runs"]
