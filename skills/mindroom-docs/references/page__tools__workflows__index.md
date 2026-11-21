# Workflows

The `workflow` tool lets an agent run a Python script in the background that runs agents, calls the agent's tools, and returns one report.
The script defines `def main(args):`, and MindRoom calls it.
Every `agent()`, `tool()` and `shell()` call of the script is saved with its result as a node of the run.

A run that stopped or failed can be resumed: its saved calls are reused, and only what changed or failed runs again.
A run that a MindRoom restart interrupts is resumed the same way on its own.

Treat `workflow` as trusted arbitrary code with authority comparable to `python` or `shell`.
The script is ordinary Python, so nothing restricts its imports, file access, subprocesses, or network clients.
Enabling `workflow` also lets the agent run the other agents of its room; see [Agents](#agents).

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
An operator [`tool_approval`](https://docs.mindroom.chat/tool-approval/) rule for `start_workflow` asks before anything starts, as for any other call.
A plugin's `tool:before_call` or `tool:environment` hook that declines `start_workflow` or `resume_workflow` also starts nothing, and `tool:after_call` hooks receive the run's report when it ends.
A restart of MindRoom while the run is unfinished is the exception: the continued job delivers the report to the conversation, and the `tool:after_call` hooks of the call that started it do not run.
Do not rely on an after-call hook running exactly once for a workflow call.

## Functions

```text
start_workflow(source=None, path=None, args=None, title=None, max_concurrent=16, resource_profile=None)
get_workflow(run_id, nodes="active")
cancel_workflow(run_id)
resume_workflow(run_id, source=None, path=None, rerun=None)
```

- `start_workflow` takes exactly one of `source` (inline Python) or `path` (a file in the agent's workspace) and returns `{job_id, run_id, status, title}` at once.
  The report reaches the agent through the [background job](https://docs.mindroom.chat/tools/agent-orchestration/#background-jobs) of the run, in full; a failed run delivers the last 4 KiB of the script's output and traceback.
- `get_workflow` returns the run's status, attempt, node counts, usage, waiting and failed calls, and the calls `nodes` selects: `active`, `failed`, or `all`.
  For a run that has ended it also returns `result`: the report, or why the run has none.
- `cancel_workflow` stops the run's script and ends the run `cancelled`; its finished calls stay saved.
  Cancelling the run's job, for example with `!cancel_job`, does the same.
- `resume_workflow` starts the next attempt of a run that has ended and returns the same handle, with the ID of a new job; see [Resume A Run](#resume-a-run).

A run belongs to the agent and the requester that started it.
Another requester, or another agent, gets `No workflow run <id> in your runs.` for its ID, exactly as for an ID that does not exist.

Where a call cannot become a background job, such as inside a subagent or a minimal-mode agent, `start_workflow` and `resume_workflow` wait for the run and return the report themselves.
Cancelling that call cancels its run, unless another call still waits for the same run.
The same call delivered again finds its own run: it waits for it, or returns the report the run ended with, and runs nothing a second time.
A run that another call resumed since belongs to that call: the earlier one neither cancels it nor returns its report, and ends with `Run <id> was resumed by another call.`

## What The Script Can Call

```python
from mindroom.workflow import agent, shell, tool, phase, log, now, random

def main(args):
    phase("Test")
    tests = shell("pytest -q")
    log(f"tests exited with {tests.exit_code}")
    if not tests.ok:
        return agent("Explain these test failures in five lines:\n\n" + tests.output)
    return tool("file", "read_file", file_name="CHANGELOG.md")
```

- `agent(prompt)` runs one agent turn and returns its answer as text; see [Agents](#agents).
- `shell(command)` runs a command with the agent's `shell` tool and returns `.ok`, `.exit_code`, and `.output`.
  It returns at most the last 50 KiB of output; redirect to a file and read it for more.
- `tool(toolkit, function, **arguments)` calls any other tool of the agent and returns its result.
- `phase(title)` groups the calls that follow, and `log(text)` adds a progress line to the run.
- `now()` returns the current UTC time and `random()` a float in `[0, 1)`; both are saved, so a resumed run gets the same values again.
- `shell(..., cached=False)` and `tool(..., cached=False)` run again on every resume; use it for a check of something outside the run.

The script calls the tools the agent has at the time of each call, with the same hooks, approval rules, worker routing, and audit trail as a call from a reply.
A run that a subagent with a narrowed tool list started has only the tools on that list, checked at every call, and never the rest of its agent's tools.
`compact_context`, `dynamic_tools`, `slack`, and functions that stop the agent's current model step are never available to a script.
Do writes (files, git, HTTP) through `shell()`, `tool()` or an agent, never with plain Python: those calls are saved with their results, and the Python between them is not.
A call whose tool was started and whose end is unknown, for example because MindRoom stopped in between, ends `indeterminate` and is never run again on its own.

## Agents

```python
agent(prompt, *, name=None, agent=None, model=None, system=None, tools=None, skills=None, knowledge=None,
      profile=None, minimal=False, timeout=None, output_path=None, dedicated_worker=False, resource_profile=None)
```

`agent(prompt)` runs a fresh copy of the run's agent with all its tools and no memory of the conversation, so the prompt must hold everything the turn needs.
The turn runs as a [subagent](https://docs.mindroom.chat/tools/agent-orchestration/) of the run's agent, with the same rules as `run_subagent`: its tools, hooks, approval rules, budgets, and the limit on nested subagents.
The options are those of `run_subagent` under shorter names.

| Option | Meaning |
|--------|---------|
| `agent` | Run that configured agent, with its own tools, instead of a copy of the run's agent |
| `name` | A label for the call; it does not pick an agent |
| `model` | Any configured model name, such as `haiku` or `opus@high` |
| `system`, `tools`, `skills`, `knowledge`, `profile` | Shape a copy of the run's agent, as the same arguments of `run_subagent` do |
| `minimal` | Run the turn in minimal mode |
| `timeout` | Seconds of work, after which the turn is cancelled and the call raises with kind `timeout`; time spent waiting for an approval does not count |
| `output_path` | Also write the answer to this workspace file |
| `dedicated_worker`, `resource_profile` | Those of `run_subagent` |

A run may name itself, every agent in its agent's `delegate_to`, and every other agent whose bot is in the run's room and that may reply to the requester there.
So an agent with `workflow` can run the agents of its room without a `delegate_to` entry for them.
The description of `start_workflow` lists the `delegate_to` agents and the agents configured for the room.

A turn that fails raises `AgentError`, whose `kind` says how: `failed`, `cancelled`, `timeout`, `refused`, `interrupted`, or `indeterminate`.
A turn that may not start, for example because the model name is unknown or the agent may not reply to the requester, is `refused` with the sentence `run_subagent` gives.
Stopping one agent from the Sub-agents panel fails only that call with kind `cancelled`; the run goes on.

While an agent's tool call waits for an approval, the call and the run show `waiting`, and that time is not charged to the run's [working time](#working-time).
A denied card denies that one tool call, and the agent goes on with its turn.

The agents of a run are not listed among the conversation's background jobs: the run reports them, and `get_workflow` shows each as a node with the IDs of its job and subagent.
An agent keeps running when the attempt that asked for it ends, and the next attempt waits for the same turn instead of starting another.
Cancelling the run stops its agents, and a call whose prompt or options changed stops the agent of the old call.
`schema` is not available yet.

## Resume A Run

`resume_workflow(run_id)` works on a run that ended in any state: `failed`, `cancelled`, `interrupted`, or `completed`.
A run that has not ended is refused with `Run <id> is still running. Cancel it first.`
The next attempt runs the script from the top, and each call gets its saved result instead of running again.

- A call that completed with the same arguments returns its saved result at once.
- A call that failed or was cancelled runs again.
- In each sequence of calls, the first call that runs again makes every call after it run again too, because it may have changed what they see.
- A `cached=False` call always runs again; the calls after it are reused when it returns the same result as before, compared whole and as JSON, so `1` and `1.0` differ.
  That also holds when the call's arguments were edited, as long as no call before it ran again and `rerun` does not name it.
- A call that ended `indeterminate` raises `ToolError` with kind `indeterminate` on every attempt, whatever its arguments are now.
  Check its effect, then name it in `rerun`, for example `resume_workflow(run_id, rerun=["n4"])`, to run it again; `get_workflow(run_id, nodes="failed")` lists the IDs.
  Only an ID exactly as listed names a call; any other is refused with `Run <id> has no call x1.` and nothing changes.
- `source` or `path` replaces the script before the attempt starts; give at most one.
- A call named in `rerun`, and the calls after one that ran again, stay marked until they have run, also when an interruption ends the attempt first.
- Every attempt runs in the room and thread the run was started in, also when `resume_workflow` is called from another one by the same requester; the report goes to where it was called.

One resume takes a run, and it takes it in one step: the run, its new script and the calls named in `rerun` change together or not at all.
Of two calls made at the same time, the second ends `failed` with `Run <id> was resumed by another call.` and changes nothing, also when the first one took the run and its attempt could not start.
A resume that is cancelled or refused before it took the run leaves the run, its report and its saved calls as they were.
That includes a resume while the run's own conversation is closed to the requester, from that conversation or from another room or thread: it is refused with `The requester no longer has access.` or `The run's agent is not ready in the run's room yet. Try again in a moment.`
When that access can no longer be checked after the resume was accepted as a job, for example across a restart of MindRoom, the job waits until it can.
Before it took the run it changes nothing while it waits; after that the run waits `starting` with it. Access that is gone ends the job, and a run it had taken ends `interrupted`.
While the job of the last attempt has not ended and saved its report, a resume is refused with `Run <id> is still delivering its report. Try again in a moment.`, so each report reaches one job.
A resumed run counts as a run in progress again: at the limit of `max_concurrent_runs` the resume's job ends `failed` with `You have 8 workflow runs in progress, the limit. Wait for one to end or cancel one.` and the run stays as it ended.
The accepted script and `rerun` list are saved with the job, so a restart of MindRoom before the attempt starts loses neither.

Only the calls are saved.
The Python between them, imports, and reads of files or the network run again on every attempt, and a write made with plain Python is repeated.
A call whose arguments contain the time or a random value never matches its saved result and runs again; take such values from `now()` and `random()`.
When a call changed although the script did not, the run's log says `Call n17 changed between attempts without a script edit. It probably contains the time or a random value; use now() or random() from mindroom.workflow.`

## Restarts And Interruptions

A restart of MindRoom, a plugin reload, a worker configuration change, or a lost supervisor stops the script's process, not the run.
The run's job stays `running`, and within about a minute of the interruption, or of MindRoom's start, the run gets its next attempt, which replays the script as a resume does.
A write that finished before the interruption is not made again, and neither is a write whose end is unknown: that call is `indeterminate`, as described above.
A run whose script had already returned its report ends with that report and gets no new attempt.

- The next attempt starts once the requester's access to the agent in the run's room is confirmed; while MindRoom cannot check it yet, for example because the agent's bot has not synced or room membership is still loading after a start, the run waits `starting`.
  Every attempt is checked in this way when it starts, the first one of a run and one that `resume_workflow` asked for included.
  When the access is gone, or the bot is no longer in the run's room, the run ends `interrupted` with `The requester no longer has access.`
- While the run's job is unfinished, only that job starts the run's attempts, so a Stop or a cancel of the job always reaches whatever could start one.
  After a restart of MindRoom, the job is first checked again as every background job that a restart stopped is, and only then does the next attempt start.
  A Stop or a cancel ends the run and starts nothing, also while the Stop is still being carried out, and an attempt that started at the same moment is stopped with it.
- While MindRoom cannot tell how the run's job stands, for example because background jobs are turned off, the run gets no attempt and its reservation stays.
- A script process that cannot start yet because MindRoom is still cleaning up after its own start leaves the run `starting`; it starts within about a minute.
- What the interrupted attempt left is settled at once: a report it had returned ends the run without waiting for any of the above.
- After 5 attempts in a row that finish no call, the run ends `interrupted` with `Stopped resuming after 5 attempts that finished nothing. Fix the script and call resume_workflow.`
- A run with no job, which is one that `start_workflow` waited for inside a subagent, is resumed in the same way, under the agent and requester it was started for.
  When the call that waited for it is gone, nothing delivers the report; `get_workflow(run_id)` returns it in `result`.
- A run that MindRoom stopped while its script process was starting stays `starting`, and the next start launches it.
- A run whose job was stopped gets no further attempt, and `resume_workflow` continues it.
  After a Stop the run ends `cancelled`; when the job was stopped because the requester's access is gone, the run ends `interrupted` with `The requester no longer has access.`
  A job that ended in any other way only fails to deliver the report: the run goes on, and `get_workflow(run_id)` returns the report in `result`.
- A reservation whose job was never admitted when MindRoom stopped is deleted at the next start.
- The report of a run stays saved until its job has delivered it, however long ago the run ended.
- Removing the `workflow` tool or the agent, or revoking the requester's access, ends the run `interrupted`; it is not resumed on its own.
  That holds when MindRoom was stopped at the time: the next start finds the tool or the agent gone and ends the run with `The requester no longer has access.`
- A script process that MindRoom did not start itself, such as one that survived a restart in a worker, is stopped, and each of its requests is refused with status 409, before and after its capability is revoked, so the process exits at once.
  The same answer goes to a process whose run has ended or has moved on to another attempt: nothing it sends late, its report included, changes the run.

## Working Time

A run may work for `max_runtime_hours` of the agent's `workflow` entry, 72 by default.
Only time in which the run is `running` counts, up to the moment its script ended.
At the limit the run ends `interrupted` with `The run used its 72 hours of working time. resume_workflow continues it.`, and `resume_workflow` gives it the full time again.
An attempt that MindRoom starts on its own after an interruption keeps the time the run had left, and a run with none left gets no such attempt: it ends `interrupted` with the same sentence.
The time a run waits for its next attempt after an interruption is not counted.
With `max_runtime_hours: 0` a run has no limit.
One script process runs for at most 30 days; a run with working time left then continues in a new attempt.

## Where The Script Runs

- With a worker backend (Docker or Kubernetes), each run's script runs in a dedicated worker of its own, as a [background script](https://docs.mindroom.chat/tools/background-scripts/#worker-and-network-requirements) does, and never falls back to the host when that backend is down.
- Without a worker backend, the script runs as a child process of MindRoom on Linux, only when `defaults.allow_shell_without_worker` is `true` or `MINDROOM_SANDBOX_EXECUTION_MODE` selects [local execution](https://docs.mindroom.chat/tools/background-scripts/#local-execution).
  Otherwise `start_workflow` fails with `Workflows run code. Route this agent to a worker, or set defaults.allow_shell_without_worker: true to run it on this machine.`
- The script never runs inside the MindRoom process itself.
