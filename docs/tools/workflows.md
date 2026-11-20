---
icon: lucide/network
---

# Workflows

The `workflow` tool lets an agent run a Python script in the background that calls the agent's tools and returns one report.
The script defines `def main(args):`, and MindRoom calls it.
Every `tool()` and `shell()` call of the script is saved with its result as a node of the run.

This release starts a run, reports it, and cancels it.
Running agents from the script, resuming a stopped run, and continuing a run after a MindRoom restart are not available yet.
After a restart, a run that was in progress ends as `interrupted` and its job as interrupted.

Treat `workflow` as trusted arbitrary code with authority comparable to `python` or `shell`.
The script is ordinary Python, so nothing restricts its imports, file access, subprocesses, or network clients.

## Enable The Tool

Add `workflow` to the agent, together with the toolkits its scripts call.

```yaml
agents:
  builder:
    display_name: Builder
    role: Run checks over a repository and report the result.
    model: default
    rooms: [engineering]
    tools:
      - workflow:
          max_concurrent_runs: 8
          max_tool_calls_per_minute: 600
      - shell
      - file

background_tool_jobs:
  enabled: true
```

| Option | Default | Meaning |
|--------|---------|---------|
| `max_concurrent_runs` | `8` | Unfinished top-level runs one requester may have with one agent |
| `max_runtime_hours` | `72` | Working time one run may use, `0` for no limit; stored with each run and not enforced yet |
| `max_tool_calls_per_minute` | `600` | New `tool()` and `shell()` calls per minute for one run |
| `max_concurrent_agents` | none | Ceiling for the `max_concurrent` a call asks for; not used yet |
| `large_run_notice_usd` | `10` | Projected cost at which a run is marked as large; not used yet |

A run is a background job, so `start_workflow` needs `background_tool_jobs.enabled: true` and `workflow` must not be in `background_tool_jobs.exclude_toolkits`.
Otherwise a call from the agent's own reply fails with `Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow.`
An operator [`tool_approval`](../tool-approval.md) rule for `start_workflow` asks before anything starts, as for any other call.

## Functions

```text
start_workflow(source=None, path=None, args=None, title=None, max_concurrent=16, resource_profile=None)
get_workflow(run_id, nodes="active")
cancel_workflow(run_id)
```

- `start_workflow` takes exactly one of `source` (inline Python) or `path` (a file in the agent's workspace) and returns `{job_id, run_id, status, title}` at once.
  The report reaches the agent through the [background job](agent-orchestration.md#background-jobs) of the run, in full; a failed run delivers the last 4 KiB of the script's output and traceback.
- `get_workflow` returns the run's status, attempt, node counts, usage, waiting and failed calls, and the calls `nodes` selects: `active`, `failed`, or `all`.
- `cancel_workflow` stops the run's script and ends the run `cancelled`; its finished calls stay saved.
  Cancelling the run's job, for example with `!cancel_job`, does the same.

A run belongs to the agent and the requester that started it.
Another requester, or another agent, gets `No workflow run <id> in your runs.` for its ID, exactly as for an ID that does not exist.

Where a call cannot become a background job, such as inside a subagent or a minimal-mode agent, `start_workflow` waits for the run and returns the report itself.
Cancelling that call cancels its run.

## What The Script Can Call

```python
from mindroom.workflow import shell, tool, phase, log

def main(args):
    phase("Test")
    tests = shell("pytest -q")
    log(f"tests exited with {tests.exit_code}")
    if not tests.ok:
        return "Tests failed:\n" + tests.output
    return tool("file", "read_file", file_name="CHANGELOG.md")
```

- `shell(command)` runs a command with the agent's `shell` tool and returns `.ok`, `.exit_code`, and `.output`.
  It returns at most the last 50 KiB of output; redirect to a file and read it for more.
- `tool(toolkit, function, **arguments)` calls any other tool of the agent and returns its result.
- `phase(title)` groups the calls that follow, and `log(text)` adds a progress line to the run.

The script calls the tools the agent has at the time of each call, with the same hooks, approval rules, worker routing, and audit trail as a call from a reply.
`compact_context`, `dynamic_tools`, `slack`, and functions that stop the agent's current model step are never available to a script.
Do writes (files, git, HTTP) through `shell()` or `tool()`, never with plain Python: those calls are saved with their results, and the Python between them is not.
A call whose tool was started and whose end is unknown, for example because MindRoom stopped in between, ends `indeterminate` and is never run again on its own.

## Where The Script Runs

- With a worker backend (Docker or Kubernetes), each run's script runs in a dedicated worker of its own, as a [background script](background-scripts.md#worker-and-network-requirements) does, and never falls back to the host when that backend is down.
- Without a worker backend, the script runs as a child process of MindRoom on Linux, only when `defaults.allow_shell_without_worker` is `true` or `MINDROOM_SANDBOX_EXECUTION_MODE` selects [local execution](background-scripts.md#local-execution).
  Otherwise `start_workflow` fails with `Workflows run code. Route this agent to a worker, or set defaults.allow_shell_without_worker: true to run it on this machine.`
- The script never runs inside the MindRoom process itself.
