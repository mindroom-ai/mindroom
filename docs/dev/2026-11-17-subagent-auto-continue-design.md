# Subagents continue after a restart

This record covers how a background subagent job that a restart cut short continues by itself, and what was rejected.

## Goal

A restart is not a user decision about running work.
A top-level reply a restart cut short already continues in place, but a background subagent job did not: it ended with "Subagent turn was interrupted by a restart. Send a follow-up to continue its history.", and its parent got a follow-up turn that told the user instead of continuing.
Success: after a clean shutdown or a crash, every background subagent turn the restart cut short continues from its recovered run without its parent calling `continue_subagent`, and the parent receives that subagent's real answer like any finished job's; nothing runs twice, a crash loop cannot continue forever, and no cancel or Stop is undone.

## Prior art

- A subagent call's job runs its child (`delegation_call_job`, `_run_job_child` in `delegation/execution.py`); the saved job keeps the child's snapshot in `adapter["child"]`, and the child's handle file names its current turn (`delegation/sessions.py`).
- A cut-short child turn is settled as `failed` with the restart reason, and its checkpointed run becomes a turn history replays (`interrupt_child` → `recover_interrupted_run`), by the job's own task at an orderly shutdown and by the next start's recovery after a crash, under the handle's recovery lock.
- `ToolJobRuntime._shutdown` settled every running job as stopped, and `ToolJobRuntime.recover` settled every saved unfinished job before dispatch started, so both paths ended the job.
- `continue_subagent` resolves the handle, rechecks the caller's grant and the requester's access, prepares a follow-up turn (`prepare_child_turn(previous=...)`), and runs it as a job (`delegation_call_job`).
- A top-level turn restarts cut short more than `_NATIVE_RECOVERY_LIMIT` (2) times stops continuing from its recovered runs.
- `orchestration/script_runtime.py` rebuilds a conversation's tool context from a saved execution identity through its bot (`ToolRuntimeSupport.build_context`).

## Decisions

1. **Identity: the job does not end.**
   A subagent job a restart stops while it is `running` keeps its job ID and stays `running` across the restart, and its continuation runs as that job.
   The invariants stay as they were: an ended job never runs again, and no call of a child turn runs twice, because the continuation is a new turn of the child's session that starts from the cut-short turn's recovered run.
   A parent that waits on or collects the job ID sees it running and then reads the continuation's outcome, `job list` shows one running job, and the job's `subagent_id` does not change.
2. **Hook point: shutdown leaves the job, the next start's worker pass continues it.**
   At an orderly shutdown the runtime no longer settles such a job, so a shutdown and a crash both leave a saved unfinished job; the child's cut-short turn is settled as before.
   `ToolJobRuntime.recover` restores it unsettled, marked as stopped by the restart, and the coordinator's worker pass (`ToolJobRuntimeCoordinator._reconcile`) continues or settles it.
   The pass, not `recover`, launches the continuation, because `recover` runs before the first Matrix sync, when room-backed reply grants are still unresolved and authorization fails closed; the pass waits while the job's grant is pending and its bot has no client.
   The pass builds the conversation's tool context from the recipient's bot, as script runs do, and the launch is the one `continue_subagent` uses: the settled handle, `follow_up_refusal`, `authorize_delegation`, `prepare_child_turn(previous=...)`, and `delegation_call_job`.
   Which job kinds continue is a predicate the runtime's creator passes; the coordinator names subagent jobs.
3. **Parent notices: none until the job really ends.**
   A job that does not end produces no ended notice and no follow-up turn, and its real outcome later reaches the parent through the existing notice, `collect`, and follow-up path.
   A parent reply the same restart cut short resumes by journal replay; when its call was still waiting for the job, its recovered run only says that call was interrupted, so the turn that started the job is told in its turn-start job note that the job continues on its own and must not be started again.
   Only the turn whose run started the job gets that line (`source_event_id`), since every other reply already holds the job's handle in its history.
4. **What the child is sent.**
   The continuation is a follow-up turn on the child's session whose prompt is `SUBAGENT_RESTART_CONTINUATION_PROMPT` (`prompts.py`, so `prompts:` can override it) when the cut-short run was recovered into history.
   A cut-short turn that called no tool leaves no run in history, as for a top-level replay, so its continuation sends the turn's own task again.
   Cache impact: recovery stores the run's messages exactly as its requests sent them and adds only answers for unanswered calls, and the continuation appends one user message, so its first request extends the cut-short run's last checkpointed request and a provider's prompt cache still matches it; nothing in history, limits, or compaction treats the recovered run specially.
5. **Which interruptions continue.**
   Only a subagent job stopped while `running` by a shutdown, restart, or event-loop teardown, whose child turn the restart settled.
   Never continued: a job with a saved cancel request, a job whose reply was stopped (the Stop is now saved on the job, where it lived only in memory), a job whose grant is revoked (the pass cancels it first, and the continuation rechecks the delegation), a child turn that completed, failed, or was cancelled on its own before the stop (its settlement stands), and a job that was `awaiting_approval`.
   A child that waited for an approval is not continued because the restart denies its card: continuing would either ask the person again about a call they saw denied or go on without the call the child was waiting to make, so its job ends as before and the parent decides.
6. **Crash-loop guard.**
   The job's adapter counts its continuations (`restart_continuations`), saved with the job before each launch, so the count survives further restarts.
   A job continued after `_RESTART_CONTINUATION_LIMIT` (2) restarts is not continued again and ends as before, interrupted with "send a follow-up".
7. **Nesting.**
   A child's own subagents run inline inside the child's job, so the restart settles them as before.
   A child that was waiting for a nested subagent holds a paused run, which recovery cannot turn into history, so its job is not continued and ends as before.
8. **Durability.**
   The saved job decides: its status `running`, its adapter's child and count, and the child's handle, whose current turn the restart settled.
   The pass saves the adapter with the follow-up child and the new count before the job's task starts, and the follow-up reserves its turn on the handle only against the settled turn it was prepared from.
   A second restart before that save finds the same state and prepares the continuation again; one after the save and before the reservation finds a prepared child that never ran and prepares a new one from the same settled turn; one after the reservation finds a running turn and continues it as the next continuation, so no window loses the continuation or runs a turn twice.
9. **No config option.**
   The behaviour is unconditional, as replies always resume (`defaults.auto_resume_after_restart` was retired).

## Rejected

- A new job linked to the old one: the old job would end, which needs a way to suppress its notice and follow-up, a link both ways, and a durable record for the window between the old job's end and the new job's acceptance.
- Reopening an ended job at the next start: it breaks "an ended job never runs again", and a parent reply still finalizing during shutdown could read the interrupted outcome first.
- Launching from `ToolJobRuntime.recover`: it runs before room-backed reply grants resolve, so deployments that grant access by room membership would refuse every continuation.
- Having the parent's follow-up turn call `continue_subagent`: it spends a parent turn per job and depends on the model doing it.
- Continuing a child that awaited approval or a nested subagent: both hold a paused run that history never replays, and the first also needs its denied card reconciled with a new one.
- A new child status or a marker file for "cut short by a restart": the handle already records it as the restart settlement, which only restart recovery writes.

## Delivery

One backend PR: the runtime keeps and relaunches stopped jobs, `delegation/continuation.py` prepares the continuation, the coordinator's pass drives it, the turn-start job note names continued jobs, and the docs and fuzz model follow.

## Testing

- Through the real runtime with scripted models: a child cut short after a tool call continues after an orderly restart and after a crash, its job completes with the child's answer, the parent got no ended notice in between, and the child's first continuation request extends the cut-short run's messages unchanged.
- A child cut short before its first tool call gets its task again; a cancel-requested job, a stopped reply's job, a job awaiting approval, a child waiting on a nested subagent, and a child that completed before the stop are not continued.
- The third restart of one job ends it as before; a restart between the adapter save and the reservation neither loses nor doubles the continuation.
- The resumed parent turn's job note names the continued job.
- `tests/test_delegation_job_fuzz.py` models continuation: a restart-stopped running child may run again only as a continuation, effects still run at most once, and every job settles.
