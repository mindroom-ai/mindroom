# Background Tool Jobs

This page records the decisions, invariant purposes, evidence, and limits behind background tool jobs.
What the feature does and how to configure it is on [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Decisions

- A reply never waits for background work: it ends with its answer while its jobs run on, so the reply lifecycle, reply tables, and delivery stay as they are without jobs.
- Only a newer human message releases a tool's wait; scheduled turns, other agents' messages, and follow-ups queue behind the reply instead of detaching its work.
  No time limit stands in for that message in scheduled, hook, or follow-up replies, because where a turn came from does not tell whether a person can release it: router relays, follow-ups, and silent schedules each broke that guess; a reply that waits on a call that never ends shows its job for cancelling.
  A run whose request carried the pause notice for that message runs no further call: calls its model makes anyway end the run unrun, because each gated call would otherwise become a job with a new card at once, for as long as the model kept calling; a subagent the paused batch runs keeps working until its own request carries the notice.
- With `mid_turn`, a wait and the post-batch check share one judgment per queued snapshot, so each queue gets one decision and at most one defer reaction, and a message the judge lets wait never moves a call to the background.
- A reply whose running calls are all `job` waits releases them for a queued human message without asking the mid-turn judge, even over an earlier decision to let that message wait: that decision weighed the work running then, and a person otherwise sat in silence for minutes behind jobs computing nothing for the reply.
  The release records the queue as one that cannot wait, so the post-batch check sends the usual pause notice and the run's later calls end unrun, as after a judged release, instead of the model waiting and being released again.
  Job admission wraps every call of a reply that offers job controls, so the count covers each running call, and a `job` wait that admission did not count is never released this way.
- After a Stop on a reply, no job of that reply gets a new approval; a call approved before the Stop counts as started, so it finishes and stays cancellable from the jobs bar like any running command.
  A job waiting on its cards is cancelled at once and its cards end denied, saying the reply was stopped; any other job of the reply, such as a subagent still working, is cancelled when it next asks, card or not, policy or person; and a call of the stopped reply that has not become a job yet never launches one.
  All jobs keep running after a deletion or an edit of a finished reply, because each job stays visible and cancellable on its own; a job otherwise ends only through its own cancel, revoked access, or the bot leaving the room.
  An edit that regenerates a reply still answering stops it the same way first, so the old question's calls get no approval either, while the regeneration's own calls do.
  A regeneration keeps its reply's record, so a later Stop of the regenerated answer also reaches the earlier answer's jobs, as the cards of both name the same reply.
  The Stop cancels such jobs instead of only denying their calls, because a subagent job told its child's call was denied would resume the child and keep working.
  Jobs are found by the reply record whose call started them, not by the reply event their cards name, because a reply may not show an event yet when its call starts a job; the link is saved with the job, because a subagent job outlives a restart and its reply's Stop must still reach it.
- An ended job reaches the agent as a notice it acknowledges with `collect`, and gets a follow-up only when no reply collected it, so results arrive without a message per job.
  Work that follow-ups start can chain at most 20 follow-ups without a human message, so follow-ups cannot keep each other going.
  A follow-up gives way to its requester's messages already waiting in its conversation, because the reply to one reports the jobs in its start note; answering first kept that message waiting and made the follow-up stop for a message older than itself.
  A cancellation gets no follow-up, since the person who cancelled already knows and no cancel, a Stop included, may make the agent speak; a silent schedule's jobs get none, so the schedule stays silent.
  The next reply still collects a cancellation like any ended job, so the agent learns that a call its reply showed as started never completed.
- A gated call that can become a job asks for its approval as the job's first phase, so a pending approval never holds the conversation.
  Its cards belong to the job alone, so no reply pauses for them and no stale card can resume newer work.
  They bind timed approvals to the same scope as a paused reply's cards, so one grant covers a conversation's gated calls whether they run as jobs or pause the reply.
  A card for the reply's own call names that reply's event, as a paused reply's card does, so a client links it to the reply that made the call; the reply's trace does not name the job instead, because its public entries carry no call identity and show a call as started before its job exists.
  A card for a call made before its reply showed an event names none, because waiting for that event could hold the card as long as the reply waits for its decision.
- Jobs are the only way long work runs in the background, so the shell stops a command at its `timeout` instead of handing back a handle of its own.
  A job's command has no default limit and any other command has 120 seconds, so a command outside a job always stops by its timeout; Desktop shell keeps its handles because each request crosses the Desktop bridge under its own deadline, and script runs keep the engine to host processes that outlive replies and restarts.
- A shell call that sets no `background_after` waits as long as a command outside a job may run, 120 seconds, and then continues as its job.
  A model cannot always tell how long a command runs, and a command that prints nothing until it ends gives a waiting reply nothing to show, so without that limit one misjudged command held its conversation until a person wrote again.
  Time its job spends awaiting approval does not count, so `approval_wait_timeout` still decides how long a reply waits for a decision.
  Other tools keep waiting until completion, because their callers usually need the result to answer.
- A worker `/execute` call streams its progress and a keep-alive every 5 seconds before its result line, so a job's call can run past every request timeout, a silent connection means the runner is gone, and a primary that stops reading stops the call.
  A call that is not a job keeps its limits, which the primary checks as lines arrive; persistent browser actions keep one response, because cancelling one closes the shared browser.
- A request says whether it runs as a job, and the runner binds that job in its own process and in per-request children, so a tool sees the same ownership wherever it runs; a `mindroom-agent` call inherits it from its command's window.
- Progress is the latest report on the runtime entry, never saved and shown only while the job runs, so a report from a thread that outlived its call cannot change a settled job.
- A command's `mindroom-agent` calls are the agent's own actions, so the command's `timeout` stops its processes but never those calls, and its shell call waits for them; a time limit on them would cancel a call a person approves late, and nothing has to tell a stopped command from one that ended.
- The response's `mindroom-agent` owner closes through the response's execution resources, so a shell job that outlives its reply keeps the CLI until the last job settles; cancelling one shell window stops only that window's calls.
- A saved job result decodes only to text, media, session-state changes, and an error under fixed wire tags, so saved data never chooses the code that runs; a streamed call's job saves the joined text the model read, not its events.
- Recovery fails on a saved job it cannot read and names that job, instead of dropping it silently.
- A subagent call that can become a job is an ordinary managed call: the reply resolves, authorizes, and prepares its child without writing anything, so its job is known as that child from its start, in the jobs bar, listings, and handles, and is granted by the caller's delegation to that child; the job then starts, runs, and settles the child and asks for the child's approvals as its phases.
  The native driver, which pauses the parent run for each child approval, remains only where calls cannot become jobs: background jobs off, minimal mode, nested children, and an excluded `delegate` toolkit.
  A driver that started its own job and continued the paused parent with the result duplicated the generic start, wait, detach, claim, and record steps, and kept a parent pause and continuation per background subagent.
- A subagent call an approval rule gates still pauses the reply for its own approval and becomes a job once approved, so a subagent job's `awaiting_approval` is always its child's and a restart never reads one as the other.
- A subagent job a restart stops while its child works does not end: it stays running under its job ID, and the runtime continues the child with a follow-up turn of its own, because a restart is not a decision about running work and a parent told of an interruption reported it to the user instead of continuing (see `docs/dev/2026-11-17-subagent-auto-continue-design.md`).
  Nothing ended, so no notice or follow-up reaches the parent until the continuation's real outcome does, and a parent waiting on the job reads that outcome.
  A tool's job still ends interrupted, because a call cannot continue and running it again could repeat its effects.
  The worker's pass launches the continuation, not recovery, which runs before the first sync; the launch is the one `continue_subagent` uses, so the caller's delegation and the requester's access are checked again.
  A stopped job has no task for a while, so every fact that decides whether it goes on is read from a durable record by the start that would continue it, not kept beside the job.
  The pass launches only once the job's bot has completed its first sync, because by then the bot's client holds the rooms it is in and room-backed reply grants can resolve; a bot that is only late keeps the job waiting, and one whose start failed for good ends it interrupted, so no job waits for a bot that will not come.
  A synced bot that is not in the job's room left it, so the job ends interrupted: the leave may have been seen by a process that had jobs off, which cancelled nothing.
  Access that stays unresolved under a synced bot, as a grant room the control client left does, ends the job interrupted after 10 minutes, a fixed bound and not an option; access proven gone is cancelled as revocation, and a bot that has not synced, as while Matrix is unreachable, keeps the job waiting without a bound.
  The child is sent `SUBAGENT_RESTART_CONTINUATION_PROMPT`, what the cut-short turn was sent, and an account of its cut-short run's calls, the one a replayed reply's prompt gets of its stopped attempt, after its history; what the turn was sent and the account are each bounded.
  Both are in the prompt because the child's history limits apply to a recovered run as to any turn and may leave the task and the calls out; the stored history is unchanged and the prompt adds one user message, so a provider's prompt cache still matches whatever history the request replays.
  A turn that called no tool left nothing in history to continue from, so it is sent again what it was sent: a subagent's turn its task, and a continuation its prompt, so a second restart before the continuation's first call loses none of the account.
  The prompt is built in one place, once, when the continuation is prepared, and is saved in the job's adapter, which no room and no model reads.
  The continued turn's task, which the job and the Sub-agents panel show whatever the tool previews are set to, stays the cut-short turn's: the account holds results of earlier calls and is the child's input only.
  The job hands the prompt to its own child's first response and to nothing else, so a subagent the continued turn starts later is sent its own task.
  A job that was waiting for a person, on an approval card or another human decision, ends instead, because the restart denies its card and continuing would ask again about a call the person saw denied or go on without it; so does a job continued after two restarts already, in case continuing it is what stops the process.
  A wait for a person that cancellation unwinds therefore leaves the job saved as waiting, so an orderly shutdown, an event-loop teardown, and a crash all read the same status.
  A Stop of the reply that started a working subagent lets it work on, as before, and no restart continues it: the reply's record holds the Stop, the job saves which reply started it and the Stop that reply had recorded by then, and recovery reads the reply's record for each subagent job the restart stopped, so a Stop the process did not live to apply to the job counts, and a Stop from before a regeneration that started the job does not.
  An orderly shutdown ends a job whose reply it saw stopped instead of keeping it, and a job that a Stop marks while it waits to continue ends when the pass comes to it.
  Such a job's result says that its user stopped it and that it is not to be continued unless the user asks, because the interruption's own reason invites a follow-up and the parent that read it continued the child itself; the runtime puts that result where it publishes a job's end, for every interruption of a subagent job whose reply was stopped, so a child that waited for an approval when the restart came ends with it too.
  A continued job is a job of its reply again, held to the rule a live job is held to and no stricter one: a Stop that lands while a continuation launches has the effect a Stop has on a working job, which is refused its next approval, and which the next restart does not continue.
  Nothing orders a Stop against the launch more finely than that, because each such check left another order of the Stop's record, its effects, and the launch's save to close.
- A subagent job's approval cards name the child's turn with the provider's call ID (`child_card_id`), under the job as their owner, because a continued job keeps its ID over several turns of its child and a provider may use a call ID again in a later turn: a decision then answers only the turn that asked.
- A follow-up waits until the job of the turn it continues has ended, not only until that child settled, because a child's turn settles before its job delivers the answer; a follow-up that took the handle in between would leave the earlier job's recovery without the turn it ran.
- A job whose own call returned its result inline is kept one day instead of 30, because once it ended no listing, notice, or follow-up names it, so after its turn only a reread by its exact ID reads it; the day is a margin for such a reread, as by an ID a person saw in the jobs bar while the call ran.
  On a busy deployment about 99% of the stored result bytes belonged to such jobs, mostly images and shell output.
  Clearing the payload when the result is acknowledged was rejected: the job would stay in memory and in the table, and a reread by ID would find no result.

## Invariant purposes

- Execution lifetime is independent of any caller's wait, so a released wait never stops work.
- Each outcome has one active result claim, and only the parent run's saved tool call acknowledges it, so a result is read once and a reply that fails before saving leaves it to be reported again.
- Cancellation is saved before cleanup and stays pending until owned work settles, so a crash during cleanup still reports the job cancelled.
  Only work that a shutdown, restart, or event-loop teardown cut short is interrupted, and a child settlement saved before a crash stands.
- A job's cancel request denies its open approval cards in the commit that saves the request, and its end denies any card a request still in flight posted after that, so no card of a stopping or ended job stays answerable, even while the card's sender cannot post the denial yet.
- A job-gated call never runs inline: one that cannot become a job fails, and the agent CLI never offers it, because such a function asks for no confirmation of its own and its job's approval phase is its only approval gate.
- A gated job durably leaves `awaiting_approval` before its call runs, so a stop says a call did not run only when it never started.
- A reply's Stop is checked where a job asks for or takes an approval, not where the job stands when the Stop arrives, so no order of the Stop and the job's own awaits lets an approval through.
  The Stop marks the reply's span this instance runs before anything else, and the reply's jobs before it waits for the runtime lock.
  A job checks its mark synchronously right before requesting a card and after saving that it took a decision, so a denied decision also cancels it; admission reads the span's mark after its last await and cancels an accepted job unlaunched when the span was stopped.
  The span's mark lives as long as the span's scope, which outlasts every admission its calls make; a job keeps its own mark.
  A cancel request the Stop could not save leaves the job marked, so approving its card later runs nothing and saves the cancel then.
  An approval recorded just before the Stop refuses the job can keep its card reading approved: a decision is final once recorded, and the Stop cannot deny the card in its own commit because the reply's record does not name its jobs.
- Revocation stops execution through internal ownership although discovery and control are no longer authorized, so losing access never leaves work running unseen.
  A config reload keeps active jobs, and controls and result reads check current authorization.
- A follow-up is named by its conversation and the set of jobs it reports, so admitting it again changes nothing.
- An ended job is deleted only once the turn that started it has finished, answered or not, and no approval in its session is pending, so a deleted job's call can never run again.
- A starting process takes the saved jobs over before dispatch starts, and the journal refuses every later write of the runtime it replaced, so two processes never write one job.
- A reply that a process stop cuts short continues in place by journal replay, below what the stopped attempt showed, with an account that names the finished calls it must not repeat (see [Bot Runtime](bot-runtime.md)).
  A detached job start is such a finished call, and its result names the job ID.
  Tool jobs the stopped attempt left running are interrupted by the restart, and their outcomes reach the conversation like any other ended job.
  Its subagent jobs continue, and the job note of the turn that started one says so, because a call that was still waiting for its job left that turn no handle and its recovered run only says the call was interrupted.
- A continuation is saved with its job, as the job's child, the prompt it starts on, and its count of continuations, before it starts, and reserves its turn only against the turn the restart settled, so a second restart at any point neither loses the continuation nor runs a turn twice.
  Only a job that saved such a count may continue from the turn before its child's, which is the window between that save and the reservation; a manual follow-up's job names a previous turn too, and continuing it there would run the earlier task again.
  A subagent is busy for follow-ups while any unfinished job holds it, matched by the subagent and not by the turn, because a continuing job names its next turn before the handle does.
  A resumed approval batch saves a checkpoint before each approved call, since those run one after another, and recovery answers a denied call with its recorded denial, so only the call that was running reads as interrupted.
  The turn a restart settled is told apart from one that failed on its own by the reason only that settlement records, and its job's interruption is what the runtime may continue; any other end of the child is its job's end.
  An interrupted outcome tells the model that a call with side effects may already have taken effect and must not run again, while a read-only call can run again for its lost result.

## Evidence

- `tests/test_tool_job_fuzz.py` and `tests/test_delegation_job_fuzz.py` generate interleaved job and subagent lifecycles, including failed saves, client cancels, restarts, and crashes.
- With an empty result for a call cancelled while its reply waited, `gpt-6-luna` told the user a cancelled deploy had succeeded, so runtime-authored cancelled outcomes carry a reason; neither `gpt-6-luna` nor `gpt-6.1-sol` called the tool again with either text (12 runs per variant).
- `background_after` and the earlier name `wait_timeout` led `gpt-6-luna` and `gpt-6.1-sol` to the same choices, `0` for a request to run in the background and `null` for a quick result (12 runs per name), so the name that says what the value does was kept.

## Limits

Cancellation cannot undo remote side effects or forcibly stop arbitrary Python threads.
Excluded tools retain native behavior.
Nested tools stay within their outer execution owner.
Subagent follow-ups use reusable sessions after the previous child turn finishes; injecting instructions into a running child is outside scope.
Subagent calls of one model step that run as jobs run at the same time, as other managed calls do; of two follow-ups to one subagent in that step, the one that finds the other's turn running fails as busy.
An earlier subagent job whose outcome save failed, or that waited while background jobs were turned off, can report the attempt its saved child names, or an interruption, instead of its final answer, when a follow-up advanced its handle before a crash or a later start recovered it; its child never runs twice.
A notice reaches only a model step after a tool batch; a reply that ends without another tool call leaves the job to its follow-up.
A follow-up in a session switched to minimal mode runs and reports results it cannot acknowledge, so a later standard reply may report them again.
A follow-up whose turn ends without collecting its jobs is not admitted again in the same process; they wait for the next reply's note.
A follow-up also gives way to a waiting message the agent then leaves unanswered, such as one addressed to another agent, and its jobs wait for the next reply's note.
Another person's waiting message does not hold a follow-up back, because that person's reply lists only their own jobs, so the message still waits for the follow-up and stops it as a newer message would.
A message is waiting only from its arrival during a busy turn until its own turn's dispatch starts, so a follow-up that takes an idle conversation while a message is still being prepared stops for it as for a newer message.
A bot that left a room cannot clear its jobs state there, so clients ignore jobs state from users who are not joined.
An orderly shutdown's last write of the jobs state drops the jobs it ended and keeps listing, as they were, the subagent jobs it keeps running for the next start to continue, so their threads still show work through the restart; when the config has turned background jobs off for that start, it drops those too.
Jobs turned off while MindRoom is down leave the kept jobs listed until a start with jobs on, as a crash before a start with jobs off does.
A running job its call still waits for enters its room's jobs state only after it has run 5 seconds, at the worker's next periodic pass, normally within about 5 seconds, because every call of a reply is a job, most end within seconds, and clients keep every state event; a job awaiting approval or a cancel, one its call left running in the background, and one already listed are listed at once.
A restart interrupts a job that waits for its own call's approval, so that call never runs, and the model is told it did not run.
A subagent job is not continued, and ends interrupted for a follow-up to continue its child, when the restart found its child waiting for an approval or for a subagent of its own, whose paused run history never replays, or stopped it before its child's first turn was reserved.
A crash in the moment between an approval's decision and the checkpoint that precedes the approved calls also finds the paused run, so that job ends interrupted too; none of the approved calls had started.
An approved call of a resumed batch that had not started when a crash stopped an earlier one is answered as interrupted too, since the run does not record which approved calls began.
A job the worker's pass declines to continue logs why, with its job ID; a job the restart itself ends, such as one that waited for a person or had a cancel request, ends as every interrupted job does, without that line.
A continued child's rows in the Sub-agents panel carry no tool previews, because the conversation's rebuilt tool context does not know whether the reply that started it showed tool calls.
A continued child cannot open the attachments of the turn that started it, because the rebuilt tool context carries no attachment IDs and neither the job nor the child saves them.
A subagent job waits to continue while its bot has not completed its first sync, so it stays running while Matrix is unreachable; the turn that started it is told MindRoom continues it or ends it, not that it already runs.
The 10 minutes a job waits for unresolved access count from the first pass of the current process that found it so, so each restart starts them again.
Within one turn of a child, a job's approval cards are still keyed by the provider's call ID alone, so a child that pauses twice in one turn on a call ID its provider reused is answered with the earlier card's decision.
Tool hooks see a continuation as a `continue_subagent` call whose message is the cut-short turn's task.
What a cut-short turn was sent is cut at 8,000 characters in the continuation prompt, which for a continuation cut short after its own calls is the prompt it was sent, the earlier run's account included.
A requester's access that turns unresolved while a continuation is being saved leaves the job waiting with that continuation counted, so restarts may end the job one continuation sooner; it never runs without access.
A bot that left the job's room and rejoined it while MindRoom was down counts as joined.
A saved job stays readable only while its tool's registered factory keeps the module and name the job was built from, so moving or renaming a factory makes that tool's saved jobs unavailable after the upgrade.
Only functions of toolkits MindRoom assembles become jobs; the knowledge search, skill access, and team delegation functions MindRoom adds beside those toolkits execute inline.
A wait that ends while the mid-turn judge is still deciding cancels that judgment, so the post-batch check judges the same queue again.
A primary that dies without closing its connection leaves a worker command running until the runner notices the dead connection, and a local shell job whose process is killed with SIGKILL keeps running.
A shell job's `mindroom-agent` calls fail once its response's CLI grant reaches its 24-hour cap, which bounds how long a leaked grant stays usable.
The jobs state event carries no progress; a subagent job's child is shown live in MindRoom Chat's Sub-agents panel through `io.mindroom.subagents` frames, which are separate from the job, and the job's state names the child's `subagent` and `run` so a client can find those frames.
