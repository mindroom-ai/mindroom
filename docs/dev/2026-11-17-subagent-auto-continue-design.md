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
   A job stopped before its child's first turn was reserved on the handle has no settled turn to continue from, so it also ends as before.
   A child an approval resumed is continued like any working child: a live test showed that a crash during its approved call left the paused row, because an approved run's resume saved no checkpoint, so a job's child now checkpoints its approved resume, first before the approved calls run (`ResumeRunner.checkpoints`).
   A paused reply's approval keeps settling from its paused row, and its replay is told its recorded calls in prose; a child has no such record of its calls, so the checkpoint is its only account of them.
   A subagent job that a restart stopped and that is not continued now ends with the job status `interrupted`, as a tool's job does, where it ended `failed`; its result text is unchanged.
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

## Round 2 plan

This section supersedes the decisions above where they differ: the saved Stop mark, the fallback to the previous turn, and the single checkpoint of a resumed batch.

Three reviews of `e327acb2b` found about ten defects; this section groups them by cause and says what changes.
The identity stays: all three reviews keep "the same job stays running", its saved count, the serial worker pass, and the reservation against the settled turn.

### Root cause of groups A to D

A stopped job that stays `running` has no task for a while, and round 0 let four facts about that state live in places a restart loses or a later step does not read.
The fix is one rule: every fact that decides whether a stopped job goes on is read from a durable record at the moment the job would go on, and the runtime and the worker pass share one predicate each for "may stay running" and "may launch now".

- **A, a Stop.**
  The reply record already holds a Stop durably and is the authority for it (reply-messages I-S1); the saved `reply_stopped` mark was a second copy written after the Stop's commit, so a crash between the two lost it, and a restored job no longer knew its reply.
  The job now saves the reply that started it (`reply_id`) and the Stop that reply had recorded by then (`reply_stop_order`), and the saved mark is deleted.
  The worker pass reads the reply before it launches a continuation and ends the job when a Stop was recorded since the job began; a reply stopped and regenerated before the job began does not count.
  In the process that receives a Stop, the job's control carries it as before: shutdown ends such a job instead of keeping it, a job waiting to continue ends at once, and a launch that races the Stop is refused under the runtime's lock.
  A continued job is linked to its reply again, so a Stop refuses its approvals as before a restart.
- **B, the subagent handle.**
  The fallback to the previous turn exists for one window, after a continuation saved its prepared child and before it reserved the turn, so it now applies only to a job that saved a continuation count; a manual follow-up's job stopped before it reserved its turn ends interrupted, as the design says.
  A follow-up is refused as busy while any unfinished job holds the subagent, by the subagent's stable ID instead of the attempt's, which also covers a job that waits to continue.
- **C, a job waiting for a person.**
  A wait for a human decision that its task's cancellation unwinds leaves the job saved as `awaiting_approval`, as an approval-card wait already does, so shutdown, teardown and a crash all read the same status and none continues it.
  With that, `_settle` needs no `keep` flag: it applies the one predicate.
- **D, readiness.**
  The pass launches only when the job's bot runs and has completed its first sync, which is when it has processed the leaves it missed and cancelled the jobs of rooms it left, and when the grant is `allowed`; the runtime checks the grant again under its lock at the launch.
  A bot whose start failed for good (`EntityFleet.is_disabled`) is the terminal case: its job ends interrupted with a logged reason.
  A removed agent or revoked access was already cancelled by the pass's revocation step, which runs first; a bot that is only late keeps the job waiting, which is the temporary case.
  Each job is handled inside its own guard, so one job that raises does not stop the pass.

### The other groups

- **E, history limits.**
  The continuation prompt carries a bounded account of the cut-short run's calls, rendered by the formatter a top-level replay's prompt uses (`render_stopped_attempt`), from previews, so a child whose history limits leave the recovered run out still reads which calls finished and which was cut short.
  History treats the recovered run as before; the cache claim becomes "the stored history is unchanged and the prompt adds one user message".
- **F, a resumed batch.**
  A resume saves its checkpoint before each approved call instead of once, since approved calls run one after another: the calls before it hold their results, and recovery keeps them.
  Recovery answers a call its approval denied with the denial the run recorded.
  This removes the precomputed plan round 1 added to the loop.
- **G, approval keys.** Declined, see the report: the key is the job's and the call's on main too, where one child turn that pauses twice on a reused call ID already shares it, and the fix is a pause-unique card identity for every job, not a rule for continuations.
- **Minors.**
  The job note reads the continuing jobs before the ended ones and drops those that ended meanwhile, and lists only jobs whose access is allowed and that no cancel was requested for.
  Shutdown's last publication leaves the kept jobs out of the rooms' state, since the next start may have jobs off.
  (Since changed: it keeps them listed unless the config has turned jobs off; see `docs/architecture/background-tool-jobs.md`.)
  The continued child's lost turn attachments are documented.

### Size

Estimated source change: about +110 −50 (runtime −5, coordinator +35, continuation +25, loop −6, recovery +5, notices +8, others +5), well under the 250-line stop line, so the round is implemented.
Measured after implementation: +189 −105 in `src`, net +84.

## Round 3

Two reviews of `87759d6d3` left five items; the identity and the round 2 rule stand, and each fix is at the place the fact is decided.

- **A, a Stop during the save before the launch.**
  The launch read the Stop mark and the access before it saved the continuation, and the save awaits.
  It now reads both after the save, with no await before it creates the task.
  A Stop's effect sets the mark as its first step, without the lock, so the two orders are the only ones: the mark is set before the task exists and the job ends, or the task exists and the Stop finds a working job, which it leaves working as before this change.
  The reply's record is still read once, by the pass before it prepares the continuation: it holds the Stops of earlier processes, and every Stop of this process sets the mark.
- **D, a room the bot left, and access that never resolves.**
  A synced bot's client holds the rooms it is in, so the pass reads the job's room there and ends the job when the bot left it.
  Unresolved access under a synced bot ends the job after a fixed 10 minutes, counted in the coordinator from the first pass that found it so; a reply waits for such a grant too, and a job without a task cannot wait forever.
- **E, a one-message history limit.**
  The prompt states the cut-short turn's own task, bounded, beside the account of its calls.
  The continued turn keeps that task as its own, so a second restart finds it again without a second copy saved anywhere.
- **The account in the room.**
  The turn's task is its label in the job and the Sub-agents panel, so the account can not be part of it: the continued turn's task is the cut-short turn's, and the response envelope sends the prompt in its place.
  This supersedes the round 2 text that put the account into the turn's task.
- **G, approval keys.**
  No longer declined: on main a follow-up is a new job with its own cards, and keeping the job over several child turns removed that boundary.
  A subagent job's cards name the child's turn with the call (`child_card_id`), under the job as owner, so the job's end still denies them; the child resumes on the provider's own call IDs, since decisions are matched to calls by position.
  Reuse within one turn is main's behaviour and stays a limit.

- **A stopped reply's job is not continued by its parent either.**
  The live recheck found the parent's job-ended turn continuing the child after the user's Stop, because the job ended with the restart's reason, which says to send a follow-up.
  Such a job now ends with a result that says its user stopped it; the runtime chooses it where it publishes a job's end, so the shutdown, a Stop of a waiting job, and the pass share it, and the pass hands the runtime a Stop it read in the reply's record.

Measured: +136 −36 in `src` against `87759d6d3`, net +100.

## Round 4

An audit after round 3 found the change at +589 −51 in `src` against an estimate of about 300, and the findings of rounds 1 to 3 in two places: Stop handling, where each round closed one order of events and the next review found another, and the continuation prompt.
This round rules on both and supersedes the Stop and prompt text of rounds 2 and 3 where they differ.

- **R1, a Stop has the effect it has on main.**
  Rounds 2 and 3 held a continued job to a stricter rule than a live job: main cancels a job waiting on its cards at once, and any other working job, a subagent included, only when it next asks for an approval.
  A continued job is a job of its reply again: it keeps the saved link to the reply, so a Stop marks it and it is refused its next approval.
  One durable check stays, moved to where it serves every ending: recovery reads the reply's record for each subagent job the restart stopped, and a job whose reply recorded a Stop since the job began ends there, with the result that says its user stopped it.
  A job a Stop marks in the process that received it ends as before: at that process's shutdown, or when the pass comes to it while it waits to continue.
  The rule for everything else: a Stop that lands while a continuation launches has the effect a Stop has on a working job.
  Deleted: the Stop check after the launch's save, the end of a waiting job from inside the Stop's own effect, the pass's read of the reply's record, and the flag by which the pass told the runtime of a Stop.
- **R2, one place builds the continuation prompt.**
  `_continuation_prompt` builds it once, when the continuation is prepared, and it is saved with the job (`continuation_prompt` in the adapter).
  The ruling asked for the prompt to be the continued turn's saved task; it is saved beside the task instead, because the task is the turn's label in the room (round 3, "The account in the room") and the account must not show there.
  A continuation a restart stops before its first call is sent the saved prompt again, so its account of the earlier run is not lost; one stopped after its own calls is sent the note, the prompt it was sent, and the account of its own run.
  The nested-child defect had one cause: the prompt replaced the task inside the response envelope (`_sending`), and the delegation driver hands that envelope to every child started under the turn, where "the prompt equals the task" is true of each nested child's first response.
  The envelope wrapper is gone: the job passes the prompt to its own child's first response (`delegation_call_job(prompt=...)`, down to `_execute_child`), which no other child reaches and which compares nothing.
- **R3, a child that awaited approval.**
  A crash between a Stop's record and its effects left a waiting child's job, which recovery ended with the restart's reason, "Send a follow-up".
  It now ends through the same path as every other Stop ending, since recovery reads the reply's record before it settles the job (R1).
- **R4, accepted limits**, documented in the architecture's Limits and not coded: a grant that turns unresolved during the launch's save uses up that continuation; a bot that left and rejoined the room while down counts as joined; the 10-minute access wait starts again in each process.
