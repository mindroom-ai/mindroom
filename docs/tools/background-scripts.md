---
icon: lucide/file-clock
---

# Background Python Scripts

The `script` tool lets an agent launch and supervise a Python program after the initiating chat turn has ended.
The program runs with a requester-and-agent-scoped identity and can call a bounded subset of the agent's registered tools through MindRoom's normal hooks, approval rules, worker routing, and audit path.
Use it for watchers, polling loops, and other small automations that should wake an agent only when something changes.

## Enable The Tool

Configure the `script` tool on the agent that will own the process.
The following complete agent example lets a watcher read a URL and send one intentional Matrix self-trigger when the value changes.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5

agents:
  watcher:
    display_name: Watcher
    role: Watch configured values and investigate meaningful changes.
    model: default
    rooms: [operations]
    tools:
      - script:
          allowed_tools: [website, matrix_message]
          max_concurrent_runs: 3
          max_tool_calls_per_minute: 30
          max_runtime_hours: 24
      - website
      - matrix_message
    instructions:
      - Use background scripts only for bounded, observable automation.
      - Cancel watchers that are no longer needed.

defaults:
  tools: []
```

`allowed_tools` contains toolkit names, not function names.
A non-empty list restricts the launch grant to those toolkits and makes their unambiguous functions eligible for unattended approval.
An empty list captures the agent's full callable tool surface at launch but preapproves none of it for background use.
Operator-authored `tool_approval` rules are evaluated before the background allowlist, and a matching `require_approval` rule still pauses the call.
Functions that declare their own confirmation requirement still require Matrix approval.
The `claude_agent`, `config_manager`, `scheduler`, and `subagents` toolkits are never preapproved for background scripts.

The limits are captured with each run.
`max_concurrent_runs` defaults to `3` for one requester, agent, and worker scope.
`max_tool_calls_per_minute` defaults to `30` and counts newly claimed logical calls rather than receipt polling or an identical retry.
`max_runtime_hours` defaults to `24`, must be positive and finite, and is enforced by lifecycle reconciliation.

## Control Functions

The agent receives four control functions.

```text
run_script(source: str | None = None, path: str | None = None, name: str | None = None)
status_script(run_id: str)
cancel_script(run_id: str, force: bool = False)
list_scripts(include_finished: bool = True)
```

`run_script` requires exactly one of `source` or `path`.
`source` accepts inline Python source.
`path` must be relative to the agent workspace, and MindRoom snapshots the file before launch so later edits do not change the running program.
Source is limited to 128 KiB.
`name` is an optional short label shown in status and list results.

`status_script` returns durable run state plus the supervisor's recent process output when it is available.
`list_scripts` returns only runs owned by the current requester and agent.
`cancel_script` revokes the tool capability before it signals the process.
Normal cancellation requests graceful termination, waits for a short bounded grace period, escalates to a force kill when needed, and publishes `cancelled` only after process exit is confirmed.
`cancel_script(..., force=True)` skips the graceful signal, but it still confirms the process outcome before claiming cancellation completed.
If signalling or confirmation is temporarily unavailable, the cancellation intent remains durable and later status or reconciliation passes retry it.

Control responses never expose the capability token or its hash.

## Calling Agent Tools From A Script

The worker environment includes the standard-library-only `mindroom.script_sdk` client.
Create one `MindRoomTools` instance and call tools by toolkit name, function name, and JSON-compatible keyword arguments.

```python
from mindroom.script_sdk import MindRoomTools

tools = MindRoomTools()
result = tools.call("website", "read_url", url="https://example.org/status.txt")
print(result, flush=True)
```

`MindRoomTools.call(toolkit_name, function_name, **arguments)` is blocking.
It submits one logical call with a stable generated call ID and argument digest, then polls only that receipt until it is terminal.
Transport retries never submit the same side effect a second time.
Arguments must have an unambiguous strict-JSON representation.
Successful results are returned as JSON-compatible Python values and are bounded before they cross the gateway.

Framework and terminal tool failures raise `MindRoomToolCallError`.
The exception exposes `kind`, `retryable`, and `call_id` fields so a script can log or stop predictably.
An `indeterminate` call means MindRoom accepted the call but cannot prove whether the side effect completed, so the script must not automatically repeat it.

The launcher injects the run ID, gateway URL, and a path to a short-lived capability file.
Use the SDK instead of reading or forwarding those values directly.
MindRoom stores only a hash of the capability in durable state and removes the raw file during terminal cleanup.

## Complete Watcher Example

This watcher polls a controlled text endpoint and wakes the same Matrix agent once per observed value change.
Replace the URL and full Matrix user ID with values for your deployment.

```python
from __future__ import annotations

import time

from mindroom.script_sdk import MindRoomTools


STATUS_URL = "https://example.org/controlled-status.txt"
AGENT_MATRIX_ID = "@mindroom_watcher:example.org"
POLL_SECONDS = 15


def main() -> None:
    tools = MindRoomTools()
    previous = tools.call("website", "read_url", url=STATUS_URL)

    while True:
        time.sleep(POLL_SECONDS)
        current = tools.call("website", "read_url", url=STATUS_URL)
        if current == previous:
            continue

        previous = current
        tools.call(
            "matrix_message",
            "matrix_message",
            action="send",
            message=f"{AGENT_MATRIX_ID} the watched value changed; inspect {STATUS_URL} now.",
            ignore_mentions=False,
        )


if __name__ == "__main__":
    main()
```

`matrix_message` defaults to `ignore_mentions=True` to prevent accidental agent loops.
Set `ignore_mentions=False` only for an intentional handoff or self-trigger like the example above.
The message must mention the actual agent Matrix ID if it is meant to start a new agent turn.
Make the watcher edge-triggered, persist or update its observed value before sending, and avoid reacting to its own unchanged output.

The script inherits the original room, thread, requester, and agent execution identity, so omitting `room_id` sends through that authorized conversation context.
Normal Matrix authorization is still enforced when a script supplies another room.

## Grants, Approval, And Revocation

MindRoom captures the run's permitted toolkit-and-function pairs at launch.
Every call intersects that launch grant with the agent's current live tool surface, so removing a tool or function revokes it without restarting the script.
Configuration reloads that remove the agent, remove the `script` tool, or change requester isolation durably revoke affected runs before process reconciliation.

Background calls use the same tool hooks, approval scripts, function-authored confirmation, execution identity, worker routing, result normalization, and audit events as an ordinary agent tool call.
An approval card is tied to the exact run, call ID, function, arguments, requester, room, and thread.
Only the original requester can decide it.
Cancellation, expiry, agent removal, and orphan recovery settle pending cards without authorizing the call.

## Worker And Network Requirements

The safe deployment uses a worker backend such as the dedicated Docker or Kubernetes backend described in [Sandbox Proxy](../deployment/sandbox-proxy.md).
The worker must run the same MindRoom revision as the primary runtime and must be able to read the staged script snapshot from its configured shared state root.
The worker must also reach the primary script gateway over an authenticated network path.

Set `MINDROOM_SCRIPT_GATEWAY_URL` to the complete worker-reachable gateway base, including `/api/script-gateway`.
Alternatively, set `MINDROOM_PUBLIC_URL` to the reachable MindRoom origin and MindRoom appends `/api/script-gateway`.
Worker mode rejects missing, malformed, credential-bearing, unresolved, unspecified, or loopback gateway addresses.
A query string or fragment is also rejected because the SDK appends receipt endpoint paths to this base.

```bash
export MINDROOM_WORKER_BACKEND=docker
export MINDROOM_DOCKER_WORKER_IMAGE=mindroom:dev
export MINDROOM_SANDBOX_PROXY_TOKEN=replace-with-a-long-random-token
export MINDROOM_SCRIPT_GATEWAY_URL=https://mindroom.example.org/api/script-gateway
```

Build the worker image from the same source checkout when testing unreleased code.

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

Background scripts always use requester-and-agent process isolation even when ordinary tools use a broader worker scope.
Their governed tool calls still follow each tool's configured primary-or-worker execution target.

Setting `MINDROOM_SANDBOX_EXECUTION_MODE` to `off`, `local`, or `disabled` permits local execution instead of a worker.
Local execution is marked unsafe, runs under the primary host account, inherits the primary process environment, and makes no secret-isolation claim.
Use local execution only for trusted development scripts.

## Lifecycle And Failure Semantics

Run states are `starting`, `running`, `exited`, `failed`, `cancelled`, and `interrupted`.
`exited` means the process returned exit code zero.
`failed` means launch failed or the process returned a nonzero exit code.
`cancelled` means cancellation was requested and process exit was confirmed.
`interrupted` means MindRoom lost a required runtime fact, such as the worker supervisor handle, or an isolation-changing reload intentionally stopped the run.

Call states are `pending`, `completed`, `failed`, and `indeterminate`.
Call receipts are durable so a script can poll one accepted call without replaying it.
Calls are serialized within one run to keep approval and side-effect order predictable.

Scripts do not survive worker loss in the first release, and MindRoom does not automatically restart the Python source after an `interrupted` run.
A primary-runtime restart can reconcile a still-live worker process, but worker eviction, restart, or migration may end it.
Design watchers so they can be launched again from known state rather than assuming an immortal process.

Process output is bounded, and cancellation cannot guarantee that an external side effect already started by a tool was rolled back.
When execution crossed that boundary and the result cannot be proven, the call is `indeterminate` rather than falsely reported as cancelled or failed.
