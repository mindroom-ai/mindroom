# Background tool jobs

This living design record defines the feature's scope and invariants.
User-facing configuration and examples are in [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Behavior

- Disabled by default.
  `background_tool_jobs.enabled` and `exclude_toolkits` are pinned at startup; changing execution policy requires a restart.
- Tools block by default.
  `wait_timeout: 0` detaches immediately; a positive value limits foreground waiting.
  When the agent starts answering a newer human message in the conversation, that wait is released while accepted work continues.
  Neither action pauses a job or authorizes a protected tool.
- One `job` tool provides scoped list, wait and cancel.
  Complete registered toolkits can be excluded, including plugins.
  Shell is excluded by default and keeps its own execution and cancellation controls.
- While background work is outstanding, the agent's latest reply in the conversation holds it.
  After independent work, the reply joins every outstanding job of its agent (or team members) and requester in the conversation, including jobs earlier replies started.
  Waiting is transient visible progress, and Stop on the waiting reply cancels the work it holds.
  Results arriving during streaming wait for a safe response boundary.
  When the agent starts answering a newer human message there, the newer reply takes over; a message that another agent answers leaves the reply holding.
  No job completion starts a reply of its own; after a restart the next reply in the conversation retrieves interrupted outcomes.
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
| `tool_jobs/completion.py` | The holding reply's join of outstanding jobs and its waiting presentation |
| `orchestration/tool_job_runtime.py` | Startup, policy revocation, saved Stops, card expiry, retention and shutdown coordination |
| Response and delivery owners | Serialize replies, preserve published text and tool traces, settle visible delivery |

Execution lifetime is independent of a caller's wait.
Each outcome has one active result claim; only persisted consumption acknowledges it.
Consumption records the reply that first consumed the outcome, so that unfinished reply can recover after the model reads the result.
A job's approval cards belong to the job alone, so no reply pauses for them and no stale card can resume newer work.
Cancelling, stopping, or restarting a job that waits for approval denies its open cards.
Cancellation is saved before cleanup and stays pending until owned work settles.
Permission revocation uses internal ownership to stop execution, even though public discovery and control are no longer authorized.
Config reload retains active jobs; controls and result admission check current authorization.
Joins happen only inside replies, so job outcomes need no journal source, turn record, or placeholder of their own.
A reply joins at most 20 times, apart from its dynamic tool continuations.
Accepted jobs retain their original source identity, so a still-pending request recovers stored outcomes instead of repeating its tool calls.
That re-run replaces the interrupted reply, as it does for any recovered request.
It answers the original request, with a nonpersistent note naming the accepted jobs whose stored outcomes it retrieves instead of repeating their calls.

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
`tests/test_tool_job_fuzz.py`, `tests/test_delegation_job_fuzz.py`, and `tests/test_tool_job_reply_hold_fuzz.py` generate interleaved job, subagent, and conversation lifecycles, including failed saves, follow-ups for this and other agents, Stops, restarts, and crashes, and check these guarantees after every step.
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
Only functions of toolkits MindRoom assembles become jobs; SDK-generated knowledge search, skill access, learning, and team delegation run inline.
