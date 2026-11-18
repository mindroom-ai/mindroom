"""A thread's workflow group as MindRoom Chat's Threads page computes it: needs you, working, later or done.

Ports `isWaitingOnYou`, `classifyThreadWorkflow` and `threadReturnTs` (mindroom-chat
`src/app/event-store/threads/threadWorkflow.ts`), combined as `src/app/threads/page/threadGroups.ts` does.
Both repositories run `tests/fixtures/thread-workflow-cases.json` against their implementation.
"""

from __future__ import annotations


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
