"""A thread's workflow group as MindRoom Chat's Threads page computes it: needs you, working, later or done.

Ports `isWaitingOnYou`, `classifyThreadWorkflow` and `threadReturnTs` (mindroom-chat
`src/app/event-store/threads/threadWorkflow.ts`), combined as `src/app/threads/page/threadGroups.ts` does.
Both repositories run `tests/fixtures/thread-workflow-cases.json` against their implementation.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from mindroom.interactive_models import interactive_prompt_from_content
from mindroom.matrix.client_visible_messages import bundled_replacement_candidates
from mindroom.matrix.event_types import BACKGROUND_JOBS_EVENT_TYPE
from mindroom.scheduling.tasks import task_records_with_writers
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
    from datetime import datetime

    import nio

    from mindroom.constants import RuntimePaths

# The client's QUESTION_WAITS_MS: an unanswered question stops counting after seven days.
_QUESTION_WAITS_MS = 7 * 24 * 60 * 60 * 1000


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def classify_thread_workflow(  # noqa: PLR0911
    *,
    now: int,
    is_resolved: bool = False,
    snoozed_until_ts: int | None = None,
    is_streaming: bool = False,
    has_background_job: bool = False,
    has_background_approval: bool = False,
    scheduled_task_count: int = 0,
    next_scheduled_ts: int | None = None,
    pending_approval: bool = False,
    pending_question: bool = False,
) -> dict[str, object]:
    """Return a thread's group, with `why` on needs-you rows and `back_at` (ms) on later rows when known."""
    if pending_approval or (has_background_approval and not is_resolved):
        return {"workflow": "needs_you", "why": "approval"}
    if pending_question:
        return {"workflow": "needs_you", "why": "question"}
    if is_resolved:
        return {"workflow": "done"}
    if snoozed_until_ts is not None and snoozed_until_ts > now:
        return {"workflow": "later", "back_at": snoozed_until_ts}
    if is_streaming or has_background_job:
        return {"workflow": "working"}
    if scheduled_task_count > 0:
        return (
            {"workflow": "later"} if next_scheduled_ts is None else {"workflow": "later", "back_at": next_scheduled_ts}
        )
    return {"workflow": "needs_you", "why": "your_turn"}


def room_thread_facts(  # noqa: C901
    room_id: str,
    state: nio.RoomGetStateResponse,
    runtime_paths: RuntimePaths,
    bot_ids: Collection[str],
    now: int,
) -> dict[str, dict[str, Any]]:
    """Read each thread's resolve time, snooze end, pending runs and published jobs from one room-state response."""
    facts: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    for event in state.events:
        content = event.get("content")
        if event.get("type") == THREAD_TAGS_EVENT_TYPE:
            parsed = parse_thread_tag_state_key(event.get("state_key"))
            if parsed is None or parsed[1] not in (RESOLVED_THREAD_TAG, SNOOZED_THREAD_TAG):
                continue
            if (record := parse_thread_tag_record(parsed[1], content)) is None:
                continue
            if parsed[1] == RESOLVED_THREAD_TAG:
                facts[parsed[0]]["resolved_ts"] = _ms(record.set_at)
            elif (until := snooze_until(record)) is not None:
                facts[parsed[0]]["snoozed_until_ts"] = _ms(until)
        elif (
            event.get("type") == BACKGROUND_JOBS_EVENT_TYPE
            and event.get("sender") == event.get("state_key")
            and event.get("sender") in bot_ids
            and isinstance(content, dict)
            and isinstance(jobs := content.get("jobs"), list)
        ):
            for job in jobs:
                if isinstance(job, dict) and isinstance(thread_id := job.get("thread_id"), str):
                    waits = job.get("status") == "awaiting_approval"
                    facts[thread_id]["has_background_approval" if waits else "has_background_job"] = True
    for task, _writer in task_records_with_writers(room_id, state, runtime_paths):
        workflow = task.workflow
        execute_at = None if workflow.execute_at is None else _ms(workflow.execute_at)
        if workflow.thread_id and not workflow.new_thread and (execute_at is None or execute_at > now):
            facts[workflow.thread_id].setdefault("runs", []).append(execute_at)
    return dict(facts)


def thread_workflow(
    facts: Mapping[str, Any],
    *,
    now: int,
    replying: bool,
    approval_ts: int | None = None,
    question_ts: int | None = None,
) -> dict[str, object]:
    """Label one thread from its `room_thread_facts` entry; an approval or question older than the resolve is answered."""
    resolved_ts = facts.get("resolved_ts")
    runs = facts.get("runs", [])

    def asked_since_resolve(ts: int | None) -> bool:
        return ts is not None and (resolved_ts is None or ts > resolved_ts)

    return classify_thread_workflow(
        now=now,
        is_resolved=resolved_ts is not None,
        snoozed_until_ts=facts.get("snoozed_until_ts"),
        is_streaming=replying,
        has_background_job=facts.get("has_background_job", False),
        has_background_approval=facts.get("has_background_approval", False),
        scheduled_task_count=len(runs),
        next_scheduled_ts=min(runs) if runs and None not in runs else None,
        pending_approval=asked_since_resolve(approval_ts),
        pending_question=question_ts is not None
        and now - question_ts < _QUESTION_WAITS_MS
        and asked_since_resolve(question_ts),
    )


def latest_reply_question_ts(thread_metadata: Mapping[str, Any], bot_ids: Collection[str]) -> int | None:
    """Return when the root's bundled newest reply, as its newest own edit shows it, asked an agent's question."""
    reply = thread_metadata.get("io.mindroom.latest_reply")
    if not isinstance(reply, dict) or reply.get("sender") not in bot_ids:
        return None
    edit = next((c for c in bundled_replacement_candidates(reply) if c.get("sender") == reply["sender"]), None)
    shown = edit.get("content", {}).get("m.new_content") if edit is not None else None
    original = reply.get("content")
    if not any(isinstance(c, dict) and interactive_prompt_from_content(c) for c in (shown, original)):
        return None
    ts = (edit or reply).get("origin_server_ts")
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else None
