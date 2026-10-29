"""Paused runs earlier releases stored are rewritten once, when their database moves to layout version 6.

Each test writes rows as an earlier release's store left them into a database at layout version 4, opens it as the
next store open does, and checks what the readers see: a paused notice no longer reads as the run's content, Agno's
summary block no longer sits in a paused run's system message, and each waiting delegation call names the member run
it waits on.

``v2026.10.627_rows.json`` holds run dicts v2026.10.627's agent loop and team runtime stored, unchanged; v2026.10.574's
team runtime stored the same team row without the member run on its delegation call. The system messages in
``agno_3_0_9_system_messages.json`` are the ones Agno 3.0.9's message builders rendered, the version v2026.10.346 ran,
for an agent and a team leader with and without a session summary.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.request_messages import build_resume_request
from mindroom.history.summary_message import is_scope_summary
from mindroom.history.types import HistoryScope, ScopeSummary
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.session_storage.agent_storage import create_agent_state_storage, create_state_storage
from mindroom.session_storage.schema import LOG_VERSION, TableNames, layout_version
from tests.session_storage.layout_4 import store_rows, upgrade_from_layout_4

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_ROWS: dict[str, dict[str, Any]] = json.loads(
    (_FIXTURES / "legacy_run_content" / "v2026.10.627_rows.json").read_text(),
)
_SYSTEM_MESSAGES: dict[str, dict[str, str]] = json.loads(
    (_FIXTURES / "legacy_paused_runs" / "agno_3_0_9_system_messages.json").read_text(),
)["system_messages"]
_AGENT = HistoryScope(kind="agent", scope_id="code")
_SQUAD = HistoryScope(kind="team", scope_id="squad")
_SESSION = "session-1"
_NOTICE = "I have tools to execute, but I need confirmation."


def _agent_store(tmp_path: Path) -> ConversationStore:
    return create_agent_state_storage(_AGENT.scope_id, tmp_path)


def _team_store(tmp_path: Path) -> ConversationStore:
    return create_state_storage(TableNames.of_team(_SQUAD).storage_name, tmp_path, _SQUAD)


def _upgraded_run(tmp_path: Path, row: dict[str, Any]) -> RunRecord | TeamRunRecord:
    """Store one run dict at layout version 4, upgrade its database, and read the run back."""
    team = "team_id" in row
    store = _team_store(tmp_path) if team else _agent_store(tmp_path)
    store_rows(store, _SESSION, [row])
    upgrade_from_layout_4(store, _SQUAD if team else _AGENT)
    run = store.get_run(row["run_id"])
    assert run is not None
    return run


def _without_ids(messages: list[Message]) -> list[Message]:
    """Return ``messages`` without their ids, which a rendered summary message draws anew for every request."""
    return [replace(message, id="") for message in messages]


def _dump(store: ConversationStore) -> list[str]:
    with closing(sqlite3.connect(store.path)) as connection:
        return list(connection.iterdump())


@pytest.mark.parametrize("name", ["agent_paused", "agent_resumed_without_text"])
def test_an_agent_row_that_stored_the_paused_notice_hands_over_none(name: str, tmp_path: Path) -> None:
    """A paused row, and the row of a resume that wrote no text, keep their state but read with no content."""
    stored = _ROWS[name]
    assert stored["content"] == _NOTICE

    run = _upgraded_run(tmp_path, stored)

    assert (run.status, run.content, run.handover_content) == (RunStatus(stored["status"]), None, None)
    assert "content" not in run.to_dict()
    assert run.requirements == RunRecord.from_dict(stored).requirements


def test_a_team_row_and_its_member_row_lose_their_paused_notices(tmp_path: Path) -> None:
    """A team paused on its member's confirmation reads with no content, and so does its member's row."""
    team, member = _ROWS["team_paused"], _ROWS["member_paused"]
    assert str(team["content"]).startswith("Team run paused. The following require input:\n")
    store = _team_store(tmp_path)
    store_rows(store, team["session_id"], [team, member])

    upgrade_from_layout_4(store, _SQUAD)

    run = store.get_run(team["run_id"])
    assert isinstance(run, TeamRunRecord)
    assert (run.status, run.content) == (RunStatus.paused, None)
    [paused_member] = run.member_responses
    assert (paused_member.run_id, paused_member.content) == (member["run_id"], None)


@pytest.mark.parametrize(
    "notice",
    [
        "I have tools to execute, but I need confirmation, user input, or external execution.",
        "I have tools to execute, but I need confirmation or user input.",
        "I have tools to execute, but I need confirmation or external execution.",
        "I have tools to execute, but I need user input or external execution.",
        "I have tools to execute, but I need user input.",
        "I have tools to execute, but it needs external execution.",
        "Team run paused.",
    ],
)
def test_every_other_wording_agno_wrote_leaves_the_content(notice: str, tmp_path: Path) -> None:
    """The paused notices Agno's runtime wrote for other pauses read as no content too."""
    assert _upgraded_run(tmp_path, {**_ROWS["agent_paused"], "content": notice}).content is None


@pytest.mark.parametrize(
    "content",
    ["I have tools to execute, but first one question.", "Team run paused. I will pick it up again tomorrow."],
)
def test_a_reply_that_only_opens_like_a_notice_keeps_its_content(content: str, tmp_path: Path) -> None:
    """Only the notices themselves leave, not a reply that happens to start with the same words."""
    assert _upgraded_run(tmp_path, {**_ROWS["agent_resumed_without_text"], "content": content}).content == content


@pytest.mark.parametrize(
    ("row", "content"),
    [
        ("agent_resumed_without_text", _NOTICE),
        ("team_paused", "Team run paused. The following require input:\n- Checker: multiply requires confirmation"),
    ],
)
def test_a_reply_the_model_wrote_that_matches_a_notice_keeps_its_content(
    row: str,
    content: str,
    tmp_path: Path,
) -> None:
    """Text the run's model answered with is its reply, even when it reads exactly like a notice."""
    stored = deepcopy(_ROWS[row])
    stored["content"] = content
    [*_, answer] = [message for message in stored["messages"] if message["role"] == "assistant"]
    answer["content"] = content

    assert _upgraded_run(tmp_path, stored).content == content


def test_a_requester_quoting_a_notice_leaves_the_content_without_it(tmp_path: Path) -> None:
    """Only the run's own answer marks notice-like text as the model's; the requester's message quoting it does not."""
    stored = deepcopy(_ROWS["agent_paused"])
    [*_, request] = [message for message in stored["messages"] if message["role"] == "user"]
    request["content"] = _NOTICE

    assert _upgraded_run(tmp_path, stored).content is None


def test_an_earlier_reply_the_input_carried_is_not_the_run_s_answer(tmp_path: Path) -> None:
    """A thread's earlier reply that the run's input carried is not the run's own answer, whatever its words."""
    stored = deepcopy(_ROWS["agent_paused"])
    earlier = {"id": "earlier-reply", "role": "assistant", "content": _NOTICE, "created_at": 0}
    stored["messages"] = [stored["messages"][0], earlier, *stored["messages"][1:]]

    assert _upgraded_run(tmp_path, stored).content is None


def _paused_with_system(content: str) -> dict[str, Any]:
    """Return the paused agent row with ``content`` as its stored system message."""
    stored = deepcopy(_ROWS["agent_paused"])
    [system] = [message for message in stored["messages"] if message["role"] == "system"]
    system["content"] = content
    return stored


@pytest.mark.parametrize("name", sorted(_SYSTEM_MESSAGES))
def test_agno_s_summary_block_leaves_the_system_message_a_paused_run_stored(name: str, tmp_path: Path) -> None:
    """The system message reads as Agno rendered it without the summary, so the resume adds the scope's summary once.

    The resumed request is the one a run paused with that system message by a current release resumes with.
    """
    rendered = _SYSTEM_MESSAGES[name]
    upgraded = _upgraded_run(tmp_path, _paused_with_system(rendered["with_summary"]))
    current = RunRecord.from_dict(_paused_with_system(rendered["without_summary"]))
    summary = ScopeSummary(text="The checker found 42.", updated_at=datetime(2026, 10, 1, tzinfo=UTC))
    history = [Message(role="user", content="earlier question"), Message(role="assistant", content="earlier answer")]

    resumed = build_resume_request(paused=upgraded.messages, summary=summary, history=history)

    assert upgraded.messages[0].content == rendered["without_summary"]
    assert _without_ids(resumed) == _without_ids(
        build_resume_request(paused=current.messages, summary=summary, history=history),
    )
    assert sum(is_scope_summary(message) for message in resumed) == 1


@pytest.mark.parametrize(
    "content",
    [
        "What does <summary_of_previous_interactions> mean?\n\n",
        f"Why did you get this?\n\n{_SYSTEM_MESSAGES['agent']['with_summary']}\n\n",
    ],
    ids=["tag", "pasted-block"],
)
def test_a_requester_s_message_holding_the_summary_block_stays_as_stored(content: str, tmp_path: Path) -> None:
    """Only a system message's summary block leaves; a requester's message naming the tag or pasting it is theirs."""
    stored = deepcopy(_ROWS["agent_paused"])
    [*_, request] = [message for message in stored["messages"] if message["role"] == "user"]
    request["content"] = content

    upgraded = _upgraded_run(tmp_path, stored)

    assert [message.content for message in upgraded.messages] == [
        message.get("content") for message in stored["messages"]
    ]


def _team_paused_on(calls: list[tuple[str, str, str | None]], member_runs: list[tuple[str, str]]) -> dict[str, Any]:
    """Return the paused team row with one waiting delegation call per ``(call id, member id, child run id)``.

    ``member_runs`` lists the paused member runs as ``(member id, run id)`` in the order the team's requirements name
    them, which is the order its resume runs them.
    """
    stored = deepcopy(_ROWS["team_paused"])
    [template] = stored["tools"]
    [requirement] = stored["requirements"]
    stored["tools"] = [
        {
            **template,
            "tool_call_id": call_id,
            "tool_args": {"member_id": member_id, "task": "Multiply 6 by 7"},
            "result": f"Member '{member_id.title()}' requires human input before continuing.",
            "child_run_id": child_run_id,
        }
        for call_id, member_id, child_run_id in calls
    ]
    stored["requirements"] = [
        {
            **requirement,
            "id": f"requirement-{run_id}",
            "member_agent_id": member_id,
            "member_run_id": run_id,
            "tool_execution": {**requirement["tool_execution"], "tool_call_id": f"call-{run_id}"},
        }
        for member_id, run_id in member_runs
    ]
    return stored


def _upgraded_links(tmp_path: Path, stored: dict[str, Any], member_runs: list[tuple[str, str]]) -> list[str | None]:
    """Store ``stored`` and its paused member rows at layout version 4, upgrade, and return each call's member run."""
    members = [
        {**_ROWS["member_paused"], "run_id": run_id, "agent_id": member_id, "agent_name": member_id.title()}
        for member_id, run_id in member_runs
    ]
    store = _team_store(tmp_path)
    store_rows(store, stored["session_id"], [stored, *members])
    upgrade_from_layout_4(store, _SQUAD)
    run = store.get_run(stored["run_id"])
    assert isinstance(run, TeamRunRecord)
    assert run.tools is not None
    return [tool.child_run_id for tool in run.tools]


def test_a_waiting_call_stored_without_its_member_run_names_it(tmp_path: Path) -> None:
    """v2026.10.574 stored the delegation call that waits on the checker without the checker's run."""
    stored = deepcopy(_ROWS["team_paused"])
    [call] = stored["tools"]
    member_run_id = call["child_run_id"]
    call["child_run_id"] = None

    assert _upgraded_links(tmp_path, stored, [("checker", member_run_id)]) == [member_run_id]


def test_each_waiting_call_names_the_run_of_the_member_it_names(tmp_path: Path) -> None:
    """Two members' calls stored without their runs each take their own member's run, whichever is named first.

    The members share a display name, and so a pause notice; the call each run takes is the one naming its member.
    """
    member_runs = [("checker", "c"), ("writer", "w")]
    stored = _team_paused_on([("call-w", "writer", None), ("call-c", "checker", None)], member_runs)
    for call in stored["tools"]:
        call["result"] = "Member 'Twin' requires human input before continuing."

    assert _upgraded_links(tmp_path, stored, member_runs) == ["w", "c"]


def test_two_waiting_calls_to_one_member_take_its_runs_in_the_order_they_resume(tmp_path: Path) -> None:
    """Calls stored without their member runs cannot be told apart, so the first run to resume takes the first call."""
    member_runs = [("checker", "second"), ("checker", "first")]
    stored = _team_paused_on([("call-1", "checker", None), ("call-2", "checker", None)], member_runs)

    assert _upgraded_links(tmp_path, stored, member_runs) == ["second", "first"]


@pytest.mark.parametrize("linked", ["c", "w"], ids=["link-names-the-paused-run", "link-names-the-finished-run"])
@pytest.mark.parametrize("first", ["writer", "checker"], ids=["writer-then-checker", "checker-twice"])
def test_a_call_a_streamed_agno_turn_linked_to_the_run_that_ended_last_names_its_own(
    first: str,
    linked: str,
    tmp_path: Path,
) -> None:
    """A streamed Agno-runtime turn stored on every call the member run whose delegation ended last.

    The first call's member finished and the checker paused on the second, and both calls name the same run: the
    checker's paused one or the first member's finished one. The waiting call takes the checker's paused run; the
    finished call keeps what it stored.
    """
    stored = _team_paused_on([("call-1", first, linked), ("call-2", "checker", linked)], [("checker", "c")])
    stored["tools"][0]["result"] = "Drafted."

    assert _upgraded_links(tmp_path, stored, [("checker", "c")]) == [linked, "c"]


def test_a_waiting_call_a_streamed_agno_turn_linked_to_a_sibling_s_run_takes_its_own(tmp_path: Path) -> None:
    """Both members paused and both calls name the checker's run; the writer's call takes the writer's run."""
    member_runs = [("checker", "c"), ("writer", "w")]
    stored = _team_paused_on([("call-w", "writer", "c"), ("call-c", "checker", "c")], member_runs)

    assert _upgraded_links(tmp_path, stored, member_runs) == ["w", "c"]


def test_a_call_whose_member_finished_keeps_what_it_stored(tmp_path: Path) -> None:
    """Only a call that still waits takes a member run; one already answered keeps its link and its result."""
    stored = _team_paused_on([("call-done", "checker", "c"), ("call-c", "checker", None)], [("checker", "c")])
    stored["tools"][0]["result"] = "42"

    assert _upgraded_links(tmp_path, stored, [("checker", "c")]) == ["c", "c"]


def test_calls_that_name_their_member_runs_stay_as_stored(tmp_path: Path) -> None:
    """A current release names each call's own run, which the rewrite keeps even when the runs resume in another order."""
    member_runs = [("checker", "first"), ("checker", "second")]
    stored = _team_paused_on([("call-1", "checker", "second"), ("call-2", "checker", "first")], member_runs)

    assert _upgraded_links(tmp_path, stored, member_runs) == ["second", "first"]


def test_the_rewrite_runs_once(tmp_path: Path) -> None:
    """The upgrade records layout version 6, a second open changes nothing, and a database at it is not rewritten."""
    store = _agent_store(tmp_path)
    store_rows(store, _SESSION, [_ROWS["agent_paused"]])
    upgrade_from_layout_4(store, _AGENT)
    upgraded = _dump(store)

    create_agent_state_storage(_AGENT.scope_id, tmp_path)
    assert _dump(store) == upgraded
    store_rows(store, "session-2", [{**_ROWS["agent_paused"], "run_id": "run-2"}])
    reopened = create_agent_state_storage(_AGENT.scope_id, tmp_path)

    with closing(sqlite3.connect(store.path)) as connection:
        assert layout_version(connection, TableNames.of_database(store.path)) == LOG_VERSION
    assert reopened.get_run("run-1").content is None
    assert reopened.get_run("run-2").content == _NOTICE
