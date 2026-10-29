"""Paused runs earlier releases stored in shapes no reader reads any more, rewritten once at layout version 6.

A paused run resumes from its stored row: its system messages, its tool calls, and, for a team, the member runs its
delegation calls wait on. Earlier releases stored a paused notice as a run's content, Agno's summary block inside a
paused run's system message, and team delegation calls that do not name the member run they wait on. Moving a
database to layout version 6 rewrites those rows once, so the resume, the team leader, and a delegation's settlement
read them as a current release's rows.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message, is_own_model_message
from mindroom.llm.runs import RunStatus
from mindroom.session_storage.schema import quote_identifier

if TYPE_CHECKING:
    import sqlite3

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
    r"</summary_of_previous_interactions>\n\n(?:Note: this information is from previous interactions[^\n]*(?:\n\n|\Z))?",
    re.DOTALL,
)
# The coordinate delegation call, and the result it holds while its member's run waits, as every release wrote them.
_COORDINATE_CALL = "delegate_task_to_member"
_WAITING_RESULT = re.compile(r"Member '.*' requires human input before continuing\.", re.DOTALL)


# LEGACY_COMPAT: Paused runs stored with a paused notice, Agno's summary block, or unlinked delegation calls.
# Legacy format: rows of a database below layout version 6, or rows the Agno-layout conversion just copied, holding:
#   a run ``content`` that is the notice a run paused without text of its own got, "I have tools to execute, but I
#   need confirmation." for an agent and "Team run paused. The following require input:" with one line per waiting
#   call for a team, or another wording Agno's runtime wrote, on the paused row and on its later row when the resume
#   wrote no text; a paused run's stored system message with Agno's ``<summary_of_previous_interactions>`` block; a
#   paused team run's waiting ``delegate_task_to_member`` entry whose ``child_run_id`` is null, or names the member run
#   whose delegation ended last in a streamed turn; and a paused run without the ``add_to_agent_memory: false``
#   messages its paused request sent, such as memory results and the approval receipt.
# Last legacy release: the notice v2026.10.661 (``paused_content`` in ``agent_loop/run.py``, ``team_paused_content`` in
#   ``teams/runtime.py``); the summary block v2026.10.346, whose agents and team leaders kept Agno's
#   ``add_session_summary_to_context``; the null link v2026.10.574; the last-ended link v2026.10.498, the last
#   release on the Agno runtime; the missing messages v2026.10.611. Each was replaced by the next release.
# Handling: once, in the write transaction that moves a database to layout version 6, after the delegation
#   snapshots' version 5, or ends its Agno-layout conversion. A notice none of the run's own model messages wrote
#   leaves its content, so neither a resume nor a delegation's settlement hands it to a team leader or a delegating
#   parent. The summary block leaves the system message, so the resume replays the scope's summary as a history
#   message, as it does for a run a current release paused, and the model reads it once. Each paused member run, in the order the team's resume runs them, gets the
#   waiting call to its member that names it, else the first such call not yet taken, which is the call the earlier
#   readers answered with it when every member finishes. Nothing restores the missing messages, so such a run resumes
#   without them. Rows of every age are rewritten, so a paused run keeps the approval window it paused with. The step
#   keeps no backup: a release at layout version 4 or 5 opens a version-6 database as current, converting nothing,
#   and reads each rewritten row as this one does.
# Coverage: tests/session_storage/test_legacy_paused_runs.py (every test),
#   tests/teams/test_continue_team.py::test_an_approved_member_call_resumes_the_member_and_then_the_leader,
#   tests/teams/test_continue_team.py::test_a_row_a_streamed_agno_turn_paused_answers_each_call_with_its_own_member_s_result,
#   tests/test_history_summary_message.py::test_resuming_a_pre_release_team_pause_replays_the_scope_s_summary_once,
#   tests/test_history_request_messages.py::test_a_run_paused_without_its_turn_context_resumes_without_it,
#   tests/test_legacy_delegation_snapshots.py::test_a_database_from_any_older_layout_takes_the_paused_run_rewrite_too,
#   tests/test_legacy_delegation_snapshots.py::test_a_database_at_version_5_takes_only_the_paused_run_rewrite,
#   tests/test_legacy_delegation_snapshots.py::test_the_startup_sweep_takes_a_team_databases_owner_from_its_meta_row.
def rewrite_paused_runs(connection: sqlite3.Connection, names: TableNames) -> None:
    """Rewrite every legacy paused-run shape in the caller's write transaction; a current row stays as it is."""
    _drop_paused_notices(connection, names)
    _strip_summary_blocks(connection, names)
    _link_waiting_delegations(connection, names)


def _drop_paused_notices(connection: sqlite3.Connection, names: TableNames) -> None:
    runs, messages = quote_identifier(names.log_runs), quote_identifier(names.log_messages)
    candidates = connection.execute(
        f"SELECT run_id, record FROM {runs} WHERE instr(record, ?) OR instr(record, ?)",  # noqa: S608
        ("I have tools to execute, but ", _TEAM_PAUSED_NOTICE),
    ).fetchall()
    for run_id, raw_record in candidates:
        record = json.loads(raw_record)
        if not _is_paused_notice(record.get("content")):
            continue
        rows = connection.execute(f"SELECT data FROM {messages} WHERE run_id = ?", (run_id,)).fetchall()  # noqa: S608
        stored = [Message.from_dict(json.loads(data)) for (data,) in rows]
        # A notice stood in only for text the model never wrote, so a run whose own answer reads like one wrote it.
        if any(
            is_own_model_message(message) and _is_paused_notice(message.get_content_string().strip())
            for message in stored
        ):
            continue
        del record["content"]
        connection.execute(f"UPDATE {runs} SET record = ? WHERE run_id = ?", (json.dumps(record), run_id))  # noqa: S608


def _is_paused_notice(text: object) -> bool:
    return isinstance(text, str) and (
        text in _AGENT_PAUSED_NOTICES or text == _TEAM_PAUSED_NOTICE or text.startswith(_TEAM_PAUSED_NOTICE_HEADER)
    )


def _strip_summary_blocks(connection: sqlite3.Connection, names: TableNames) -> None:
    messages = quote_identifier(names.log_messages)
    candidates = connection.execute(
        f"SELECT run_id, message_id, data FROM {messages} WHERE instr(data, ?)",  # noqa: S608
        (_SUMMARY_TAG,),
    ).fetchall()
    for run_id, message_id, raw_data in candidates:
        data = json.loads(raw_data)
        content = data.get("content")
        if data.get("role") != "system" or not isinstance(content, str) or not _SUMMARY_BLOCK.search(content):
            continue
        # A block that ended the message leaves the separator before it, which Agno would have stripped.
        data["content"] = _SUMMARY_BLOCK.sub("", content).rstrip()
        connection.execute(
            f"UPDATE {messages} SET data = ? WHERE run_id = ? AND message_id = ?",  # noqa: S608
            (json.dumps(data), run_id, message_id),
        )


def _link_waiting_delegations(connection: sqlite3.Connection, names: TableNames) -> None:
    runs, tools = quote_identifier(names.log_runs), quote_identifier(names.log_tools)
    paused_teams = connection.execute(
        f"SELECT run_id, requirements FROM {runs} "  # noqa: S608
        "WHERE kind = 'team' AND status = ? AND parent_run_id IS NULL AND requirements IS NOT NULL",
        (RunStatus.paused.value,),
    ).fetchall()
    for run_id, raw_requirements in paused_teams:
        # The team's resume runs one plan per paused member run, in the order its requirements first name them.
        member_runs = dict.fromkeys(
            (requirement["member_agent_id"], requirement["member_run_id"])
            for requirement in json.loads(raw_requirements)
            if requirement.get("member_agent_id") is not None and requirement.get("member_run_id")
        )
        waiting: list[tuple[int, dict[str, Any]]] = []
        for position, data in connection.execute(
            f"SELECT ord, data FROM {tools} WHERE run_id = ? ORDER BY ord",  # noqa: S608
            (run_id,),
        ).fetchall():
            call = json.loads(data)
            if call.get("tool_name") == _COORDINATE_CALL and _WAITING_RESULT.fullmatch(call.get("result") or ""):
                waiting.append((position, call))
        for member_id, member_run_id in member_runs:
            calls = [entry for entry in waiting if (entry[1].get("tool_args") or {}).get("member_id") == member_id]
            linked = next((entry for entry in calls if entry[1].get("child_run_id") == member_run_id), None)
            taken = linked or next(iter(calls), None)
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


__all__ = ["rewrite_paused_runs"]
