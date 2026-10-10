# Background tool jobs

This living design record defines the feature's scope and invariants.
User-facing configuration and examples are in [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Behavior

- Disabled by default.
  `background_tool_jobs.enabled` and `exclude_toolkits` are pinned at startup; changing execution policy requires a restart.
- Tools block by default.
  `wait_timeout: 0` detaches immediately; a positive value limits foreground waiting.
  A newer human message in the conversation, or any turn already queued for its lock other than a wake, releases that wait while accepted work continues; the reply then finishes and waits for the work.
  Other agents' messages and scheduled turns that arrive during such a wait queue as behind any running reply.
  Neither action pauses a job or authorizes a protected tool.
- One `job` tool provides scoped list, wait and cancel.
  Complete registered toolkits can be excluded, including plugins.
  Shell is excluded by default and keeps its own execution and cancellation controls.
- A reply never waits for background work inside its turn.
  At its response boundary, it continues with every ready outcome of its agent (or team members) and requester in the conversation, including outcomes of jobs earlier replies started.
  With work still outstanding but nothing ready, the reply ends its turn with its answer and stays `waiting` (see [Reply Messages](../architecture/reply-messages.md#background-work)): its message keeps a waiting notice below the answer, a streaming status, and its Stop button.
  When the work is ready, a wake continues the same message below its answer with the results.
  A newer reply of the same key (agent, requester, conversation, and participants) that starts waiting takes the work over, and the older message shows its answer without the notice.
  A reply that runs for an approval, reaches the join limit, or is a delegated child inside its caller never waits; the key's next reply takes the work.
  A silent schedule never waits; its work waits for the next silent run in that conversation.
  A wake the conversation's newer reply made pointless runs nothing: under the conversation lock it retrieves only what is still ready, and a wait with no work left ends with its answer.
  A wake a restart cut short continues below what it showed and is told which calls it already made.
  A conversation is not held by a waiting reply: later messages are answered as usual.
- Stop cancels the reply and the background work it started or waits for; work a newer message of the conversation started is that message's to stop.
  It suppresses automatic continuation from that stopped work, while explicit result retrieval remains possible.
  Deleting the messages a reply answers, or editing a waiting reply's message, cancels that work too.
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
| `tool_jobs/completion.py` | The response boundary: ready results to continue with, or the key of the work the span's answer waits for |
| `tool_jobs/wakes.py` | A wake's journal source and identity, and the envelope of the turn that continues a waiting reply |
| Reply records (`reply_lifecycle.py`, `event_journal/replies.py`) | Whether a reply waits and for which key, wake spans, takeover, and the job Stops a reply's Stop records |
| `orchestration/tool_job_runtime.py` | Startup, policy revocation, applying recorded job Stops, card expiry, admitting wakes, retention and shutdown coordination |
| `response_runner.py` | Running a wake under the conversation lock, retrieving only what is still ready |
| Response and delivery owners | Serialize turns, preserve published text and tool traces, settle visible delivery |

Execution lifetime is independent of a caller's wait.
Each outcome has one active result claim; only persisted consumption acknowledges it.
Consumption records the reply that first consumed the outcome, so that unfinished reply can recover after the model reads the result.
A job's approval cards belong to the job alone, so no reply pauses for them and no stale card can resume newer work.
Cancelling, stopping, or restarting a job that waits for approval denies its open cards.
Cancellation is saved before cleanup and stays pending until owned work settles.
Permission revocation uses internal ownership to stop execution, even though public discovery and control are no longer authorized.
Config reload retains active jobs; controls and result admission check current authorization.
A wait is the span's exit: its sources settle answered, its turn records like any other, and the reply's records keep the key of the work it waits for.
The coordinator admits one internal `job_wake` journal source per set of ready outcomes of a waiting reply, or one that ends a wait no work is left for; each wake is named by what it was admitted for, so admitting it again changes nothing.
Under the conversation lock the wake finds which work is still ready: it claims a `wake` span and continues with that work, or runs nothing and settles; a wait whose key has no outstanding work left ends with its answer.
A wake that pauses for an approval, or that a process stop cuts short, keeps its source pending, and its retry continues as a wake.
A reply continues with ready results at most 20 times in one span, apart from dynamic tool continuations; then the next reply takes the remaining work.
A reply that a process stop cuts short continues in place by journal replay, below what the stopped attempt showed, with an account that names the finished calls it must not repeat (see [Bot Runtime](../architecture/bot-runtime.md)); background jobs add no restart path of their own.
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
Waiting replies are reply records and survive restarts; recovered outcomes wake them.
`tests/test_tool_job_fuzz.py` and `tests/test_delegation_job_fuzz.py` generate interleaved job and subagent lifecycles, including failed saves, Stops, restarts, and crashes, and `tests/test_reply_lifecycle_fuzz.py` drives waits, wakes, takeovers, and job Stops through the reply rules; both check their guarantees after every step.
Consumed results remain for 30 days after the last acknowledged read, longer while response or approval ownership requires them.
Expiry then deletes the job; its originating turn has finished, so the call cannot run again.
A Stop's job cancellation stays recorded until the runtime applies it, so a Stop recorded while the runtime was starting or disabled still cancels that work.
Constructor identity is a digest, not a retained settings blob.

Enabled recovery fails on a snapshot it cannot read and names its job.
A disabled instance never opted in, so it logs a warning and skips unreadable snapshots while parking the others.

## Limits

Cancellation cannot undo remote side effects or forcibly stop arbitrary Python threads.
Excluded tools retain native behavior.
Nested tools stay within their outer execution owner.
Subagent follow-ups use reusable sessions after the previous child turn finishes; injecting instructions into a running child is outside scope.
A reply waits only for the work of its own requester, so a reply to another requester in the conversation leaves the first requester's message waiting.
A wake runs only once the turn running at that moment lets the conversation go.
An ad hoc team's message and its host agent's own message can both wait for that agent's work; the first to continue retrieves it, and the other then ends its wait with its answer.
An ad hoc team's wake runs in coordinate mode, since the key keeps its members but not the mode its reply chose, and one whose member left the configuration ends its wait instead.
A Stop on a reply whose work a newer reply already took over cancels nothing, since that reply already ended.
Removing an agent or team from the configuration ends its waiting replies, but their messages keep their waiting notice, since no bot remains to edit them.
Turning the feature off ends waiting replies at startup, keeping their answers.
When denying an ended job's approval cards fails and the process then stops before a retry succeeds, those cards stay answerable until their own deadline, and answering them does nothing.
Only functions of toolkits MindRoom assembles become jobs; SDK-generated knowledge search, skill access, learning, and team delegation run inline.
