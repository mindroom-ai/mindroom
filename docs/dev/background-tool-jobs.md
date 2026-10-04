# Background tool jobs

This living design record defines the feature's scope and invariants.
User-facing configuration and examples are in [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Behavior

- Disabled by default.
  `background_tool_jobs.enabled` and `exclude_toolkits` are pinned at startup; changing execution policy requires a restart.
- Tools block by default.
  `wait_timeout: 0` detaches immediately; a positive value limits foreground waiting.
  A newer human message in the conversation, or any turn already queued for its lock other than a held message's continuation, releases that wait while accepted work continues; the reply then finishes and its message holds the work.
  Other agents' messages and scheduled turns that arrive during such a wait queue as behind any running reply.
  Neither action pauses a job or authorizes a protected tool.
- One `job` tool provides scoped list, wait and cancel.
  Complete registered toolkits can be excluded, including plugins.
  Shell is excluded by default and keeps its own execution and cancellation controls.
- A reply never waits for background work inside its turn.
  At its response boundary, it continues with every ready outcome of its agent (or team members) and requester in the conversation, including outcomes of jobs earlier replies started.
  With work still outstanding but nothing ready, the reply finishes, and its message holds that work: it keeps a waiting notice, a streaming status, and its Stop button.
  When held work becomes ready, ends, or starts waiting for approval, a later turn continues the same message: the model retrieves the results, and the message shows the reply's earlier text followed by the new answer.
  A newer reply of the agent with the same participants that reaches its response boundary takes the work over, and the older message shows its own reply again without the notice.
  A turn that never reaches that boundary, such as another agent's reply, a reply whose participation check stays silent, or a delegated child running inside its caller, leaves the message holding.
  A silent schedule holds its own work without a message, and the turn continuing it is silent too.
  A message offers each ready outcome to the model once, across its own turn and the turns continuing it; one the model leaves unretrieved waits for the conversation's next reply.
  Work that a foreground wait claims, or whose access is only unresolved, such as while room membership resolves after a restart, stays held without being offered; a proven denial ends it.
  A continuation or edit of a held message that ends before its response boundary, because it failed or was stopped or interrupted, releases the hold, and a message the turn did not replace shows how it ended.
  A continuation a crash cut short continues below what it already showed, like any reply a restart cut short, and retrieves the outcomes the cut-short run already read instead of losing them.
  Stop on a held message cancels the work it holds through the message's latest turn at once, leaving a newer running reply's work alone, and shows the message as stopped; Stop while a turn continues the message stops that turn and the work like any reply, including a turn that began before it could be stopped.
  A newer reply that reaches its boundary but ends without a finished message leaves an older message holding the work.
- Stop cancels the reply and this agent's outstanding managed jobs for the same requester and conversation, including earlier turns.
  It suppresses automatic continuation from that stopped work, while explicit result retrieval remains possible.
- A restart preserves outcomes, interrupts abandoned local execution including jobs waiting for approval, and never automatically reruns a tool.
- A background child that needs approval asks through cards its job posts, and the job stays `awaiting_approval` until the decisions arrive.
  Turning the feature off parks saved work.

## Ownership

| Owner | Responsibility |
| --- | --- |
| `tool_jobs/runtime.py` | One accepted execution, its durable outcome, result claims, cancellation and retention |
| `tool_jobs/authorization.py` | Current local grants using shared construction policy |
| `tool_jobs/agno_compat_*.py` | Explicit, version-checked SDK bindings; no application authorization policy |
| `tool_jobs/agno_execution.py` and `consumption.py` | Exact call execution and acknowledgement after the SDK saves consumption |
| `tool_jobs/resources.py` | Defer model and storage cleanup until the reply and its detached jobs release them |
| `delegation/background.py` | Native child identity and cleanup inside the generic execution owner |
| `delegation/job_approvals.py` | Approval cards a background child's job posts and denies when the job ends early |
| `tool_jobs/completion.py` | The response boundary: ready results to continue with, or the work the reply's message holds |
| `tool_jobs/held_replies.py` | Which work a message holds, its notice and edits, and the wake source and envelope of a turn continuing it |
| `event_journal/held_replies.py` | Durable holds, one per conversation, requester, and participants, each save a new generation |
| `orchestration/tool_job_runtime.py` | Startup, policy revocation, saved Stops, card expiry, held-message wakes, retention and shutdown coordination |
| `response_runner.py` | Saving, taking over, and releasing holds after each reply, continuing a woken message under the conversation lock, and Stop on a held message |
| Response and delivery owners | Serialize turns, preserve published text and tool traces, settle visible delivery |

Execution lifetime is independent of a caller's wait.
Each outcome has one active result claim; only persisted consumption acknowledges it.
Consumption records the reply that first consumed the outcome, so that unfinished reply can recover after the model reads the result.
A job's approval cards belong to the job alone, so no reply pauses for them and no stale card can resume newer work.
Cancelling, stopping, or restarting a job that waits for approval denies its open cards.
Cancellation is saved before cleanup and stays pending until owned work settles.
Permission revocation uses internal ownership to stop execution, even though public discovery and control are no longer authorized.
Config reload retains active jobs; controls and result admission check current authorization.
A hold is saved after its reply's final delivery, so the reply's source settles and its turn records like any other; the message then shows the notice.
The coordinator admits one internal `held_reply_wake` journal source per generation of a hold whose work became ready, ended, or now waits for something else.
That source's turn edits the held message and never claims it in the turn ledger, whose sole owner stays the reply that sent it; a Stop of that turn settles the owner.
Under the conversation lock the turn finds which work is ready: it continues with that work, shows a changed notice, or releases a message that holds nothing more.
A continuation that fails, is stopped or interrupted within the process, or pauses for approval releases its hold; one a process stop cuts short keeps holding, and its wake runs again, asking again for the outcomes the stopped run had already read.
A message continues with ready results at most 20 times across its own turn and the turns continuing it, apart from dynamic tool continuations; then the next reply takes the remaining work.
A reply that a process stop cuts short continues in place by journal replay, below what the stopped attempt showed, with an account that names the finished calls it must not repeat (see [Bot Runtime](../architecture/bot-runtime.md)); background jobs add no restart path of their own.
A continuation's wake replays the same way: once the held message shows more than its hold, the re-run continues below it like any other reply.
A detached job start is such a finished call, and its result names the job ID.
Jobs the stopped attempt left running are interrupted by the restart, and their outcomes reach the new attempt at its response boundary like any other ready work.
An interrupted outcome tells the model that a call with side effects may already have taken effect and must not run again, while a read-only call can run again for its lost result; replayed against real models, this wording stopped repeated side effects without stopping read-only calls from running again.

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
Only work that a shutdown, restart, or event-loop teardown cut short is interrupted; a cancellation or Stop saved before a crash still settles as cancelled, and a child settlement saved before a crash stands.
Holds live in the journal's `held_replies` table and survive restarts; the reply's final delivery already completed, so stale-stream cleanup leaves the held message alone, and recovered outcomes wake it.
`tests/test_tool_job_fuzz.py`, `tests/test_delegation_job_fuzz.py`, and `tests/test_tool_job_held_reply_fuzz.py` generate interleaved job, subagent, and conversation lifecycles, including failed saves, held messages, wakes racing new replies, Stops, failed continuations, restarts, and crashes, and check these guarantees after every step.
Consumed results remain for 30 days after the last acknowledged read, longer while response or approval ownership requires them.
Expiry then deletes the job; its originating turn has finished, so the call cannot run again.
A Stop recorded while the runtime could not receive it, for example while the feature was disabled, is restored at startup only for jobs that the stopped turn itself started.
Constructor identity is a digest, not a retained settings blob.

Enabled recovery fails on a snapshot it cannot read and names its job.
A disabled instance never opted in, so it logs a warning and skips unreadable snapshots while parking the others.

## Limits

Cancellation cannot undo remote side effects or forcibly stop arbitrary Python threads.
Excluded tools retain native behavior.
Nested tools stay within their outer execution owner.
Subagent follow-ups use reusable sessions after the previous child turn finishes; injecting instructions into a running child is outside scope.
A message holds only the work of its own requester, so a reply to another requester in the conversation leaves the first requester's message holding its work.
A continuation runs only once the turn running at that moment lets the conversation go.
A held message briefly shows its reply as finished before the waiting notice returns, because the hold is saved after the reply's final delivery.
Work that a failed or interrupted continuation, a crash before a hold was saved, or the join limit leaves unheld waits for the requester's next answered message.
A crash after a continuation's final edit but before its hold is saved leaves the hold showing the message as it was before that continuation, so a later takeover or Stop restores that earlier text.
An ad hoc team's message and its host agent's own message can both hold that agent's work; the first to continue retrieves it, and the other then shows its reply as finished.
An ad hoc team's hold keeps its members but not the mode its reply chose, so its continuation runs in coordinate mode, and one whose member left the configuration releases the message instead.
Turning the feature off leaves held messages waiting until it is turned back on.
When denying an ended job's approval cards fails and the process then stops before a retry succeeds, those cards stay answerable until their own deadline, and answering them does nothing.
Only functions of toolkits MindRoom assembles become jobs; SDK-generated knowledge search, skill access, learning, and team delegation run inline.
