# Background Tool Jobs

This page records the decisions, invariant purposes, evidence, and limits behind background tool jobs.
What the feature does and how to configure it is on [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Decisions

- A reply never waits for background work: it ends with its answer while its jobs run on, so the reply lifecycle, reply tables, and delivery stay as they are without jobs.
- Only a newer human message releases a tool's wait; scheduled turns, other agents' messages, and follow-ups queue behind the reply instead of detaching its work.
- With `mid_turn`, a wait and the post-batch check share one judgment per queued snapshot, so each queue gets one decision and at most one defer reaction, and a message the judge lets wait never moves a call to the background.
- Stop, an edit, or deleting a message stops only the reply, because each job stays visible and cancellable on its own; a job ends only through its own cancel, revoked access, or the bot leaving the room.
- An ended job reaches the agent as a notice it acknowledges with `collect`, and gets a follow-up only when no reply collected it, so results arrive without a message per job.
  Work that follow-ups start can chain at most 20 follow-ups without a human message, so follow-ups cannot keep each other going.
  A cancellation needs no acknowledgement and gets no follow-up, since the person who cancelled already knows, and a silent schedule's jobs get none, so the schedule stays silent.
- A gated call that can become a job asks for its approval as the job's first phase, so a pending approval never holds the conversation.
  Its cards belong to the job alone, so no reply pauses for them and no stale card can resume newer work.
- Shell is excluded by default because it already has its own background commands and cancellation.
- Saved custom generator events replay as fixed SDK subclasses, so saved data never chooses the class or methods that run.
- Recovery fails on a saved job it cannot read and names that job, instead of dropping it silently.

## Invariant purposes

- Execution lifetime is independent of any caller's wait, so a released wait never stops work.
- Each outcome has one active result claim, and only the parent run's saved tool call acknowledges it, so a result is read once and a reply that fails before saving leaves it to be reported again; a cancellation, which has no result to read, is acknowledged when a notice names it.
- Cancellation is saved before cleanup and stays pending until owned work settles, so a crash during cleanup still reports the job cancelled.
  Only work that a shutdown, restart, or event-loop teardown cut short is interrupted, and a child settlement saved before a crash stands.
- Cancelling or restarting a job that waits for approval denies its open cards, so no card outlives the call it would approve.
- A job-gated call never runs inline: one that cannot become a job fails, and the agent CLI never offers it, because such a function asks for no confirmation of its own and its job's approval phase is its only approval gate.
- A gated job durably leaves `awaiting_approval` before its call runs, so a stop says a call did not run only when it never started.
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
A notice reaches only a model step after a tool batch; a reply that ends without another tool call leaves the job to its follow-up.
A follow-up in a session switched to minimal mode runs and reports results it cannot acknowledge, so a later standard reply may report them again.
A follow-up whose turn ends without collecting its jobs is not admitted again in the same process; they wait for the next reply's note.
A bot that left a room cannot clear its jobs state there, so clients ignore jobs state from users who are not joined.
When denying an ended job's approval cards fails and the process then stops before a retry succeeds, those cards stay answerable until their own deadline, and answering them does nothing.
A restart interrupts a job that waits for its own call's approval, so that call never runs, and the model is told it did not run.
Job cards offer no timed automatic approvals, and automatic approvals granted on paused-run cards do not apply to them.
Only functions of toolkits MindRoom assembles become jobs; SDK-generated knowledge search, skill access, learning, and team delegation run inline.
A wait that ends while the mid-turn judge is still deciding cancels that judgment, so the post-batch check judges the same queue again.
