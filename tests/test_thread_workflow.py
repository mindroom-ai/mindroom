"""Tests for the thread workflow groups that matrix_room shares with MindRoom Chat."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nio
import pytest

from mindroom.interactive_models import InteractivePrompt, interactive_prompt_content
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
    assert classify_thread_workflow(now=case["now"], **case["status"]) == case["expected"]


def _ms(year: int, month: int = 1, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


_NOW = _ms(2026, 6, 1)


def _tag(thread_id: str, tag: str, *, data: dict[str, object] | None = None) -> dict[str, Any]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": json.dumps([thread_id, tag], separators=(",", ":")),
        "sender": "@user:server",
        "content": {"set_by": "@user:server", "set_at": "2026-01-01T00:00:00+00:00", "data": data or {}},
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


def _jobs(jobs: object, *, sender: str = SCHEDULE_WRITER_ID, state_key: str | None = None) -> dict[str, Any]:
    return {
        "type": BACKGROUND_JOBS_EVENT_TYPE,
        "state_key": state_key or sender,
        "sender": sender,
        "content": {"jobs": jobs, "total": 1},
    }


def _facts(tmp_path: Path, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    state = nio.RoomGetStateResponse(events=events, room_id="!test:server")
    return room_thread_facts("!test:server", state, schedule_runtime_paths(tmp_path), {SCHEDULE_WRITER_ID}, _NOW)


def test_room_thread_facts_reads_tags_tasks_and_jobs(tmp_path: Path) -> None:
    """Resolved and snoozed tags, pending tasks and published jobs become per-thread facts."""
    events = [
        _tag("$resolved", "resolved"),
        _tag("$snoozed", "snoozed", data={"until": "2999-01-01T00:00:00+00:00"}),
        _task("t1", thread_id="$later", execute_at=datetime(2999, 1, 1, tzinfo=UTC)),
        _task("t2", thread_id="$later", schedule_type="cron", cron_schedule=CronSchedule(minute="0")),
        _jobs(
            [
                {"job_id": "j1", "thread_id": "$job", "status": "running"},
                {"job_id": "j2", "thread_id": "$approval", "status": "awaiting_approval"},
                {"job_id": "j3", "thread_id": None, "status": "running"},
            ],
        ),
    ]
    assert _facts(tmp_path, events) == {
        "$resolved": {"resolved_ts": _ms(2026)},
        "$snoozed": {"snoozed_until_ts": _ms(2999)},
        "$later": {"runs": [_ms(2999), None]},
        "$job": {"has_background_job": True},
        "$approval": {"has_background_approval": True},
    }


def test_room_thread_facts_ignores_state_it_cannot_trust(tmp_path: Path) -> None:
    """Malformed, finished, past and foreign state contributes nothing, without an exception."""
    events = [
        {**_tag("$empty", "resolved"), "content": {}},
        _tag("$soon", "snoozed", data={"until": "soon"}),
        _task("t1", thread_id="$past", execute_at=datetime(2000, 1, 1, tzinfo=UTC)),
        _task("t2", thread_id="$new", new_thread=True),
        _task("t3", thread_id="$cancelled", status="cancelled"),
        {**_task("t4", thread_id="$foreign"), "sender": "@mallory:server"},
        _jobs([{"job_id": "j1", "thread_id": "$mallory", "status": "running"}], sender="@mallory:server"),
        _jobs([{"job_id": "j2", "thread_id": "$other", "status": "running"}], state_key="@user:server"),
        _jobs("none"),
    ]
    assert _facts(tmp_path, events) == {}


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
        ({"runs": [20_000, 30_000]}, {}, {"workflow": "later", "back_at": 20_000}),
        ({"runs": [20_000, None]}, {}, {"workflow": "later"}),
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
_PROMPT = interactive_prompt_content(
    InteractivePrompt(
        creator_agent="general",
        question_text="Pick one",
        options={"1": "a"},
        option_labels={"1": "A"},
        source_event_id="$src",
    ),
)


def _reply(
    content: dict[str, object],
    *,
    sender: str = _BOT,
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
        pytest.param(_reply({}, edit=_edit(_PROMPT, sender="@mallory:localhost")), None, id="someone else's edit"),
        pytest.param(_reply(_PROMPT, sender="@user:localhost"), None, id="reply from a person"),
        pytest.param(
            _reply({"io.mindroom.interactive": {"question_text": "Pick one"}}),
            None,
            id="prompt without options",
        ),
        pytest.param({"count": 3}, None, id="no bundle"),
    ],
)
def test_latest_reply_question_ts(thread_metadata: dict[str, Any], expected: int | None) -> None:
    """A question counts when the bundled newest reply, as its newest own edit shows it, is an agent's prompt."""
    assert latest_reply_question_ts(thread_metadata, {_BOT}) == expected
