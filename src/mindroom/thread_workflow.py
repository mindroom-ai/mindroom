"""A thread's workflow group as MindRoom Chat's Threads page computes it (`threadWorkflow.ts`, `threadGroups.ts`).

Both repositories run `tests/fixtures/thread-workflow-cases.json` against their implementation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import ACTING_REQUESTER_KEY
from mindroom.matrix.client_visible_messages import bundled_replacement_candidates
from mindroom.matrix.event_types import BACKGROUND_JOBS_EVENT_TYPE
from mindroom.scheduling.tasks import task_records
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    SNOOZED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    parse_thread_tag_record,
    parse_thread_tag_state_key,
    snooze_until,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.thread_tags import ThreadTagRecord

# The client's QUESTION_WAITS_MS: an unanswered question stops counting after seven days.
_QUESTION_WAITS_MS = 7 * 24 * 60 * 60 * 1000
_JOB_FIELDS = ("job_id", "kind", "tool", "status", "started_at")


def classify_thread_workflow(status: Mapping[str, Any], now: int) -> dict[str, object]:  # noqa: PLR0911
    """Group a thread by the case file's status keys (absent: false, 0 or unknown), with `why` or known `back_at`."""
    if status.get("pending_approval") or (status.get("has_background_approval") and not status.get("is_resolved")):
        return {"workflow": "needs_you", "why": "approval"}
    if status.get("pending_question"):
        return {"workflow": "needs_you", "why": "question"}
    if status.get("is_resolved"):
        return {"workflow": "done"}
    if (snoozed_until_ts := status.get("snoozed_until_ts")) is not None and snoozed_until_ts > now:
        return {"workflow": "later", "back_at": snoozed_until_ts}
    if status.get("is_streaming") or status.get("has_background_job"):
        return {"workflow": "working"}
    if status.get("scheduled_task_count", 0) > 0:
        next_ts = status.get("next_scheduled_ts")
        return {"workflow": "later"} if next_ts is None else {"workflow": "later", "back_at": next_ts}
    return {"workflow": "needs_you", "why": "your_turn"}


def _lifecycle_tags(events: list[dict[str, Any]]) -> dict[tuple[str, str], tuple[ThreadTagRecord, object]]:
    """Each thread's tags and their event's server time; per-tag events override or (`{}`) remove legacy tags."""
    legacy: dict[tuple[str, str], tuple[ThreadTagRecord, object]] = {}
    per_tag: dict[tuple[str, str], tuple[ThreadTagRecord | None, object]] = {}
    for event in events:
        content, state_key, ts = event.get("content"), event.get("state_key"), event.get("origin_server_ts")
        if event.get("type") != THREAD_TAGS_EVENT_TYPE or not isinstance(content, dict):
            continue
        if (key := parse_thread_tag_state_key(state_key)) is not None:
            record = parse_thread_tag_record(key[1], content) if content else None
            if record is not None or not content:
                per_tag[key] = (record, ts)
        elif isinstance(state_key, str) and isinstance(tags := content.get("tags"), dict):
            for tag, value in tags.items():
                if isinstance(tag, str) and (record := parse_thread_tag_record(tag, value)) is not None:
                    legacy[state_key, tag] = (record, ts)
    return {key: (record, ts) for key, (record, ts) in (legacy | per_tag).items() if record is not None}


def _published_jobs(events: list[dict[str, Any]], bot_ids: Collection[str]) -> list[dict[str, Any]]:
    """The well-formed jobs that a bot still joined published about itself, as the client's `agentRoomState` reads."""
    joined = {
        event.get("state_key")
        for event in events
        if event.get("type") == "m.room.member"
        and isinstance(event.get("state_key"), str)
        and isinstance(event.get("content"), dict)
        and event["content"].get("membership") == "join"
    }
    return [
        job
        for event in events
        if event.get("type") == BACKGROUND_JOBS_EVENT_TYPE
        and isinstance(publisher := event.get("state_key"), str)
        and publisher == event.get("sender")
        and publisher in bot_ids
        and publisher in joined
        and isinstance(content := event.get("content"), dict)
        and isinstance(jobs := content.get("jobs"), list)
        for job in jobs
        if isinstance(job, dict)
        and all(isinstance(job.get(field), str) for field in _JOB_FIELDS)
        and isinstance(job.get("thread_id"), str)
    ]


def room_thread_facts(
    room_id: str,
    state: nio.RoomGetStateResponse,
    runtime_paths: RuntimePaths,
    bot_ids: Collection[str],
    now: int,
) -> dict[str, dict[str, Any]]:
    """Read each thread's classifier inputs, and `resolved_ts`, from one room-state response; bad records are skipped."""
    events = [event for event in state.events if isinstance(event, dict)]
    facts: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    # As the client: a pinned root is not resolved, whatever its tag says.
    pinned = {
        root
        for event in events
        if event.get("type") == "m.room.pinned_events"
        and event.get("state_key") == ""
        and isinstance(content := event.get("content"), dict)
        and isinstance(roots := content.get("pinned"), list)
        for root in roots
        if isinstance(root, str)
    }
    for (thread_id, tag), (record, ts) in _lifecycle_tags(events).items():
        if tag == RESOLVED_THREAD_TAG and thread_id not in pinned:
            facts[thread_id]["resolved_ts"] = ts if isinstance(ts, int) else 0
        elif tag == SNOOZED_THREAD_TAG and (until := snooze_until(record)) is not None:
            facts[thread_id]["snoozed_until_ts"] = int(until.timestamp() * 1000)
    for job in _published_jobs(events, bot_ids):
        waits = job["status"] == "awaiting_approval"
        facts[job["thread_id"]]["has_background_approval" if waits else "has_background_job"] = True
    runs: defaultdict[str, list[int | None]] = defaultdict(list)
    for task in task_records(room_id, state, runtime_paths):
        workflow = task.workflow
        execute_at = None if workflow.execute_at is None else int(workflow.execute_at.timestamp() * 1000)
        if workflow.thread_id and not workflow.new_thread and (execute_at is None or execute_at > now):
            runs[workflow.thread_id].append(execute_at)
    for thread_id, times in runs.items():
        facts[thread_id]["scheduled_task_count"] = len(times)
        if None not in times:
            facts[thread_id]["next_scheduled_ts"] = min(time for time in times if time is not None)
    return dict(facts)


def thread_workflow(
    facts: Mapping[str, Any],
    *,
    now: int,
    replying: bool,
    approval_ts: int | None = None,
    question_ts: int | None = None,
) -> dict[str, object]:
    """Label one thread from its `room_thread_facts` entry; a request older than the resolve counts as answered."""
    resolved_ts = facts.get("resolved_ts")
    cutoff = float("-inf") if resolved_ts is None else resolved_ts
    status = {
        **facts,
        "is_resolved": resolved_ts is not None,
        "is_streaming": replying,
        "pending_approval": approval_ts is not None and approval_ts > cutoff,
        "pending_question": question_ts is not None and question_ts > max(cutoff, now - _QUESTION_WAITS_MS),
    }
    return classify_thread_workflow(status, now)


def latest_reply_question_ts(
    thread_metadata: Mapping[str, Any],
    bot_ids: Collection[str],
    requester_ids: Collection[str] = (),
) -> int | None:
    """Return when the root's bundled newest reply, as its newest own edit shows it, asked the requester a question.

    A reply that names another person as its acting requester asks that person, as the client skips it.
    """
    reply = thread_metadata.get("io.mindroom.latest_reply")
    sender = reply.get("sender") if isinstance(reply, dict) else None
    if not isinstance(reply, dict) or not isinstance(sender, str) or sender not in bot_ids:
        return None
    # A malformed edit is ignored, so the original's question and time stand.
    edits = [c for c in bundled_replacement_candidates(reply) if c.get("sender") == sender]
    ok = [
        c for c in edits if isinstance(c.get("content"), dict) and isinstance(c["content"].get("m.new_content"), dict)
    ]
    edit = ok[0] if ok else None
    shown = edit["content"]["m.new_content"] if edit is not None else None
    # As the client's readInteractiveQuestion: the shown content's prompt, else the original's.
    payloads = [c.get("io.mindroom.interactive") for c in (shown, reply.get("content")) if isinstance(c, dict)]
    prompt = next((payload for payload in payloads if payload is not None), None)
    # A streamed reply names its acting requester in the edit that replaces its placeholder.
    askers = [c.get(ACTING_REQUESTER_KEY) for c in (shown, reply.get("content")) if isinstance(c, dict)]
    asker = next((a for a in askers if a is not None), None)
    if requester_ids and isinstance(asker, str) and asker not in requester_ids and asker not in bot_ids:
        return None
    options = prompt.get("options") if isinstance(prompt, dict) else None
    # A question the client shows: text, and an option keyed by the reaction that picks it, not only by number.
    if not (
        isinstance(options, dict)
        and isinstance(text := cast("dict[str, object]", prompt).get("question_text"), str)
        and text
        and any(isinstance(value, str) and not re.fullmatch("[0-9]+", str(key)) for key, value in options.items())
    ):
        return None
    ts = (edit or reply).get("origin_server_ts")
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else None
