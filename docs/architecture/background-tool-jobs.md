# Background Tool Jobs

This page records the decisions, invariant purposes, evidence, and limits behind background tool jobs.
What the feature does and how to configure it is on [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Decisions

- A reply never waits for background work: it ends with its answer while its jobs run on, so the reply lifecycle, reply tables, and delivery stay as they are without jobs.
- Only a newer human message releases a tool's wait; scheduled turns, other agents' messages, and follow-ups queue behind the reply instead of detaching its work.
  No time limit stands in for that message in scheduled, hook, or follow-up replies, because where a turn came from does not tell whether a person can release it: router relays, follow-ups, and silent schedules each broke that guess; a reply that waits on a call that never ends shows its job for cancelling.
- With `mid_turn`, a wait and the post-batch check share one judgment per queued snapshot, so each queue gets one decision and at most one defer reaction, and a message the judge lets wait never moves a call to the background.
- A Stop on a reply also cancels the reply's jobs that await approval, so approving one of their cards later cannot run a call the person stopped; the cards end denied, saying the reply was stopped.
  An edit that regenerates a reply still answering stops it the same way first, so the old question's waiting calls end too.
  The reply's jobs whose call already passed its approval keep running, as do all jobs after a deletion or an edit of a finished reply, because each job stays visible and cancellable on its own; a job otherwise ends only through its own cancel, revoked access, or the bot leaving the room.
  A regeneration keeps its reply's record, so a later Stop of the regenerated answer also ends the earlier answer's jobs that still await approval, as the cards of both name the same reply.
  The Stop cancels such jobs instead of only denying their cards, because a subagent job told its child's call was denied would resume the child and keep working.
  Jobs are found by the reply record whose call started them, not by the reply event their cards name, because a reply may not show an event yet when its call starts a job; the link lives only in memory, because a restart interrupts every job that awaits approval.
- An ended job reaches the agent as a notice it acknowledges with `collect`, and gets a follow-up only when no reply collected it, so results arrive without a message per job.
  Work that follow-ups start can chain at most 20 follow-ups without a human message, so follow-ups cannot keep each other going.
  A follow-up gives way to its requester's messages already waiting in its conversation, because the reply to one reports the jobs in its start note; answering first kept that message waiting and made the follow-up stop for a message older than itself.
  A cancellation needs no acknowledgement and gets no follow-up, since the person who cancelled already knows, and a silent schedule's jobs get none, so the schedule stays silent.
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
- A follow-up waits until the job of the turn it continues has ended, not only until that child settled, because a child's turn settles before its job delivers the answer; a follow-up that took the handle in between would leave the earlier job's recovery without the turn it ran.
- A job whose own call returned its result inline is kept one day instead of 30, because once it ended no listing, notice, or follow-up names it, so after its turn only a reread by its exact ID reads it; the day is a margin for such a reread, as by an ID a person saw in the jobs bar while the call ran.
  On a busy deployment about 99% of the stored result bytes belonged to such jobs, mostly images and shell output.
  Clearing the payload when the result is acknowledged was rejected: the job would stay in memory and in the table, and a reread by ID would find no result.

## Invariant purposes

- Execution lifetime is independent of any caller's wait, so a released wait never stops work.
- Each outcome has one active result claim, and only the parent run's saved tool call acknowledges it, so a result is read once and a reply that fails before saving leaves it to be reported again; a cancellation, which has no result to read, is acknowledged when a notice names it.
- Cancellation is saved before cleanup and stays pending until owned work settles, so a crash during cleanup still reports the job cancelled.
  Only work that a shutdown, restart, or event-loop teardown cut short is interrupted, and a child settlement saved before a crash stands.
- A job's cancel request denies its open approval cards in the commit that saves the request, and its end denies any card a request still in flight posted after that, so no card of a stopping or ended job stays answerable, even while the router cannot post the denial yet.
- A job-gated call never runs inline: one that cannot become a job fails, and the agent CLI never offers it, because such a function asks for no confirmation of its own and its job's approval phase is its only approval gate.
- A gated job durably leaves `awaiting_approval` before its call runs, so a stop says a call did not run only when it never started.
- A reply's Stop ends its jobs whose call has not passed its approval yet, card posted or not, in whatever order the Stop and the jobs' own steps run.
  The runtime tracks that in memory: accepting such a job as `awaiting_approval` was rejected, because a call the policy approves would then count its policy check against `approval_wait_timeout` and could leave its reply early.
  The Stop marks those jobs in memory before it waits for the runtime lock, and a marked job posts no card, passes no approval, and enters no tool, so a decision that arrives meanwhile runs nothing.
  It looks again under the lock that accepting a job holds, and the stopped span accepts no job afterwards, so a call the span recorded before the Stop never becomes a job after it, whatever its task awaits on the way.
  A subagent job counts from its child's pause, before the policy decides the child's calls; a subagent whose child is still working keeps running.
  A cancel request the Stop could not save leaves the job marked, and the revocation pass retries the save, so a failed write never lets the call run.
- Revocation stops execution through internal ownership although discovery and control are no longer authorized, so losing access never leaves work running unseen.
  A config reload keeps active jobs, and controls and result reads check current authorization.
- A follow-up is named by its conversation and the set of jobs it reports, so admitting it again changes nothing.
- An ended job is deleted only once the turn that started it has finished, answered or not, and no approval in its session is pending, so a deleted job's call can never run again.
- A starting process takes the saved jobs over before dispatch starts, and the journal refuses every later write of the runtime it replaced, so two processes never write one job.
- A reply that a process stop cuts short continues in place by journal replay, below what the stopped attempt showed, with an account that names the finished calls it must not repeat (see [Bot Runtime](bot-runtime.md)).
  A detached job start is such a finished call, and its result names the job ID.
  Jobs the stopped attempt left running are interrupted by the restart, and their outcomes reach the conversation like any other ended job.
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
A restart interrupts a job that waits for its own call's approval, so that call never runs, and the model is told it did not run.
A saved job stays readable only while its tool's registered factory keeps the module and name the job was built from, so moving or renaming a factory makes that tool's saved jobs unavailable after the upgrade.
Only functions of toolkits MindRoom assembles become jobs; the knowledge search, skill access, and team delegation functions MindRoom adds beside those toolkits execute inline.
A wait that ends while the mid-turn judge is still deciding cancels that judgment, so the post-batch check judges the same queue again.
A primary that dies without closing its connection leaves a worker command running until the runner notices the dead connection, and a local shell job whose process is killed with SIGKILL keeps running.
A shell job's `mindroom-agent` calls fail once its response's CLI grant reaches its 24-hour cap, which bounds how long a leaked grant stays usable.
The jobs bar does not show progress.
