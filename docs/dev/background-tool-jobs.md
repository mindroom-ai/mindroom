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
- After independent work, the reply joins outstanding jobs.
  Waiting is transient visible progress.
  Results arriving during streaming wait for a safe response boundary; idle completion enters the existing serialized conversation runner.
- Stop cancels the reply and this agent's outstanding managed jobs for the same requester and conversation, including earlier turns.
  It suppresses automatic continuation from that stopped work, while explicit result retrieval remains possible.
- A restart preserves outcomes and approvals, interrupts abandoned local execution, and never automatically reruns a tool.
  Recovery rechecks current permissions and room membership before any saved approval runs.
  Turning the feature off parks saved work.

## Ownership

| Owner | Responsibility |
| --- | --- |
| `tool_jobs/runtime.py` | One accepted execution, durable generations, result claims, cancellation and retention |
| `tool_jobs/authorization.py` | Current local grants using shared construction policy |
| `tool_jobs/agno_compat_*.py` | Explicit, version-checked SDK bindings; no application authorization policy |
| `tool_jobs/agno_execution.py` and `consumption.py` | Exact call execution and acknowledgement after the SDK saves consumption |
| `tool_jobs/resources.py` | Defer model and storage cleanup until the reply and its detached jobs release them |
| `delegation/background.py` | Native child identity and cleanup inside the generic execution owner |
| `orchestration/tool_job_runtime.py` | Startup, policy revocation, completion wakeups and shutdown coordination |
| Response and delivery owners | Serialize replies, preserve published text and tool traces, settle visible delivery |

Execution lifetime is independent of a caller's wait.
Each outcome generation has one active result claim; only persisted consumption acknowledges it.
Consumption records the reply that first consumed each generation, so that unfinished reply can recover after the model reads the result without waking unrelated replies.
Approval continuations bind both job ID and generation, so stale cards cannot mutate newer work.
Cancelling a job during its approval pause expires the card presenting that pause, which resumes the reply waiting on it.
Cancellation publishes its generation before cleanup and stays pending until owned work settles.
Permission revocation uses internal ownership to stop execution, even though public discovery and control are no longer authorized.
Config reload retains active jobs; controls and result admission check current authorization.
Completion enters the existing response owner as a nonprojected internal journal source carrying the original requester and exact recipient; it never sends Matrix messages back through ingress.
Its reply is recorded as the turn of that internal source, so Stop, recovery, and dedup treat it like any other reply.
Accepted jobs retain their original source identity, so a still-pending request recovers stored outcomes instead of repeating its tool calls, and internal completion defers to that source while it remains pending.
That re-run replaces the interrupted reply, as it does for any recovered request.
It answers the original request, with a nonpersistent note naming the accepted jobs whose stored outcomes it retrieves instead of repeating their calls.

Shutdown first stops completion admission and drains execution.
Receipt access and the storage lease remain available until response finalizers finish.
Only then may a replacement process acquire the job store.

## Results and recovery

Discovery summaries contain at most 500 characters and retain native subagent IDs.
`wait` reads the complete supported saved value.
Workspace-backed result redirection uses `mindroom_output_path`, and ordinary automatic output saving bounds large model payloads.
Each job stores its full result once, in one typed payload whose durable envelope has a separate 64 MiB backstop; it is not a display limit.

Managed generator events retain their SDK family, serialized fields, and captured result text.
Custom events replay as fixed SDK subclasses; plugin class identity and methods are not restored from saved data.

Job metadata stays in memory, while each generation's payload is a separate file that retrieval reads on demand.
A payload file is written before the metadata that references it, so a crash in between leaves the job running for recovery to interrupt.
Only work that a shutdown, restart, or event-loop teardown cut short is interrupted; a cancellation or Stop saved before a crash still settles as cancelled, and a child settlement saved before a crash stands.
`tests/test_tool_job_fuzz.py`, `tests/test_delegation_job_fuzz.py`, and `tests/test_tool_job_completion_fuzz.py` generate interleaved job, subagent, and completion-wake lifecycles, including failed saves, restarts, and crashes, and check these guarantees after every step.
Consumed results remain for 30 days after the last acknowledged read, longer while response or approval ownership requires them.
Expiry then deletes the job's files; its originating turn has finished, so the call cannot run again.
A job stays while jobs started by a turn that delivered its outcome remain, so Stop can trace them through it to their human turn.
A Stop recorded while the runtime could not receive it, for example while the feature was disabled, is restored at startup only for jobs on the stopped turn's own source and completion ancestry.
Constructor identity is a digest, not a retained settings blob.

Snapshots carry one schema version.
Enabled recovery fails on a snapshot with any other version and names the file to remove.
A disabled instance never opted in, so it logs a warning and skips unreadable snapshots while parking the others.

## Limits

Cancellation cannot undo remote side effects or forcibly stop arbitrary Python threads.
Excluded tools retain native behavior.
Nested tools stay within their outer execution owner.
Subagent follow-ups use reusable sessions after the previous child turn finishes; injecting instructions into a running child is outside scope.
Only functions of toolkits MindRoom assembles become jobs; SDK-generated knowledge search, skill access, learning, and team delegation run inline.
