# Background tool jobs

This living design record defines the feature's scope and invariants.
User-facing configuration and examples are in [Agent Orchestration](../tools/agent-orchestration.md#background-jobs).

## Behavior

- Disabled by default.
  `background_tool_jobs.enabled` and `exclude_toolkits` are pinned at startup; changing execution policy requires a restart.
- Tools block by default.
  `wait_timeout: 0` detaches immediately; a positive value limits foreground waiting.
  A human follow-up releases that wait while accepted work continues.
  Neither action pauses a job or authorizes a protected tool.
- One `job` tool provides scoped list, inspect, wait and cancel.
  Complete registered toolkits can be excluded, including plugins.
  Shell is excluded by default and keeps its own execution and cancellation controls.
- After independent work, the reply joins outstanding jobs.
  Waiting is transient visible progress.
  Results arriving during streaming wait for a safe response boundary; idle completion enters the existing serialized conversation runner.
- Stop cancels the reply and this agent's outstanding managed jobs for the same requester and conversation, including earlier turns.
  It suppresses automatic continuation from that stopped work, while explicit result retrieval remains possible.
- A restart preserves outcomes and approvals, interrupts abandoned local execution, and never automatically reruns a tool.
  Turning the feature off parks saved work.

## Ownership

| Owner | Responsibility |
| --- | --- |
| `tool_jobs/runtime.py` | One accepted execution, durable generations, result claims, cancellation and retention |
| `tool_jobs/authorization.py` | Current local grants using shared construction and knowledge policy |
| `tool_jobs/agno_compat_*.py` | Explicit, version-checked SDK bindings; no application authorization policy |
| `tool_jobs/agno_execution.py` and `consumption.py` | Exact call execution and acknowledgement after the SDK saves consumption |
| `tool_jobs/resources.py` | Retain toolkit resources until every accepted user drains |
| `delegation/background.py` | Native child identity and cleanup inside the generic execution owner |
| `orchestration/tool_job_runtime.py` | Startup, policy revocation, completion wakeups and shutdown coordination |
| Response and delivery owners | Serialize replies, preserve published text and tool traces, settle visible delivery |

Execution lifetime is independent of a caller's wait.
Each outcome generation has one active result claim; only persisted consumption acknowledges it.
Approval continuations bind both job ID and generation, so stale cards cannot mutate newer work.
Cancellation publishes its generation before cleanup and stays pending until owned work settles.
Permission revocation uses internal ownership to stop execution, even though public discovery and control are no longer authorized.

Shutdown first stops completion admission and drains execution.
Receipt access and the storage lease remain available until response finalizers finish.
Only then may a replacement process acquire the job store.

## Results and recovery

Discovery summaries contain at most 500 characters and retain native subagent IDs.
`wait` reads the complete supported saved value.
Workspace-backed result redirection uses `mindroom_output_path`, and ordinary automatic output saving bounds large model payloads.
The durable codec has a separate 64 MiB per-value envelope backstop; it is not a display limit.

Managed generator events retain their SDK family, serialized fields, and captured result text.
Custom events replay as fixed SDK subclasses; plugin class identity and methods are not restored from saved data.

Unread terminal payloads cool to disk.
Consumed results remain for 30 days after the last acknowledged read, longer while response or approval ownership requires them.
Expiry keeps an execution receipt but drops full results, tool arguments and child task input.
Constructor identity is a digest, not a retained settings blob.

The released v2026.9.165 format is normalized at one legacy boundary.
Its exact notification receipt does not imply consumption.
Saved child outcomes survive, abandoned human holds interrupt, and missing constructor or source evidence is never inferred.
These old snapshots cannot support exact recovery of their original Matrix source because the writer did not record it.
Consumed historical results can expire once no pending approval owns their conversation.

## Limits

Cancellation cannot undo remote side effects or forcibly stop arbitrary Python threads.
Excluded tools retain native behavior.
Nested tools stay within their outer execution owner.
Subagent follow-ups use reusable sessions after the previous child turn finishes; injecting instructions into a running child is outside scope.

## Verification contract

Regressions cover SDK calls, native approvals, current grants, output files, transient waiting, foreground release, Stop, shutdown ordering and restart consumption.
Fresh process tests check that disabled construction does not install job SDK bindings.
Real Matrix exploration additionally checks follow-ups, active streaming, Stop and restart with a supported model and independently inspected durable effects.

Completion requires passing relevant tests and repository checks, recording any unverified live scenarios, and independent review of the final pushed commit.
