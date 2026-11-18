"""Tests for the thread workflow groups that matrix_room shares with MindRoom Chat."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nio
import pytest

from mindroom.matrix.event_types import BACKGROUND_JOBS_EVENT_TYPE
from mindroom.scheduling.tasks import CronSchedule, ScheduledWorkflow
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from mindroom.thread_workflow import (
    classify_thread_workflow,
    latest_reply_question_ts,
    room_thread_facts,
    thread_workflow,
)
from tests.scheduling_helpers import SCHEDULE_WRITER_ID, schedule_runtime_paths, scheduled_task_state_event

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "thread-workflow-cases.json").read_text(encoding="utf-8"),
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_shared_workflow_case(case: dict[str, Any]) -> None:
    """Each shared case classifies as MindRoom Chat classifies it."""
    assert classify_thread_workflow(case["status"], case["now"]) == case["expected"]


def _ms(year: int, month: int = 1, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


_NOW = _ms(2026, 6, 1)
_RESOLVED_AT = _ms(2026)


def _tag_record(data: dict[str, object] | None = None, set_at: str = "2026-01-01T00:00:00+00:00") -> dict[str, object]:
    return {"set_by": "@user:server", "set_at": set_at, "data": data or {}}


def _tag(thread_id: str, tag: str, content: object = None, *, ts: int = _RESOLVED_AT) -> dict[str, Any]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": json.dumps([thread_id, tag], separators=(",", ":")),
        "sender": "@user:server",
        "origin_server_ts": ts,
        "content": _tag_record() if content is None else content,
    }


def _legacy_tags(thread_id: str, tags: dict[str, object], *, ts: int) -> dict[str, Any]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": thread_id,
        "sender": "@user:server",
        "origin_server_ts": ts,
        "content": {"tags": tags},
    }


def _task(task_id: str, *, thread_id: str, status: str = "pending", **workflow: Any) -> dict[str, Any]:  # noqa: ANN401
    workflow.setdefault("schedule_type", "once")
    scheduled = ScheduledWorkflow(
        message="m",
        description="d",
        created_by="@user:server",
        thread_id=thread_id,
        **workflow,
    )
    return scheduled_task_state_event(task_id, {"workflow": scheduled.model_dump_json(), "status": status})


def _job(job_id: str, thread_id: str | None, status: str = "running") -> dict[str, object]:
    return {
        "job_id": job_id,
        "thread_id": thread_id,
        "kind": "tool",
        "tool": "shell",
        "status": status,
        "started_at": "2026-06-01T00:00:00Z",
    }


def _jobs(jobs: object, *, sender: str = SCHEDULE_WRITER_ID, state_key: str | None = None) -> dict[str, Any]:
    return {
        "type": BACKGROUND_JOBS_EVENT_TYPE,
        "state_key": state_key or sender,
        "sender": sender,
        "content": {"jobs": jobs, "total": 1},
    }


def _member(user_id: str, membership: str = "join") -> dict[str, Any]:
    return {"type": "m.room.member", "state_key": user_id, "sender": user_id, "content": {"membership": membership}}


def _facts(tmp_path: Path, events: list[Any]) -> dict[str, dict[str, Any]]:
    state = nio.RoomGetStateResponse(events=events, room_id="!test:server")
    return room_thread_facts("!test:server", state, schedule_runtime_paths(tmp_path), {SCHEDULE_WRITER_ID}, _NOW)


def test_room_thread_facts_reads_tags_tasks_and_jobs(tmp_path: Path) -> None:
    """Resolved and snoozed tags, pending tasks and jobs a joined bot published become classifier inputs."""
    events = [
        _member(SCHEDULE_WRITER_ID),
        _tag("$resolved", "resolved"),
        _tag("$snoozed", "snoozed", _tag_record({"until": "2999-01-01T00:00:00+00:00"})),
        _task("t1", thread_id="$later", execute_at=datetime(2999, 1, 1, tzinfo=UTC)),
        _task("t2", thread_id="$later", schedule_type="cron", cron_schedule=CronSchedule(minute="0")),
        _task("t3", thread_id="$soon", execute_at=datetime(2999, 1, 1, tzinfo=UTC)),
        _task("t4", thread_id="$soon", execute_at=datetime(2998, 1, 1, tzinfo=UTC)),
        _jobs([_job("j1", "$job"), _job("j2", "$approval", "awaiting_approval"), _job("j3", None)]),
    ]
    assert _facts(tmp_path, events) == {
        "$resolved": {"resolved_ts": _RESOLVED_AT},
        "$snoozed": {"snoozed_until_ts": _ms(2999)},
        "$later": {"scheduled_task_count": 2},
        "$soon": {"scheduled_task_count": 2, "next_scheduled_ts": _ms(2998)},
        "$job": {"has_background_job": True},
        "$approval": {"has_background_approval": True},
    }


@pytest.mark.parametrize("set_at", ["2999-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"])
def test_room_thread_facts_resolves_at_the_servers_time(tmp_path: Path, set_at: str) -> None:
    """A resolve counts from its state event's server time, not from the clock its writer put in `set_at`."""
    events = [_tag("$t", "resolved", _tag_record(set_at=set_at), ts=_RESOLVED_AT)]
    assert _facts(tmp_path, events) == {"$t": {"resolved_ts": _RESOLVED_AT}}


def test_room_thread_facts_reads_legacy_tags_under_per_tag_events(tmp_path: Path) -> None:
    """A thread's legacy tags event counts unless a per-tag event overrides it or removes the tag."""
    snooze = _tag_record({"until": "2999-01-01T00:00:00+00:00"})
    events = [
        _legacy_tags("$old", {"resolved": _tag_record(), "snoozed": snooze}, ts=1000),
        _legacy_tags("$both", {"resolved": _tag_record(), "snoozed": snooze}, ts=1000),
        _tag("$both", "resolved", ts=2000),
        _tag("$both", "snoozed", {}),
        _tag("$broken", "resolved", {"set_by": 5}),
        _legacy_tags("$broken", {"resolved": _tag_record()}, ts=3000),
    ]
    assert _facts(tmp_path, events) == {
        "$old": {"resolved_ts": 1000, "snoozed_until_ts": _ms(2999)},
        "$both": {"resolved_ts": 2000},
        "$broken": {"resolved_ts": 3000},
    }


def test_room_thread_facts_ignores_state_it_cannot_trust(tmp_path: Path) -> None:
    """Malformed, finished, past and foreign records contribute nothing, and the healthy ones still count."""
    events = [
        "not an event",
        _member(SCHEDULE_WRITER_ID),
        _member("@mindroom_gone:server", "leave"),
        _member("@mindroom_other:server"),
        _tag("$empty", "resolved", {}),
        _tag("$soon", "snoozed", _tag_record({"until": "soon"})),
        {**_tag("$listkey", "resolved"), "state_key": ["$listkey", "resolved"]},
        _task("t1", thread_id="$past", execute_at=datetime(2000, 1, 1, tzinfo=UTC)),
        _task("t2", thread_id="$new", new_thread=True),
        _task("t3", thread_id="$cancelled", status="cancelled"),
        {**_task("t4", thread_id="$foreign"), "sender": "@mallory:server"},
        scheduled_task_state_event("t5", {"workflow": "[1, 2]", "status": "pending"}),
        _task("t6", thread_id="$healthy", schedule_type="cron", cron_schedule=CronSchedule(minute="0")),
        _jobs([_job("j1", "$mallory")], sender="@mallory:server"),
        _jobs([_job("j2", "$other")], state_key="@user:server"),
        _jobs([_job("j3", "$gone")], sender="@mindroom_gone:server"),
        _jobs("none", sender="@mindroom_other:server"),
        _jobs([{"job_id": "j4", "thread_id": "$incomplete"}, _job("j5", "$job")]),
    ]
    state = nio.RoomGetStateResponse(events=events, room_id="!test:server")
    bot_ids = {SCHEDULE_WRITER_ID, "@mindroom_gone:server", "@mindroom_other:server"}
    facts = room_thread_facts("!test:server", state, schedule_runtime_paths(tmp_path), bot_ids, _NOW)
    assert facts == {"$healthy": {"scheduled_task_count": 1}, "$job": {"has_background_job": True}}


_QUESTION_WAITS_MS = 7 * 24 * 3600 * 1000
_NEEDS_APPROVAL = {"workflow": "needs_you", "why": "approval"}
_NEEDS_ANSWER = {"workflow": "needs_you", "why": "question"}


@pytest.mark.parametrize(
    ("facts", "kwargs", "expected"),
    [
        ({"resolved_ts": 5000}, {"approval_ts": 4000}, {"workflow": "done"}),
        ({"resolved_ts": 5000}, {"approval_ts": 6000}, _NEEDS_APPROVAL),
        ({"resolved_ts": 5000}, {"question_ts": 5000}, {"workflow": "done"}),
        ({"resolved_ts": 5000}, {"question_ts": 6000}, _NEEDS_ANSWER),
        ({}, {"question_ts": 10_000 - _QUESTION_WAITS_MS}, {"workflow": "needs_you", "why": "your_turn"}),
        ({}, {"question_ts": 10_001 - _QUESTION_WAITS_MS}, _NEEDS_ANSWER),
        ({"scheduled_task_count": 2, "next_scheduled_ts": 20_000}, {}, {"workflow": "later", "back_at": 20_000}),
        ({"scheduled_task_count": 2}, {}, {"workflow": "later"}),
        ({"has_background_job": True}, {}, {"workflow": "working"}),
        ({}, {"replying": True}, {"workflow": "working"}),
        ({"has_background_approval": True, "snoozed_until_ts": 20_000}, {}, _NEEDS_APPROVAL),
    ],
)
def test_thread_workflow_counts_only_what_was_asked_after_the_resolve(
    facts: dict[str, Any],
    kwargs: dict[str, Any],
    expected: dict[str, object],
) -> None:
    """Approvals and questions count only after the resolve, and a question only for seven days."""
    assert thread_workflow(facts, now=10_000, **{"replying": False, **kwargs}) == expected


_BOT = "@mindroom_general:localhost"
# As the writer keys options: by the reaction that answers, and by its number as a typed alias.
_PROMPT = {
    "io.mindroom.interactive": {
        "question_text": "Pick one",
        "options": {"👍": "a", "1": "a"},
        "option_labels": {"👍": "A", "1": "A"},
    },
}
_NUMBERS_ONLY = {"io.mindroom.interactive": {"question_text": "Pick one", "options": {"1": "a"}}}


def _reply(
    content: dict[str, object],
    *,
    sender: object = _BOT,
    edit: dict[str, object] | None = None,
) -> dict[str, object]:
    reply: dict[str, object] = {"sender": sender, "origin_server_ts": 100, "content": {"body": "x", **content}}
    if edit is not None:
        reply["unsigned"] = {"m.relations": {"m.replace": edit}}
    return {"io.mindroom.latest_reply": reply}


def _edit(new_content: dict[str, object], *, sender: str = _BOT) -> dict[str, object]:
    return {"sender": sender, "origin_server_ts": 200, "content": {"m.new_content": {"body": "x", **new_content}}}


@pytest.mark.parametrize(
    ("thread_metadata", "expected"),
    [
        pytest.param(_reply(_PROMPT), 100, id="question in the reply"),
        pytest.param(_reply({}, edit=_edit(_PROMPT)), 200, id="question added by the newest edit"),
        pytest.param(_reply(_PROMPT, edit=_edit({})), 200, id="edit keeps the original's question"),
        pytest.param(_reply(_PROMPT, edit={"sender": _BOT, "content": "x"}), None, id="edit without a time"),
        pytest.param(_reply({}, edit=_edit(_PROMPT, sender="@mallory:localhost")), None, id="someone else's edit"),
        pytest.param(_reply(_PROMPT, sender="@user:localhost"), None, id="reply from a person"),
        pytest.param(_reply(_PROMPT, sender=["@x:localhost"]), None, id="sender is not a string"),
        pytest.param(_reply(_NUMBERS_ONLY), None, id="no option to react with"),
        pytest.param(_reply({"io.mindroom.interactive": {"options": {"👍": "a"}}}), None, id="no question text"),
        pytest.param({"count": 3}, None, id="no bundle"),
    ],
)
def test_latest_reply_question_ts(thread_metadata: dict[str, Any], expected: int | None) -> None:
    """A question counts when the bundled newest reply, as its newest own edit shows it, asks one the client shows."""
    assert latest_reply_question_ts(thread_metadata, {_BOT}) == expected
