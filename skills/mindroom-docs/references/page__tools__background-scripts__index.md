# Background Python Scripts

The `script` tool lets an agent launch and supervise a Python program after the initiating chat turn has ended.
The program keeps a requester-and-agent-scoped execution identity and can call a bounded subset of the agent's registered tools through MindRoom's normal hooks, approval rules, worker routing, and audit path.
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
Unknown or ineligible toolkit names, including `*`, reject the launch with a clear error.
It limits calls made through `MindRoomTools`; it does not restrict Python imports, filesystem access, operating-system calls, subprocesses, or direct network clients in the script source.
Treat `script` as trusted arbitrary code with authority comparable to `python` or `shell` inside its selected execution runtime.
MindRoom captures the configured `allowed_tools` value when the script launches.
A non-empty launch-time list restricts the run's grant to those toolkits and makes their unambiguous functions eligible for unattended approval.
An empty launch-time list captures the agent's full background-eligible callable surface but preapproves none of it for background use.
A later `allowed_tools` expansion cannot widen a running script's unattended approval authority; operator-authored `tool_approval` rules always apply live.
The `browser`, `script`, `compact_context`, `delegate`, `dynamic_tools`, `dynamic_workflow`, `memory`, and `self_config` toolkits are never available to background scripts, even when they are present on the agent.
Operator-authored `tool_approval` rules are evaluated before the background allowlist, and a matching `require_approval` rule still pauses the call.
Functions that declare their own confirmation requirement still require Matrix approval.
The `claude_agent`, `config_manager`, `scheduler`, and `subagents` toolkits are never preapproved for background scripts.

The limits are captured with each run.
`max_concurrent_runs` defaults to `3` for one requester and agent.
`max_tool_calls_per_minute` defaults to `30` and counts newly claimed logical calls rather than receipt polling or an identical retry.
`max_runtime_hours` defaults to `24`, must be positive and finite, and is enforced by lifecycle reconciliation.

## Control Functions

The agent receives five control functions.

```text
start_script(
    source: str | None = None,
    path: str | None = None,
    name: str | None = None,
    resource_profile: Literal["small", "standard", "large"] | None = None,
)
get_script_resource_profiles()
get_script(run_id: str)
cancel_script(run_id: str, force: bool = False)
list_scripts(include_finished: bool = True)
```

`start_script` requires exactly one of `source` or `path`.
`source` accepts inline Python source.
`path` must be relative to the agent workspace, and MindRoom snapshots the file before launch so later edits do not change the running program.
Source is limited to 128 KiB.
`name` is an optional short label shown in status and list results.
On Kubernetes, `resource_profile` selects one of three administrator-bounded profiles.
Omitting it uses the configured default.
Call `get_script_resource_profiles` first to see the live default and exact CPU and memory requests and limits.
The selected profile and its exact quantities are also returned by start, status, and list operations.
An explicit profile fails when the active worker backend cannot enforce resource profiles.

`get_script` returns durable run state plus the supervisor's recent process output when it is available.
`list_scripts` returns only runs owned by the current requester and agent.
`cancel_script` revokes the tool capability before it signals the process.
Normal cancellation requests graceful termination, waits for a short bounded grace period, escalates to a force kill when needed, and publishes `cancelled` only after process exit is confirmed.
`cancel_script(..., force=True)` skips the graceful signal, but it still confirms the process outcome before claiming cancellation completed.
If signalling or confirmation is temporarily unavailable, the cancellation intent remains durable and later status or reconciliation passes retry it.

Control responses never expose the capability token or its hash.

## Calling Agent Tools From A Script

The worker environment includes the `mindroom.script_sdk` client, whose implementation uses no third-party imports of its own.
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
Operator approval denial raises a non-retryable `MindRoomToolCallError` with kind `approval_denied`.
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

MindRoom captures the run's permitted toolkit-and-function pairs and unattended approval authority at launch.
Every call intersects that launch grant with the agent's current live tool surface, so removing a tool or function revokes it without restarting the script.
Every call also rechecks the requester's current room access and agent reply authorization, including joined-room membership grants.
Adding toolkits or functions to the live surface, including through a later `allowed_tools` expansion, does not grant a running script new unattended authority.
Authorization changes, agent removal, `script` tool removal, and requester-isolation changes durably revoke affected runs before process reconciliation.

Background calls use the same tool hooks, approval scripts, function-authored confirmation, execution identity, worker routing, result normalization, and audit events as an ordinary agent tool call.
An approval card is tied to the exact run, call ID, function, arguments, requester, room, and thread.
Only the original requester can decide it.
Cancellation, expiry, agent removal, and orphan recovery settle pending cards without authorizing the call.

## Worker And Network Requirements

The supported safe deployment uses a dedicated Docker or Kubernetes worker backend as described in [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/).
The worker must run the same MindRoom revision as the primary runtime and must be able to read the staged script snapshot from its configured worker-state root.
The worker must also reach the primary script gateway over an authenticated network path.
Kubernetes background scripts are disabled by default because a general primary API listener exposes more authority than the capability-gated script gateway.
They are admitted only when `MINDROOM_SCRIPT_GATEWAY_URL` names a gateway-only listener and the operator sets `MINDROOM_SCRIPT_GATEWAY_ISOLATED=true` to attest that workers cannot reach other primary API routes through that listener.
Enforce that boundary with a separate listener or path-filtering proxy and network policy; the environment flag does not create network isolation by itself.
When Agent Vault is enabled, run-specific Kubernetes script process pods deliberately omit Agent Vault init, token, proxy, and CA material because their lifecycle is scoped to one run.
Calls through `MindRoomTools` still use the authenticated script gateway and normal live tool-execution routing, so ordinary dedicated tool workers retain their configured Agent Vault boundary.
Direct network clients started by the script process do not receive Agent Vault credential injection.

Set `MINDROOM_SCRIPT_GATEWAY_URL` to the complete worker-reachable gateway base, including `/api/script-gateway`.
For Docker only, `MINDROOM_PUBLIC_URL` can instead name the reachable MindRoom origin and MindRoom appends `/api/script-gateway`.
Kubernetes always requires an explicit `MINDROOM_SCRIPT_GATEWAY_URL` naming its gateway-only listener.
Worker mode rejects missing, malformed, credential-bearing, unresolved, unspecified, or loopback gateway addresses.
A query string or fragment is also rejected because the SDK appends receipt endpoint paths to this base.

```bash
export MINDROOM_WORKER_BACKEND=docker
export MINDROOM_DOCKER_WORKER_IMAGE=mindroom:dev
export MINDROOM_SANDBOX_PROXY_TOKEN=replace-with-a-long-random-token
export MINDROOM_SCRIPT_GATEWAY_URL=https://mindroom.example.org/api/script-gateway
```

For Kubernetes, configure the dedicated backend and isolated gateway attestation instead:

```bash
export MINDROOM_WORKER_BACKEND=kubernetes
export MINDROOM_SCRIPT_GATEWAY_URL=https://script-gateway.example.org/api/script-gateway
export MINDROOM_SCRIPT_GATEWAY_ISOLATED=true
```

Build the worker image from the same source checkout when testing unreleased code.

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

Every non-local script run receives its own dedicated worker process and worker filesystem root, even when another run belongs to the same requester and agent.
The run-specific worker key extends the canonical requester-and-agent key while keeping the agent name as its final component.
Private agents are supported in worker mode when they use `private.per: user_agent`; broader requester-wide private scopes remain unavailable to background-script workers.
The worker receives its run snapshot plus the canonical requester-and-agent scoped workspace and state projections used by that worker scope.
The script can read and modify files visible through that scoped worker filesystem and can read operator-authored worker environment values, including backend `extra_env` and workspace environment overlays.
MindRoom does not automatically mirror global worker-grantable credentials into a script-specific worker; governed SDK calls obtain their normal authority through the primary gateway.
This credential rule is not a general secret boundary because operators can deliberately expose values through worker storage or environment configuration.
Worker mode is process and filesystem isolation, not an independent network sandbox.
Direct network access follows the Docker, host, and configured egress policy of the worker, and `allowed_tools` does not govern that traffic.
Sibling run-specific worker roots and snapshots are not mounted into the script worker.
Brokered tool calls still use the durable requester-and-agent execution identity and each tool's current primary-or-worker execution target rather than the arbitrary script process's run-specific worker.

Setting `MINDROOM_SANDBOX_EXECUTION_MODE` to `off`, `local`, or `disabled` permits local execution instead of a worker.
Local execution is marked unsafe, runs under the primary host account, inherits the primary process environment, and makes no secret-isolation claim.
Background scripts require Linux process-group containment and return an error on unsupported platforms.
Use local execution only for trusted development scripts.

## Lifecycle And Failure Semantics

Run states are `starting`, `running`, `exited`, `failed`, `cancelled`, and `interrupted`.
`exited` means the process returned exit code zero.
`failed` means launch failed or the process returned a nonzero exit code.
`cancelled` means cancellation was requested and process exit was confirmed.
`interrupted` means MindRoom lost a required runtime fact, such as the worker supervisor handle, or an isolation-changing reload intentionally stopped the run.
Authorization loss, owning-agent removal, and removal of the `script` tool are also recorded as interruptions.

Call states are `pending`, `completed`, `failed`, and `indeterminate`.
Call receipts are durable so a script can poll one accepted call without replaying it.
Calls are serialized within one run to keep approval and side-effect order predictable.

MindRoom never adopts, resumes, or automatically relaunches Python source after a primary-runtime restart, upgrade, worker loss, or worker replacement.
Startup fences new launches, durably revokes every inherited nonterminal run, terminates processes reachable through the exact currently configured backend, removes private snapshots, and retires exact dedicated workers before reopening.
Routine Docker launch changes such as an image or worker-token rotation retain cleanup ownership of existing workers.
If the Docker daemon, worker storage root, or worker name prefix changed while MindRoom was offline, startup leaves the run revoked and nonterminal and keeps script admission closed instead of reconstructing the historical backend; restore the prior ownership configuration, restart once to retire the run, then apply the new configuration.
MindRoom durably revokes every affected run before process reconciliation, and publishes `interrupted` only after process exit is confirmed.
If the shutdown deadline expires before exit is confirmed, the capability remains revoked and the run stays nonterminal for startup reconciliation.
Design watchers to checkpoint their observed state and deliberately relaunch from that known state rather than assuming an immortal process.

Terminal process output is retained durably with a 64 KiB bound and remains available from `get_script(run_id)` after maintenance observes the exit.
Cancellation cannot guarantee that an external side effect already started by a tool was rolled back.
When execution crossed that boundary and the result cannot be proven, the call is `indeterminate` rather than falsely reported as cancelled or failed.

Terminal runs are retained for 30 days by default.
Private source and capability snapshots and exact dedicated workers are removed before terminal state is published.
After the retention window, lifecycle maintenance removes background approval rows, durable tool-call receipts, and the terminal run row without selecting a worker backend.
Set `MINDROOM_SCRIPT_RETENTION_SECONDS` to a finite positive number of seconds to change the window; zero, negative, nonnumeric, and non-finite values are rejected at startup.
