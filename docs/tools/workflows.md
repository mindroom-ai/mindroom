---
icon: lucide/network
---

# Workflows

The `workflow` tool lets an agent run a Python script in the background that calls the agent's tools and returns one report.
The script defines `def main(args):`, and MindRoom calls it.
Every `tool()` and `shell()` call of the script is saved with its result as a node of the run.

A run that stopped or failed can be resumed: its saved calls are reused, and only what changed or failed runs again.
A run that a MindRoom restart interrupts is resumed the same way on its own.
Running agents from the script is not available yet.

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
| `max_concurrent_runs` | `8` | Unfinished top-level runs one requester may have with one agent; a run inside another run does not count |
| `max_runtime_hours` | `72` | Working time one run may use, `0` for no limit; see [Working time](#working-time) |
| `max_tool_calls_per_minute` | `600` | New `tool()` and `shell()` calls per minute for one run |
| `max_concurrent_agents` | none | Ceiling for the `max_concurrent` a call asks for; not used yet |
| `large_run_notice_usd` | `10` | Projected cost at which a run is marked as large; not used yet |

A run is a background job, so `start_workflow` needs `background_tool_jobs.enabled: true` and `workflow` must not be in `background_tool_jobs.exclude_toolkits`.
Otherwise a call from the agent's own reply fails with `Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow.`
An operator [`tool_approval`](../tool-approval.md) rule for `start_workflow` asks before anything starts, as for any other call.
A plugin's `tool:before_call` or `tool:environment` hook that declines `start_workflow` also starts nothing, and `tool:after_call` hooks receive the run's report when it ends.

## Functions

```text
start_workflow(source=None, path=None, args=None, title=None, max_concurrent=16, resource_profile=None)
get_workflow(run_id, nodes="active")
cancel_workflow(run_id)
resume_workflow(run_id, source=None, path=None, rerun=None)
```

- `start_workflow` takes exactly one of `source` (inline Python) or `path` (a file in the agent's workspace) and returns `{job_id, run_id, status, title}` at once.
  The report reaches the agent through the [background job](agent-orchestration.md#background-jobs) of the run, in full; a failed run delivers the last 4 KiB of the script's output and traceback.
- `get_workflow` returns the run's status, attempt, node counts, usage, waiting and failed calls, and the calls `nodes` selects: `active`, `failed`, or `all`.
- `cancel_workflow` stops the run's script and ends the run `cancelled`; its finished calls stay saved.
  Cancelling the run's job, for example with `!cancel_job`, does the same.
- `resume_workflow` starts the next attempt of a run that has ended and returns the same handle, with the ID of a new job; see [Resume A Run](#resume-a-run).

A run belongs to the agent and the requester that started it.
Another requester, or another agent, gets `No workflow run <id> in your runs.` for its ID, exactly as for an ID that does not exist.

Where a call cannot become a background job, such as inside a subagent or a minimal-mode agent, `start_workflow` and `resume_workflow` wait for the run and return the report themselves.
Cancelling that call cancels its run, unless another call still waits for the same run.

## What The Script Can Call

```python
from mindroom.workflow import shell, tool, phase, log, now, random

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
- `now()` returns the current UTC time and `random()` a float in `[0, 1)`; both are saved, so a resumed run gets the same values again.
- `shell(..., cached=False)` and `tool(..., cached=False)` run again on every resume; use it for a check of something outside the run.

The script calls the tools the agent has at the time of each call, with the same hooks, approval rules, worker routing, and audit trail as a call from a reply.
A run that a subagent with a narrowed tool list started has only the tools on that list, checked at every call, and never the rest of its agent's tools.
`compact_context`, `dynamic_tools`, `slack`, and functions that stop the agent's current model step are never available to a script.
Do writes (files, git, HTTP) through `shell()` or `tool()`, never with plain Python: those calls are saved with their results, and the Python between them is not.
A call whose tool was started and whose end is unknown, for example because MindRoom stopped in between, ends `indeterminate` and is never run again on its own.

## Resume A Run

`resume_workflow(run_id)` works on a run that ended in any state: `failed`, `cancelled`, `interrupted`, or `completed`.
A run that has not ended is refused with `Run <id> is still running. Cancel it first.`
The next attempt runs the script from the top, and each call gets its saved result instead of running again.

- A call that completed with the same arguments returns its saved result at once.
- A call that failed or was cancelled runs again.
- In each sequence of calls, the first call that runs again makes every call after it run again too, because it may have changed what they see.
- A `cached=False` call always runs again; the calls after it are reused when it returns the same result as before.
- A call that ended `indeterminate` raises `ToolError` with kind `indeterminate` on every attempt, whatever its arguments are now.
  Check its effect, then name it in `rerun`, for example `resume_workflow(run_id, rerun=["n4"])`, to run it again; `get_workflow(run_id, nodes="failed")` lists the IDs.
- `source` or `path` replaces the script before the attempt starts; give at most one.

Only the calls are saved.
The Python between them, imports, and reads of files or the network run again on every attempt, and a write made with plain Python is repeated.
A call whose arguments contain the time or a random value never matches its saved result and runs again; take such values from `now()` and `random()`.
When a call changed although the script did not, the run's log says `Call n17 changed between attempts without a script edit. It probably contains the time or a random value; use now() or random() from mindroom.workflow.`

## Restarts And Interruptions

A restart of MindRoom, a plugin reload, a worker configuration change, or a lost supervisor stops the script's process, not the run.
The run's job stays `running`, and within about a minute of the interruption, or of MindRoom's start, the run gets its next attempt, which replays the script as a resume does.
A write that finished before the interruption is not made again, and neither is a write whose end is unknown: that call is `indeterminate`, as described above.
A run whose script had already returned its report ends with that report and gets no new attempt.

- The next attempt starts once the requester's access to the agent is confirmed; while MindRoom cannot check it yet, the run waits.
  When the access is gone, the run ends `interrupted` with `The requester no longer has access.`
- After 5 attempts in a row that finish no call, the run ends `interrupted` with `Stopped resuming after 5 attempts that finished nothing. Fix the script and call resume_workflow.`
- A run with no job, which is one that `start_workflow` waited for inside a subagent, is not resumed after an interruption: it ends `interrupted` with the reason, and `resume_workflow` continues it.
- A run that was only reserved when MindRoom stopped, with no job and no script process yet, is deleted at the next start.
- Removing the `workflow` tool or the agent, or revoking the requester's access, ends the run `interrupted`; it is not resumed on its own.
- A script process that MindRoom did not start itself, such as one that survived a restart in a worker, is stopped, and each of its requests is refused with status 409.

## Working Time

A run may work for `max_runtime_hours` of the agent's `workflow` entry, 72 by default.
Only time in which the run is `running` counts.
At the limit the run ends `interrupted` with `The run used its 72 hours of working time. resume_workflow continues it.`, and `resume_workflow` gives it the full time again.
An attempt that MindRoom starts on its own after an interruption keeps the time the run had left.
With `max_runtime_hours: 0` a run has no limit.
One script process runs for at most 30 days; a run with working time left then continues in a new attempt.

## Where The Script Runs

- With a worker backend (Docker or Kubernetes), each run's script runs in a dedicated worker of its own, as a [background script](background-scripts.md#worker-and-network-requirements) does, and never falls back to the host when that backend is down.
- Without a worker backend, the script runs as a child process of MindRoom on Linux, only when `defaults.allow_shell_without_worker` is `true` or `MINDROOM_SANDBOX_EXECUTION_MODE` selects [local execution](background-scripts.md#local-execution).
  Otherwise `start_workflow` fails with `Workflows run code. Route this agent to a worker, or set defaults.allow_shell_without_worker: true to run it on this machine.`
- The script never runs inside the MindRoom process itself.
