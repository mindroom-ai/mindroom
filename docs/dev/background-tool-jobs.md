# Background tool jobs

This living design record defines the feature's scope and invariants.
User-facing configuration and examples are in [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Behavior

- Disabled by default.
  `background_tool_jobs.enabled` and `exclude_toolkits` are pinned at startup; changing execution policy requires a restart.
  With the feature off, startup builds no job runtime and leaves saved jobs untouched; the next enabled start recovers them, reporting unfinished ones as interrupted, and an approval continuation that needed a job fails on resume.
- Tools block by default.
  `wait_timeout: 0` detaches immediately; a positive value limits foreground waiting.
  Only a newer human message in the conversation releases that wait while accepted work continues; scheduled turns, other agents' messages, and follow-ups queued behind the reply wait for it.
  Releasing a wait neither pauses a job nor authorizes a protected tool.
- One `job` tool provides scoped `list`, `wait`, `cancel`, and `collect`; `cancel` asks a job to stop and returns at once.
  Complete registered toolkits can be excluded, including plugins.
  Shell is excluded by default and keeps its own execution and cancellation controls.
- A job belongs to its conversation and requester, and to the entity that answers there: the agent, or the team that owns the jobs its members started for it.
  That entity's `job` tool sees all of them; a team member sees only its own, and a team's leader has its own `job` tool.
- A reply never waits for background work: it ends with its answer while its jobs run on.
  Every model step of the answering entity hears once, on the batch's last tool result, which of the conversation's jobs ended since its last step, and a reply that starts after jobs ended hears of them in its prompt; both ask for one `job(action="collect")` call.
  Hearing of a cancellation acknowledges it.
  Ended jobs no reply collected get one follow-up per conversation and set of jobs: an internal `job_follow_up` journal source that starts an ordinary turn posting a new reply.
  Under the conversation lock, a follow-up whose jobs a reply already collected runs nothing, unless its own reply a restart cut short needs answering again.
  At most 20 follow-ups run in a row: a job a follow-up turn started counts that follow-up, a job a human message started counts none, and a job past the limit waits for a human message without holding back other jobs.
  Cancelled jobs and a silent schedule's jobs get no follow-up.
- Stop, an edit, or deleting a message stops only the reply.
  Background jobs end only through their own cancel: the client's jobs bar, `!cancel_job`, `job(action="cancel")`, revoked access, or the bot leaving the room.
- Each agent publishes its live jobs per room as `io.mindroom.background_jobs` state keyed by its user ID, carrying no arguments or results, for the client's jobs bar; a client cancels with an `io.mindroom.background_job_cancel` event.
- A restart preserves outcomes, interrupts abandoned local execution including jobs waiting for approval, and never automatically reruns a tool.
- A background child that needs approval asks through cards its job posts, and the job stays `awaiting_approval` until the decisions arrive.
  A gated call of a top-level agent that can become a managed job asks the same way, as its job's first phase: it is marked `mindroom_job` instead of pausing the run, and the reply waits up to `approval_wait_timeout` for the decision before going on.
  Such a call never runs inline: one that cannot become a job fails, and the agent CLI never offers it.
  Calls that must finish inside their run, tools whose own definition requires confirmation, excluded toolkits, subagents, and minimal mode keep the paused-run approval.

## Ownership

| Owner | Responsibility |
| --- | --- |
| `tool_jobs/runtime.py` | One accepted execution, its durable outcome, result claims, ownership, cancellation, the ended-jobs query, and retention |
| `tool_jobs/authorization.py` | Current local grants using shared construction policy |
| `tool_jobs/agno_compat_*.py` | Explicit, version-checked SDK bindings; no application authorization policy |
| `tool_jobs/agno_execution.py` and `consumption.py` | Exact call execution, and acknowledgement once the parent run saves the exact call that read a result |
| `tool_jobs/resources.py` | Defer model and storage cleanup until the reply and its detached jobs release them |
| `delegation/background.py` | Native child identity and cleanup inside the generic execution owner |
| `tool_jobs/approvals.py` | Approval cards a job posts for its child's or its own gated call, and denies when the job ends early |
| `delegation/job_approvals.py` | A background child's approval requests, asked through its job's cards |
| `tool_jobs/conversation.py` | The conversation key a reply finds its jobs by, and that conversation's ended jobs |
| `tool_jobs/notices.py` | The notice on a tool result and the turn-start note, once per job per reply |
| `tool_jobs/follow_ups.py` | A follow-up's journal source, identity, streak, and envelope |
| `tool_jobs/publication.py` | The jobs state each agent publishes per room |
| `tool_jobs/client_cancel.py` | Client cancel events, `!jobs` and `!cancel_job`, and cancelling a left room's jobs |
| `orchestration/tool_job_runtime.py` | Startup, policy revocation, publishing state, card expiry, admitting follow-ups, retention and shutdown coordination |
| `response_runner.py` | Running a follow-up under the conversation lock |
| Response and delivery owners | Serialize turns, preserve published text and tool traces, settle visible delivery |

Execution lifetime is independent of a caller's wait.
Each outcome has one active result claim; only persisted consumption acknowledges it.
A job's approval cards belong to the job alone, so no reply pauses for them and no stale card can resume newer work.
Cancelling or restarting a job that waits for approval denies its open cards.
Cancellation is saved before cleanup and stays pending until owned work settles.
Permission revocation uses internal ownership to stop execution, even though public discovery and control are no longer authorized.
Config reload retains active jobs; controls and result admission check current authorization.
A follow-up is named by its conversation and the set of jobs it reports, so admitting it again changes nothing, and the coordinator does not admit one again in the same process.
A reply that a process stop cuts short continues in place by journal replay, below what the stopped attempt showed, with an account that names the finished calls it must not repeat (see [Bot Runtime](../architecture/bot-runtime.md)).
A detached job start is such a finished call, and its result names the job ID.
Jobs the stopped attempt left running are interrupted by the restart, and their outcomes reach the conversation like any other ended job.
An interrupted outcome tells the model that a call with side effects may already have taken effect and must not run again, while a read-only call can run again for its lost result.

Shutdown first stops the coordinator's worker and drains execution.
Receipt access remains available until response finalizers finish.
A starting process takes the saved jobs over in the event journal before dispatch starts, and the journal refuses every later write of the runtime it replaced.

## Results and recovery

Discovery summaries contain at most 500 characters and retain native subagent IDs.
`wait` reads the complete supported saved value.
Workspace-backed result redirection uses `mindroom_output_path`, and ordinary automatic output saving bounds large model payloads.
Each job stores its full result once, in one typed payload whose durable envelope has a separate 64 MiB backstop; it is not a display limit.

Managed generator events retain their SDK family, serialized fields, and captured result text.
Custom events replay as fixed SDK subclasses; plugin class identity and methods are not restored from saved data.

Jobs live in the event journal's `tool_jobs` table, so they share the journal's database and backend.
Job metadata stays in memory, while the outcome's payload is saved with that outcome in one statement and retrieval reads it on demand.
Only work that a shutdown, restart, or event-loop teardown cut short is interrupted; a cancellation saved before a crash still settles as cancelled, and a child settlement saved before a crash stands.
`tests/test_tool_job_fuzz.py` and `tests/test_delegation_job_fuzz.py` generate interleaved job and subagent lifecycles, including failed saves, client cancels, restarts, and crashes.
Ended jobs remain for 30 days after their last change, read or not, longer while response or approval ownership requires them.
Expiry then deletes the job; its originating turn has finished, so the call cannot run again.
Constructor identity is a digest, not a retained settings blob.

Enabled recovery fails on a snapshot it cannot read and names its job.

## Limits

Cancellation cannot undo remote side effects or forcibly stop arbitrary Python threads.
Excluded tools retain native behavior.
Nested tools stay within their outer execution owner.
Subagent follow-ups use reusable sessions after the previous child turn finishes; injecting instructions into a running child is outside scope.
A notice reaches only a model step after a tool batch; a reply that ends without another tool call leaves the job to its follow-up.
A follow-up in a session switched to minimal mode runs and reports results it cannot acknowledge, so a later standard reply may report them again.
A bot that left a room cannot clear its jobs state there, so clients ignore jobs state from users who are not joined.
When denying an ended job's approval cards fails and the process then stops before a retry succeeds, those cards stay answerable until their own deadline, and answering them does nothing.
A restart interrupts a job that waits for its own call's approval, so that call never runs, and the model is told it did not run.
Job cards offer no timed automatic approvals, and automatic approvals granted on paused-run cards do not apply to them.
Only functions of toolkits MindRoom assembles become jobs; SDK-generated knowledge search, skill access, learning, and team delegation run inline.
