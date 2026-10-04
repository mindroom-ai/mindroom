---
icon: simple/python
---

# Background Python Scripts

The `script` tool lets an agent start a Python program that keeps running after the chat turn ends.
Use it for watchers, polling loops, and other small automations that should wake the agent only when something changes.
The script can call a chosen subset of the agent's tools through the `MindRoomTools` SDK, with the same hooks, approval rules, worker routing, and audit trail as an ordinary tool call.

Treat `script` as trusted arbitrary code with authority comparable to `python` or `shell`.
`allowed_tools` limits only calls made through `MindRoomTools`; it does not restrict Python imports, filesystem access, operating-system calls, subprocesses, or direct network clients in the script.

## Enable The Tool

Add `script` to the agent that will own the process, together with the toolkits the script may call.
This example lets a watcher read a URL and send a Matrix message to its own agent when the value changes.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5-5

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

Scripts also need a worker backend or explicit local execution; see [Worker And Network Requirements](#worker-and-network-requirements).

| Option | Default | Meaning |
|--------|---------|---------|
| `allowed_tools` | `[]` | Toolkit names (not function names) the script may call through `MindRoomTools` |
| `max_concurrent_runs` | `3` | Maximum unfinished runs for one requester and agent, at least `1` |
| `max_tool_calls_per_minute` | `30` | Maximum new SDK calls per minute for one run, at least `1`; automatic SDK retries and status polling do not count |
| `max_runtime_hours` | `24` | Maximum lifetime of one run in hours, positive and finite; the deadline holds even while the primary runtime is offline |

Each run keeps the limits and `allowed_tools` value in effect when it started.

### Which tools a script can call and when it needs approval

- A non-empty `allowed_tools` list restricts the script to those toolkits, and their functions run without approval.
- An empty list lets the script call every eligible toolkit on the agent, but each call waits for approval unless a `tool_approval` rule auto-approves it.
- Unknown or ineligible toolkit names, including `*`, make `start_script` fail with an error.
- The `browser`, `script`, `compact_context`, `delegate`, `dynamic_tools`, `dynamic_workflow`, `memory`, `self_config`, and `skill_manage` toolkits are never available to scripts, even when the agent has them.
- Listing `claude_agent`, `config_manager`, or `scheduler` in `allowed_tools` does not auto-approve their calls; only a `tool_approval` rule can.
- Operator [`tool_approval`](../tool-approval.md) rules are checked first, so a matching `require_approval` rule still pauses the call, and functions that declare their own confirmation still ask for it.
- Only the original requester can approve or deny a script's call, and approval cards for a cancelled or finished run are settled without running the call.

Access is checked again on every call.
Removing a toolkit or function from the agent, or the requester losing access to the room or agent, takes effect immediately without restarting the script.
Adding toolkits later, including expanding `allowed_tools`, never gives a running script new unapproved access.

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

- `start_script` requires exactly one of `source` (inline Python) or `path` (relative to the agent workspace), limited to 128 KiB.
  A `path` file is copied at launch, so later edits do not change the running program.
  `name` is an optional label shown in status and list results.
- `resource_profile` selects a Kubernetes CPU and memory profile; omitting it uses the configured default.
  `get_script_resource_profiles` shows the default and each profile's exact requests and limits, and start, status, and list results report the selected profile.
  An explicit profile fails on a backend that cannot enforce resource profiles.
  Operators define the profiles as described in [Kubernetes deployment](../deployment/kubernetes.md) and the [environment variable reference](../configuration/index.md#workers-and-background-scripts).
- `get_script` returns the run state plus recent process output when available.
- `list_scripts` returns only runs owned by the current requester and agent.
- `cancel_script` immediately revokes the script's tool access, requests graceful termination, force-kills after a short grace period, and reports `cancelled` once the process has exited.
  `force=True` kills the process without the graceful step.

## Calling Agent Tools From A Script

The worker environment includes the `mindroom.script_sdk` client, which reads its connection settings from the environment the launcher provides.
Create one `MindRoomTools` instance and call tools by toolkit name, function name, and JSON-compatible keyword arguments.

```python
from mindroom.script_sdk import MindRoomTools

tools = MindRoomTools()
result = tools.call("website", "read_url", url="https://example.org/status.txt")
print(result, flush=True)
```

`MindRoomTools.call(toolkit_name, function_name, **arguments)` blocks until the call finishes and returns a JSON-compatible value.
Arguments must have an unambiguous strict-JSON representation.
A result larger than about 64 KiB fails the call with kind `result_too_large`.
Calls within one run execute one at a time, in order.
The SDK retries transient failures automatically without repeating the side effect.

Failures raise `MindRoomToolCallError`, which exposes `kind`, `retryable`, and `call_id` so a script can log or stop predictably.
A denied approval raises a non-retryable error with kind `approval_denied`.
An `indeterminate` call means MindRoom accepted the call but cannot prove whether its side effect happened, so the script must not repeat it automatically.

## Complete Watcher Example

This watcher polls a text endpoint and wakes the same agent once per observed value change.
Replace the URL and agent name with values for your deployment.

```python
from __future__ import annotations

import time

from mindroom.script_sdk import MindRoomTools


STATUS_URL = "https://example.org/controlled-status.txt"
AGENT_NAME = "watcher"
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
            message=f"The watched value changed; inspect {STATUS_URL} now.",
            recipient=AGENT_NAME,
        )


if __name__ == "__main__":
    main()
```

The script runs in the room, thread, and requester context it was started from, so omitting `room_id` sends to that conversation, and sending to another room still requires normal Matrix authorization.
Setting `recipient` to the agent's own name starts a new turn for that agent; see [Matrix Message](matrix-message.md#agent-conversations) for recipient rules.
Self-messaging requires a human requester, so start a self-triggering watcher from a turn a person started.
Make the watcher edge-triggered: update its observed value before sending, and never react to its own unchanged output.

## Worker And Network Requirements

Run scripts on a dedicated Docker or Kubernetes worker backend as described in [Sandbox Proxy](../deployment/sandbox-proxy.md).
The shared `static_runner` sandbox is not supported.
Workers should run the same MindRoom release as the primary; see [Keeping worker versions matched](../deployment/sandbox-proxy.md#keeping-worker-versions-matched).
Without a worker backend or explicit local execution, `start_script` fails with `Background scripts require a worker or an explicitly disabled sandbox.`

Workers must reach the primary's script gateway over the network.
Set `MINDROOM_SCRIPT_GATEWAY_URL` to the complete worker-reachable gateway base, including `/api/script-gateway`.
With Docker, `MINDROOM_PUBLIC_URL` can name the reachable MindRoom origin instead, and MindRoom appends `/api/script-gateway`.
Missing, malformed, credential-bearing, unresolvable, unspecified, or loopback gateway addresses are rejected, as are URLs with a query string or fragment.

```bash
export MINDROOM_WORKER_BACKEND=docker
export MINDROOM_DOCKER_WORKER_IMAGE=mindroom:dev
export MINDROOM_SANDBOX_PROXY_TOKEN=replace-with-a-long-random-token
export MINDROOM_SCRIPT_GATEWAY_URL=https://mindroom.example.org/api/script-gateway
```

### Kubernetes

Kubernetes scripts stay disabled until the gateway runs on a listener that exposes nothing else, because workers that can reach the general API listener get more authority than the script gateway grants.
Until then, `start_script` fails with `Kubernetes background scripts require a gateway-only listener; use Docker or explicit unsafe-local mode until that boundary is configured.`

- `MINDROOM_SCRIPT_GATEWAY_PORT` makes the primary serve a second listener that answers only `/api/script-gateway` routes.
- `MINDROOM_SCRIPT_GATEWAY_URL` must name that listener explicitly; `MINDROOM_PUBLIC_URL` is not used on Kubernetes.
- `MINDROOM_SCRIPT_GATEWAY_ISOLATED=true` attests that workers cannot reach other primary API routes through that listener.
  The flag does not create network isolation, and the main API port is unchanged, so network policy must still keep workers off it.

The runtime Helm chart's `scriptGateway.enabled` value configures the listener, its `<fullname>-script-gateway` ClusterIP Service, the worker and control-plane NetworkPolicies, `NO_PROXY`, and all three variables, as described in the [runtime chart README](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#background-script-gateway).

```bash
export MINDROOM_WORKER_BACKEND=kubernetes
export MINDROOM_SCRIPT_GATEWAY_PORT=8767
export MINDROOM_SCRIPT_GATEWAY_URL=http://mindroom-runtime-script-gateway.mindroom.svc.cluster.local:8767/api/script-gateway
export MINDROOM_SCRIPT_GATEWAY_ISOLATED=true
```

With Agent Vault enabled, the script's own direct network requests do not receive Agent Vault credential injection, while its `MindRoomTools` calls run through normal tool routing and keep the configured Agent Vault boundary.

### What a script's worker can access

- Each run gets its own dedicated worker and filesystem root, even when other runs belong to the same requester and agent.
- The worker sees the requester-and-agent scoped workspace (see [Filesystem isolation and agent data](../deployment/sandbox-proxy.md#filesystem-isolation-and-agent-data)), and the script can read and modify files there; runs of the same requester and agent share that workspace, so they see each other's files in it.
- The script can read operator-provided worker environment values, including backend `extra_env` and workspace environment overlays.
- Global worker credentials are not copied into script workers; `MindRoomTools` calls get their normal authority through the gateway, and each call runs where that tool normally runs, not in the script's worker.
- Worker mode isolates processes and files, not the network: direct network access follows the worker's Docker, host, and egress policy, and `allowed_tools` does not govern it.
- [Private agents](../configuration/agents.md#private-instances) must use `private.per: user_agent`; other private scopes fail with `Background-script workers for private agents require private.per=user_agent.`

### Local execution

Setting `MINDROOM_SANDBOX_EXECUTION_MODE` to `off`, `local`, or `disabled` runs scripts in local execution mode instead of a worker.
Local execution is marked unsafe, runs under the primary host account, inherits the primary process environment, and provides no secret isolation.
It requires Linux and fails on other platforms with `Background scripts require Linux process-group containment.`
Use local execution only for trusted development scripts.

## Lifecycle And Failure Semantics

| Run state | Meaning |
|-----------|---------|
| `starting`, `running` | The run is launching or active |
| `exited` | The process returned exit code zero |
| `failed` | Launch failed or the process returned a nonzero exit code |
| `cancelled` | Cancellation was requested and the process has exited |
| `interrupted` | MindRoom stopped or lost the run for one of the reasons below |

Tool calls are `pending`, `completed`, `failed`, or `indeterminate`.
Cancelling a run cannot roll back a side effect a tool already started; such a call is reported `indeterminate` rather than cancelled or failed.

### Restarts, upgrades, and reloads

Kubernetes runs keep running through primary restarts, compatible upgrades, and config reloads that do not affect their owner.
An upgraded image applies only to new runs; existing runs stay on the image they started with.
While MindRoom starts up or replaces its worker backend, new launches return an error and must be retried once MindRoom is ready, while SDK calls from running scripts are retried automatically.
Keep the isolated gateway address stable across upgrades.

Docker and local runs are interrupted by every primary restart.
Worker-backend replacement also interrupts Docker runs.

A run is also interrupted when:

- its worker or supervisor is lost, for example through node loss;
- its owning agent, its `script` tool, or the requester's authorization is removed;
- a reload changes the owning agent's execution isolation or assigned knowledge base paths, or changes `defaults.worker_grantable_credentials`;
- any plugin reload happens, even for plugins the script does not use;
- a restart changes what the run depends on, such as the gateway URL, worker ownership or storage, the selected resource profile's resources, or the `MINDROOM_SCRIPT_GATEWAY_ISOLATED` attestation.

Changing an unused resource profile does not interrupt runs.
MindRoom never relaunches an interrupted script automatically, so design watchers to checkpoint their observed state for a deliberate relaunch.

If the Docker daemon, worker storage root, or worker name prefix changed while MindRoom was offline, script launches stay blocked until the old runs are retired.
Restore the previous settings, restart once so MindRoom can retire those runs, then apply the new settings.

### Output and retention

`get_script(run_id)` keeps returning up to 64 KiB of process output after the run ends.
Finished runs, their tool-call receipts, and approval records are kept for 30 days by default.
Set `MINDROOM_SCRIPT_RETENTION_SECONDS` to change the window; see the [environment variable reference](../configuration/index.md#workers-and-background-scripts).
