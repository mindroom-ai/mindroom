---
icon: lucide/network
---

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
| `max_tool_calls_per_minute` | `600` | New `tool()` and `shell()` calls per minute for one run; a call above the rate waits for the minute to have room, it does not fail |
| `max_concurrent_agents` | none | Ceiling for the `max_concurrent` a call asks for: a start that asks for more gets this many |
| `large_run_notice_usd` | `10` | Projected cost at which a run is marked as large, `0` for never by cost; see [Cost Limit](#cost-limit) |

A run is a background job, so `start_workflow` needs `background_tool_jobs.enabled: true` and `workflow` must not be in `background_tool_jobs.exclude_toolkits`.
Otherwise a call from the agent's own reply fails with `Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow.`
An operator [`tool_approval`](../tool-approval.md) rule for `start_workflow` asks before anything starts, as for any other call.
A plugin's `tool:before_call` or `tool:environment` hook that declines `start_workflow` or `resume_workflow` also starts nothing, and `tool:after_call` hooks receive the run's report when it ends.
A restart of MindRoom while the run is unfinished is the exception: the continued job delivers the report to the conversation, and the `tool:after_call` hooks of the call that started it do not run.
Do not rely on an after-call hook running exactly once for a workflow call.

## Functions

```text
start_workflow(source=None, path=None, args=None, title=None, max_concurrent=16, resource_profile=None, max_cost_usd=None)
get_workflow(run_id, nodes="active")
cancel_workflow(run_id)
resume_workflow(run_id, source=None, path=None, rerun=None, max_cost_usd=None)
```

- `start_workflow` takes exactly one of `source` (inline Python) or `path` (a file in the agent's workspace) and returns `{job_id, run_id, status, title}` at once.
  The report reaches the agent through the [background job](agent-orchestration.md#background-jobs) of the run, in full; a failed run delivers the last 4 KiB of the script's output and traceback.
  `max_cost_usd` stops the run as `interrupted` at that cost so it can be resumed with a higher limit; see [Cost Limit](#cost-limit).
- `get_workflow` returns the run's status, attempt, node counts, usage, waiting and failed calls, and the calls `nodes` selects: `active`, `failed`, or `all`.
  For a run that has ended it also returns `result`: the report, or why the run has none.
  See [Cost](#cost) for `usage`.
- `cancel_workflow` stops the run's script and ends the run `cancelled`; its finished calls stay saved.
  Cancelling the run's job, for example with `!cancel_job`, does the same.
  A call of a run, its script's or one of its agents', cannot cancel that run or a run it is inside: it is refused with `A call of a run cannot cancel that run or one it is inside. Return from main() or raise to end the script.`
  The plain `script` tool's `list_scripts`, `get_script` and `cancel_script` neither list nor stop a run's script.
- `resume_workflow` starts the next attempt of a run that has ended and returns the same handle, with the ID of a new job; see [Resume A Run](#resume-a-run).

A run belongs to the agent and the requester that started it.
Another requester, or another agent, gets `No workflow run <id> in your runs.` for its ID, exactly as for an ID that does not exist.

Where a call cannot become a background job, such as inside a subagent or a minimal-mode agent, `start_workflow` and `resume_workflow` wait for the run and return the report themselves.
Cancelling that call cancels its run, unless another call still waits for the same run.
The same call delivered again finds its own run: it waits for it, or returns the report the run ended with, and runs nothing a second time.
A run that another call resumed since belongs to that call: the earlier one neither cancels it nor returns its report, and ends with `Run <id> was resumed by another call.`

## What The Script Can Call

```python
from mindroom.workflow import agent, foreach, parallel, ask_user, shell, tool, phase, log, now, random, PartialFailure

def main(args):
    phase("Test")
    tests = shell("pytest -q")
    log(f"tests exited with {tests.exit_code}")
    if not tests.ok:
        return agent("Explain these test failures in five lines:\n\n" + tests.output)
    return tool("filesystem", "read_file", path="CHANGELOG.md")
```

- `agent(prompt)` runs one agent turn and returns its answer as text, or as parsed JSON with `schema`; see [Agents](#agents).
- `ask_user(question, options=None)` asks the run's requester in the thread and returns the answer; see [Questions](#questions).
- `shell(command)` runs a command with the agent's `shell` tool and returns `.ok`, `.exit_code`, and `.output`.
  It returns at most the last 50 KiB of output; redirect to a file and read it for more.
- `tool(toolkit, function, **arguments)` calls any other tool of the agent and returns its result.
  Name the toolkit as it is registered: another name of it, such as `file` for `filesystem`, is refused as a tool the run does not have.
  `tool("delegate", "run_subagent", ...)` and `tool("workflow", "start_workflow", ...)` start an agent and a run; see [Runs Inside Runs](#runs-inside-runs).
- `foreach(items, fn)` and `parallel([fn1, fn2])` run calls in parallel; see [Fan-Out](#fan-out).
- `phase(title)` groups the calls that follow, and `log(text)` adds a progress line to the run.
- `now()` returns the current UTC time and `random()` a float in `[0, 1)`; both are saved, so a resumed run gets the same values again.
- `shell(..., cached=False)` and `tool(..., cached=False)` run again on every resume; use it for a check of something outside the run.

The script calls the tools the agent has at the time of each call, with the same hooks, approval rules, worker routing, and audit trail as a call from a reply.
A run that a subagent with a narrowed tool list started has only the tools on that list, checked at every call, and never the rest of its agent's tools.
A run that such a run's script starts or resumes has no more than that run, and a resume by a subagent with a shorter list leaves the run only what both lists hold; a later resume never gives tools back.
A turn that is limited to some of its agent's tools in another way, such as a listening turn, starts and resumes no run: `A turn that is limited to some of its agent's tools cannot start or resume a workflow.`
A plain script of the `script` tool cannot call the `workflow` tool.
`compact_context`, `dynamic_tools`, `slack`, and functions that stop the agent's current model step are never available to a script.
Do writes (files, git, HTTP) through `shell()`, `tool()` or an agent, never with plain Python: those calls are saved with their results, and the Python between them is not.
A call whose tool was started and that did not return its result ends `indeterminate` and is never run again on its own.
That holds whatever came between: the tool raised, a plugin's check withheld its result, the answer was lost on the way from a worker, or MindRoom stopped.
The error still starts with what the tool said.
Only a call that provably never reached its tool fails as safe to run again: a hook or an approval refused it, the shell tool refused the command before running it, a worker refused the request in its own words, or the call was never sent to its worker.

## Agents

```python
agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None, tools=None, skills=None, knowledge=None,
      profile=None, minimal=False, timeout=None, output_path=None, dedicated_worker=False, resource_profile=None)
```

`agent(prompt)` runs a fresh copy of the run's agent with all its tools and no memory of the conversation, so the prompt must hold everything the turn needs.
The turn runs as a [subagent](agent-orchestration.md) of the run's agent, with the same rules as `run_subagent`: its tools, hooks, approval rules, budgets, and the limit on nested subagents.
The options are those of `run_subagent` under shorter names.

| Option | Meaning |
|--------|---------|
| `schema` | A JSON Schema; the call returns the parsed JSON value that matches it instead of text. See [Structured Output](#structured-output) |
| `agent` | Run that configured agent, with its own tools, instead of a copy of the run's agent |
| `name` | A label for the call; it does not pick an agent. Without it the label is the first line of the prompt. Every label is redacted and then cut to 80 characters; one that was cut ends in `... [truncated]` |
| `model` | Any configured model name, such as `haiku` or `opus@high` |
| `system`, `tools`, `skills`, `knowledge`, `profile` | Shape a copy of the run's agent, as the same arguments of `run_subagent` do. `tools`, `skills` and `knowledge` narrow the copy only together with `system`, and an empty list gives it none |
| `minimal` | Run the turn in minimal mode |
| `timeout` | Seconds of work, after which the turn is cancelled and the call raises with kind `timeout`. Time spent waiting for an approval does not count, and the work before an approval or a restart does. The turn is stopped on time also between two attempts of the run; the time MindRoom itself is down counts as work |
| `output_path` | Also write the answer to this workspace file; not together with `schema` |
| `dedicated_worker`, `resource_profile` | Those of `run_subagent` |

A run may name itself, every agent in its agent's `delegate_to`, and every other agent whose bot is in the run's room and that may reply to the requester there.
So an agent with `workflow` can run the agents of its room without a `delegate_to` entry for them.
The description of `start_workflow` lists the `delegate_to` agents and the agents configured for the room.
That list is read from the configuration alone; whether a listed agent's bot is in the room and may reply to the requester is checked when the call starts.
While the bot of a room's agent has not finished starting, for example right after a restart, or the requester's access to that agent is still loading, the call is not refused: it waits and starts once both are known.
An agent whose bot failed to start for good is waited for in the same way, until the run is stopped or its working time ends.
A run that a member of a configured team started while it answered in that team is the team's: the team's bot must be in the room, and the requester's access to the team is what counts.
A run that an agent started while another agent ran it as its child is served by the calling agent's bot, and the requester must still reach both agents: the calling agent's bot grants nothing the child's own access does not.
Every call of a run takes the same check before it is saved or sent: the run's saved tool list, then the access of the run's agent as it is now, and the serving bot's place in the run's room.
That holds for `tool()`, for an agent behind `tool("delegate", ...)`, for `agent()`, and for each attempt.
A call made while that access cannot be answered yet, for example while the bot is restarting, waits and is asked again.

A turn that fails raises `AgentError`, whose `kind` says how: `failed`, `cancelled`, `timeout`, `schema`, `refused`, `interrupted`, or `indeterminate`.
A turn that may not start, for example because the model name is unknown or the agent may not reply to the requester, is `refused` with the sentence `run_subagent` gives.
`tools`, `skills` or `knowledge` without `system` is refused with `tools, skills and knowledge narrow a copy only together with system=.`
Stopping one agent from the Sub-agents panel fails only that call with kind `cancelled`; the run goes on.
An agent that had started a tool call ends `indeterminate` instead, however its turn ended: it failed, it ran out of its `timeout`, or it was stopped, alone or with its run.
No resume runs it again without `rerun`.
A turn whose record shows that it started no tool call keeps its kind and runs again on resume.
A Stop while background jobs are off cannot read an agent's turn, so an agent that was given a job ends `indeterminate` then too.
A Stop of the run asks all its agents to end at once and waits up to 30 seconds for them together, then reads what each agent did.

While an agent's tool call waits for an approval, the agent's call shows `waiting`, and so does the run when no other call of it is working.
Time the run spends `waiting` is not charged to its [working time](#working-time).
A denied card, and one that expired after `tool_approval.timeout_days`, denies that one tool call, and the agent goes on with its turn.
A `tool()` or `shell()` call of the script that needs an approval shows `waiting` in the same way, and a denial raises `ToolError` with kind `denied`.

An agent of a run acts in the room and the thread the run was started in, for the run's requester.
A message it sends with `matrix_message` and no other target lands in that thread.

The agents of a run are not listed among the conversation's background jobs: the run reports them, and `get_workflow` shows each as a node whose `links` hold the IDs of its job and subagent and the agent and model it ran as.
An agent keeps running when the attempt that asked for it ends, and the next attempt waits for the same turn instead of starting another.
Cancelling the run stops its agents, and a call whose prompt or options changed stops the agent of the old call.

### Structured Output

```python
check = agent("List factual problems in this report.\n\n" + draft,
              schema={"type": "object", "required": ["problems"],
                      "properties": {"problems": {"type": "array", "items": {"type": "string"}}}})
if not check["problems"]:
    ...
```

With `schema`, a JSON Schema as a dict, `agent()` returns the parsed JSON value instead of text; JSON `null` is a value like any other.
It needs no special tool, so it works with every model, in standard and in minimal mode.

- The schema is checked first.
  One that is no valid JSON Schema fails the call with kind `schema` and `schema is not valid JSON Schema: <path>: <reason>.` before any agent starts.
  A `$ref` or `$dynamicRef` must point inside the schema, as `#/$defs/name` does.
  One that names a URL or a file, or nothing in the schema, fails the call the same way, and nothing is ever fetched.
- The agent gets the prompt, then `End your answer with one fenced json block that matches this JSON Schema:` and the schema.
- The value is the last fenced `json` block of the answer, or the whole answer when it has none.
- An answer without a JSON value, or with one that does not match, is corrected by the same agent in a further turn: `Your answer did not match the schema: <error>. Reply with only the corrected fenced json block.`
  The agent keeps its conversation, each turn is a job and a [cost](#cost) entry of its own, and the turns share the call's `timeout`.
- After 5 turns the call raises `AgentError` with kind `schema`: `Agent '<name>' did not return JSON matching the schema after 5 attempts. Last error: <error>.`
  A resume runs it again, unless one of its turns started a tool call: then the call is `indeterminate`, as any call that may have written.
  The same holds when a further turn fails, is cancelled, times out or is cut off by a restart: what an earlier turn did still counts.
- `schema` and `output_path` cannot be used together, since the file would hold an answer before it is checked.
  Such a call fails with kind `schema` before any agent starts; return the value and write it from the script.
- The schema is part of what the call is, so a resume with another schema runs the call again.

## Questions

```python
when = ask_user("When shall the report go out?")
if ask_user("Deploy to production?", ["Yes", "No"]) == "Yes":
    shell("make deploy")
```

`ask_user(question, options=None)` shows the question in the run's thread and returns the answer of the run's requester.
With `options`, a list of strings, the answer is one of them; without, it is free text.
Only the branch that asks waits: the other items of a `foreach` or `parallel` keep running, and the question holds none of the run's `max_concurrent` places.
The run shows `waiting` once nothing else of it works, and that time is not charged to its [working time](#working-time).

- The question is one card in the thread, a notice from the run's agent that carries `io.mindroom.workflow_question` with `run`, `node`, `question_id`, `requester`, `text`, `options`, `free_text`, `status` and `answer`.
  `status` is `open`, `answered` or `cancelled`, and the card is edited when it changes.
- Its text for other clients is `Question q3fa91c: <text> 1. Yes 2. No. Answer with !answer q3fa91c <number or text>.`
- `!answer q3fa91c 2` answers with an option's number or its text, and `!answer q3fa91c after lunch` with free text.
  Without the ID, `!answer` answers the requester's only open question in the thread; with several open it asks for the ID shown on the card and answers none.
  An ID without an answer answers nothing, and neither does an ID that two open questions happen to share: answer that one on its card.
- MindRoom Chat answers with the event `io.mindroom.workflow_answer`, `{"run", "node", "option"}` or `{"run", "node", "text"}`, sent in the run's thread as a reply to the card.
- An answer counts only from the run's requester, in the run's room and thread, for the card as it is now, and only while the requester still has access to the run's agent there.
  A question whose card could not be posted yet takes no answer.
  Any other answer event changes nothing and gets `io.mindroom.workflow_answer_rejected` with `{"run", "node", "reason"}` as a reply: `not_for_you`, `wrong_place`, `no_longer_open`, `already_answered` or `not_an_option`.
- The ID is `q` and six hex characters made from the run, the call and how often the call was asked, so two runs in one thread never share one, and neither do a question and the question that replaced it.
- The question waits as long as it takes.
  It stays open across a restart of MindRoom, the card is posted once, and an answer that comes while the script is not running is saved and returned when it runs again.
- The answer is saved as the call's result, so a resume does not ask again.
  A call whose question or options changed asks again, and the old card shows `cancelled`.
- A run that ends closes its open questions: their cards show `cancelled`, and a cancelled `ask_user` raises `WorkflowError` with kind `cancelled`.
- Options that are not a list of non-empty strings fail in the script with `options must be a list of strings, such as ['Yes', 'No'].`
- A question takes at most 20 options of at most 200 bytes each, since an answer is an option's own text and the card must be one event.
  More, or longer ones, fail the call with `A question takes at most 20 options of at most 200 bytes each.` before anything waits.
- The card shows the first 2,000 characters of a long question, or fewer if the card would not be one event, and the first 500 of a long answer, each ending in `... [truncated]`.
  The script gets the whole answer.
- A card that could not be posted is posted again when the script asks again, and every ten seconds by the run itself; the question of a [cost limit](#cost-limit) is posted the same way.

## Runs Inside Runs

An agent of a run has the `workflow` tool of its agent, so it can call `start_workflow` itself.
That call waits for the new run and returns its report to the agent.

- The new run is a run inside the agent's run: it names that run and the agent's call, and the outer run shows it as a group under that call.
- It has its own `max_concurrent`, so an outer run that works on one call at a time does not wait for itself.
- It does not count toward `max_concurrent_runs`.
- Cancelling the outer run, or stopping the agent, cancels it.
- Its cost is part of the outer run's cost, once; see [Cost](#cost).
- It is found by the call it was made in, its script and its `args`, and not by the ID of the model's tool call.
  So an agent that makes the same call again in the same turn waits for the same run, or gets the report that run ended with, and starts no second one.
  To run the same script twice from one agent call, give it different `args`.
- The agents a run starts are one level deeper than the agent that started the run, and the limit is 3 levels.
  At the limit `agent()` is refused with `Cannot delegate: the maximum delegation depth was reached.`

A script can start agents and runs through `tool()` as well.

- `tool("delegate", "run_subagent", task=..., agent_name=..., system_prompt=...)` is the same as `agent()`: the run shows an agent call, with a job of its own, and the call returns the agent's answer.
- `tool("workflow", "start_workflow", source=...)` starts a run inside the script's run and returns its report.
  Its agents are as deep as the script's own.
- Both first pass what every `tool()` call passes: the tool must still be the agent's, the hooks run, and the operator's approval rule for `run_subagent` or `start_workflow` applies.
  While the call waits for an approval it shows `waiting`, nothing is started before the decision, and a denial raises `ToolError` with kind `denied`.
- `tool("delegate", "continue_subagent", ...)` is refused with `A workflow script cannot call delegate.continue_subagent yet. Start a fresh agent with agent() or tool("delegate", "run_subagent", ...).`

## Cost

`get_workflow` returns `usage` with `input_tokens`, `output_tokens`, `tool_calls`, `cost_usd`, `attempt_cost_usd` and `cost_unknown`.

- `cost_usd` is what the run cost so far, over every attempt, and `attempt_cost_usd` is what the current attempt cost.
  After a resume the second number shows what the resume itself spent.
- Every model turn is counted once, however often MindRoom restarted while it ran.
  What an agent spent before a restart that it was continued after is counted for the attempt before that restart, not for the one that followed.
- A call that runs again is paid again, and the run keeps what its earlier turn cost.
- A saved answer that a resume reuses adds nothing.
- What the subagents of a run's agent cost is counted beside the agent's own turn, once, as cost only: their tokens are not in the token counts.
- A run inside the run is in both sums, once, and it is part of the attempt it was started in.
  It is kept for as long as the run around it, so that run's sums never shrink.
- A turn on a model without a price in the configuration adds its tokens and sets `cost_unknown`; `cost_usd` is then a lower bound.
  So does a subagent of a run's agent whose cost is not known, whatever the agent's other subagents cost.
- An agent whose turn had ended before a restart is counted for the attempt that started it, not for the one that read its answer.
- Each call that asked a model shows its own `usage` in `nodes`.

### Cost Limit

Nothing caps what a run costs unless its start names a limit: `start_workflow(..., max_cost_usd=5)`.

- When `cost_usd`, the sum over every attempt and every run inside the run, reaches the limit, no further call starts.
  Calls that already run finish, and once nothing of the run is working any more, the run ends `interrupted` with `Stopped at the cost limit ($X of $Y).` on its card and in its delivered report.
  An amount under a cent is written with up to six decimal places.
- A nested run is under its ancestors' limits too: before starting new work, it checks every ancestor run that has a `max_cost_usd`.
  Once an ancestor is over its limit, the nested run starts no new call, lets its running calls finish, and ends `interrupted` with that reason, and the parent then stops at its limit.
- To continue a run stopped at its cost limit, call `resume_workflow(run_id, max_cost_usd=...)` with a limit above what the run has spent, or click Resume on the run's card.
  Resume replays from stored results as any resume does; saved answers cost nothing, while calls that run again add to `cost_usd`, and an interrupted nested child continues under the parent's raised limit.
  Calling `resume_workflow` on a run that has reached its limit without a higher `max_cost_usd` is refused with `Run <id> has already spent $<spent> (limit $<limit>). Pass max_cost_usd above $<spent> to resume it.`
  A card Resume control (`io.mindroom.workflow_control`) without `max_cost_usd` on a run stopped at its limit raises `max_cost_usd` to `spent + max_cost_usd` (one more allowance of the same size).
- A turn on a model without a price adds nothing to `cost_usd`, so a limit cannot stop a run of unpriced models.
- The limit is checked before starting new work and when a call ends, so the run can pass it by what the calls already running cost.
  A further turn of a [`schema`](#structured-output) call is new spending too: at the limit it does not start, and the run ends `interrupted`.
  The check reads what the run has spent from the store, so it holds after a resume and after a restart of MindRoom.

A run is marked `large` once it has more than 25 agent calls, or once its projected cost passes the `large_run_notice_usd` of the agent's `workflow` tool entry (10 by default, 0 turns the cost test off).
The mark is advisory: the run's log says `Large run: <n> agents, about $<projected> projected.`, its card is edited at once, and nothing is paused.

## The Run In The Room

A run that started shows one card in its thread: a notice from the run's agent that MindRoom edits as the run goes on.
A run inside a run has no card; it is a group in the graph of the run around it.

- The card's text is `Workflow "<title>" <status>: <n> done, <n> running, <n> waiting. Stop it with !cancel_job <job id>.`, and the last sentence is left out once the run has ended.
- Its content carries `io.mindroom.workflow_run` with `run`, `job_id`, `requester`, `title`, `status`, `counts`, `usage`, `phases`, `attempt`, `started_at`, `ended_at`, `error` and `large`.
  `counts` has `queued`, `running`, `waiting`, `completed`, `failed`, `indeterminate` and `cached`; a cancelled call counts as failed, and `cached` counts the completed calls whose saved result this attempt reused.
  `usage` is that of [Cost](#cost) plus `projected_cost_usd`: what the run will have cost if every agent still to end costs what a finished turn cost on average.
  `error` is the last line of what a run that did not complete ended with.
  Every cost on the card, in `usage` and in the rows of `graph`, is a decimal string such as `"0.12"`, with six decimal places at most: a homeserver signs the card, and what it signs holds no float.
  The frames below carry the same costs as numbers.
- The card is edited at once when the run's status, its list of phases, or whether a call waits for a person changes, and when the run is marked large.
  While only numbers change it is edited every ten minutes at most.
- The last edit of an ended run adds `graph`: the run's calls, groups, phases and last log lines, cut down to what one event holds, with the counts of what was left out in `omitted`.
- The card is posted once: a restart of MindRoom finds it again and edits it.
- A card that could not be sent is sent again with the run's next view, every ten seconds while the run works.
  The edit that says how the run ended is sent until the room took it, after 20 seconds, 40 seconds and so on up to every ten minutes, and again after a restart of MindRoom.
- A row of a call whose agent still runs has no usage yet: its `run` names the sub-agent run whose own frames show what it used so far.

While the run works, clients that show live updates get its graph as `io.mindroom.workflow` frames, see [Live Updates](../streaming.md#live-updates).
A frame is `{v, run, sid, seq, meta, snapshot}` or `{v, run, sid, seq, meta, delta}`.
`meta` holds the run's agent, thread, title, status, attempt, times, `max_concurrent`, `max_cost_usd`, `large`, `counts` and `usage`.
A snapshot holds `phases`, `groups`, `nodes`, `edges`, `omitted` and `log`, and a delta the rows that changed since the frame before it.
A snapshot is sent first, every ten seconds, at the run's end, and whenever a row left the graph.
A graph too large for one event keeps the calls that wait for a person first, then failed, running and queued ones, then the newest completed ones, and counts the rest in `omitted`.
Room state lists the run as one job, with its IDs only: no title, label or phase is written there, and the jobs of the run's agents are not listed.

MindRoom Chat's Resume button sends `io.mindroom.workflow_control` with `{"run": "<run id>", "action": "resume"}` (and optionally `"max_cost_usd"`) in the run's thread.
It resumes the run as `resume_workflow(run_id, max_cost_usd=...)` does, with a new job, when the run's requester sent it in the run's room and thread.
For a run stopped at its cost limit, a Resume control without `max_cost_usd` raises the limit to `spent + current max_cost_usd` (one more allowance of the same size); an explicit `max_cost_usd` that is not above what the run spent changes nothing.
The report of that job is delivered in the thread when the run ends, as the report of any job nobody waits for.
The bot that posts a run's cards also takes these events and the answers to its questions: for a run that a team's member started in the team, that is the team's bot.
Anything else, such as another person's event, another action, or a run that has not ended, changes nothing.
A Stop that names the job of one agent of a run cancels that agent only; the script gets `AgentError` with kind `cancelled` for it.

## Fan-Out

```python
from mindroom.workflow import PartialFailure, agent, foreach, parallel

def main(args):
    try:
        notes = foreach(args["topics"], lambda topic: agent(f"Research {topic}. Cite sources."))
    except PartialFailure as failure:
        notes = failure.ok
    summary, critique = parallel([
        lambda: agent("Summarize these notes:\n\n" + "\n\n".join(notes)),
        lambda: agent("List what these notes leave open:\n\n" + "\n\n".join(notes)),
    ])
    return summary + "\n\nOpen points:\n" + critique
```

- `foreach(items, fn)` calls `fn(item)` for every item in parallel and returns the results in the order of the items.
- `parallel([fn1, fn2])` calls each function, which takes no argument, in parallel and returns the results in the order of the functions.
  For a loop over items use `foreach`: `parallel([lambda: agent(p) for p in prompts])` is refused with `These functions capture the same loop variable, so each would see its last value. Use foreach(prompts, lambda p: agent(p)).`
  `parallel([agent(a), agent(b)])` is refused with `parallel() takes functions. Write parallel([lambda: agent(a), lambda: agent(b)]) or use foreach(items, fn).`
- A function may make several calls, one after the other, and may itself call `foreach` or `parallel`.
- There is no limit on the number of items.
  The run's `max_concurrent`, 16 unless `start_workflow` was given another number, is how many agent and tool calls of the run work at the same time; the others wait `queued` and start in the order they were made.
  A call that waits for an approval keeps its concurrency place until it finishes, so queued calls wait for a free place.
  A queued call has started nothing: when the run is cancelled it never starts, and after a restart the next attempt runs it.
  An agent that kept working over a restart goes on at once, so for that time the run may work on more calls than its limit.
  `foreach(items, fn, max_concurrent=4)` and `parallel(fns, max_concurrent=4)` hold one fan-out to fewer.
  The script itself works on at most 256 items of one fan-out at a time, whatever the limit.
- When calls fail, the others still finish, and then the fan-out raises `PartialFailure`.
  Its `.ok` holds the results that succeeded, in order; `.results` has one entry per position, with `None` where the call failed; `.errors` maps each failed position to its exception; `.items` holds the items of a `foreach`.
  Its message names the failures, for example `2 of 5 calls failed: [1] AgentError(failed): ...; [3] KeyError: 'x'`.
  A `PartialFailure` that the script does not catch fails the run.
- An exception of the script's own code inside a function, such as a `KeyError`, fails only that item, and the run shows it as a failed call of kind `error` where it happened.
- A run that completed although calls failed says so in the first line of its report: `3 calls failed (n4, n9, n12); get_workflow(run_id, nodes="failed") shows them.`

On a resume each item is matched by its value, not by its position, so adding, removing, or reordering items reuses the saved calls of the items that stayed.
Of two equal items the first and the second are told apart by their order.
An item that is not a JSON value, such as an object of the script's own class, is matched by its position.
The functions of `parallel` are matched by position.
An item whose calls all completed is not run again, and one whose call failed before it started any tool is: such an agent in a fan-out of 500 reruns that one agent.
What follows the fan-out in the script then runs again too, because it may depend on the item that ran again.

### Agents That Edit The Same Repository

Agents that run in parallel share the agent's workspace, so two of them editing one checkout would overwrite each other.
Give each item its own git worktree and branch, and merge the branches in one step afterwards.

```python
def fix(issue):
    path = f"../wt-{issue['id']}"
    shell(f"git worktree add {path} -b fix/{issue['id']}")
    agent(f"Work only in {path}. Fix and commit: {issue['text']}")
    return f"fix/{issue['id']}"

def main(args):
    branches = foreach(args["issues"], fix)
    return agent(f"Merge these branches into the current branch one by one and resolve conflicts: {branches}")
```

`agent(..., dedicated_worker=True)` gives an agent its own container when files outside git must not collide either.

## Resume A Run

`resume_workflow(run_id)` works on a run that ended in any state: `failed`, `cancelled`, `interrupted`, or `completed`.
A run that has not ended is refused with `Run <id> is still running. Cancel it first.`
The next attempt runs the script from the top, and each call gets its saved result instead of running again.
Resume retries only calls proven not to have started.
A call that may have written (a tool that raised, an agent that made tool calls) is `indeterminate`: check its effect, then name it in `rerun`.

- A call that completed with the same arguments returns its saved result at once.
- A call that failed or was cancelled before its tool was started, or an agent before it started a tool call, runs again.
  Any other call that did not complete is `indeterminate`: no write is made twice unless `rerun` names its call.
- In each sequence of calls, the first call that runs again makes every call after it run again too, because it may have changed what they see.
  Each item of a `foreach` and each function of a `parallel` is a sequence of its own, so one item that runs again leaves the other items alone; the calls after that fan-out run again.
- A `cached=False` call always runs again; the calls after it are reused when it returns the same result as before, compared whole and as JSON, so `1` and `1.0` differ.
  That also holds when the call's arguments were edited, as long as no call before it ran again and `rerun` does not name it.
- A call that ended `indeterminate` raises `ToolError` with kind `indeterminate` on every attempt, whatever its arguments are now.
  Check its effect, then name it in `rerun`, for example `resume_workflow(run_id, rerun=["n4"])`, to run it again; `get_workflow(run_id, nodes="failed")` lists the IDs.
  Only an ID exactly as listed names a call; any other is refused with `Run <id> has no call x1.` and nothing changes.
- `source` or `path` replaces the script before the attempt starts; give at most one.
- `max_cost_usd` sets the run's cost limit for the next attempt; when the run stopped at its cost limit, it must exceed what the run has spent.
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
- An agent that was working when MindRoom stopped is not started again: its job is continued as the same subagent, as a [subagent job](agent-orchestration.md#background-jobs) is, and the next attempt waits for it.
  The continued agent is sent the whole prompt of its call again, with an account of the tool calls it had made.
  The call's usage is what the turn used before and after the restart together.
- An agent that cannot be continued, for example after three restarts in a row or because its session is gone, ends its call.
  When it had started no tool call, the call fails with kind `interrupted` and the next attempt runs it again as a new agent.
  When it had, the call is `indeterminate` and is never run again on its own.
  A call whose record of tool calls cannot be read, or is not there, is `indeterminate` too: after a crash before the turn saved anything, nothing shows that it called no tool.
  So is a call whose ended agent the requester may no longer read.
- An agent of a run whose own job was stopped is not continued.
- An agent that was waiting for an approval when MindRoom stopped keeps waiting: its job is not ended, its card stays open with the expiry it had, and a decision that was already given on another card of the same turn stands.
  Once MindRoom runs again the agent's paused turn is resumed with those decisions, so the agent is not sent its task again, no tool call it had finished is made again, and no call gets a second card.
  This holds for an orderly restart and for a killed process alike, and however many restarts the wait lasts.
- When that paused turn cannot be resumed, the agent goes on in a new turn instead: it is sent its prompt and an account of the tool calls it had finished, its open cards are closed as `Superseded after a restart.`, and it asks again for the call that had waited, which had not run.
  When neither is possible the call ends as that of an agent that cannot be continued does, and a tool call that was still waiting for its decision counts as not started.
- A `tool()` or `shell()` call of the script that was waiting for an approval when MindRoom stopped never ran: the next attempt asks again with a new card.
- After 5 attempts in a row that finish no call, the run ends `interrupted` with `Stopped resuming after 5 attempts that finished nothing. Fix the script and call resume_workflow.`
- A run with no job, which is one that `start_workflow` waited for inside a subagent, is resumed in the same way, under the agent and requester it was started for.
  When the call that waited for it is gone, nothing delivers the report; `get_workflow(run_id)` returns it in `result`.
- A run inside a run goes on by itself after a restart, as the same run.
  The agent that was inside its `start_workflow` call when MindRoom stopped is continued, and it is not asked to make the call again.
  The call was saved with the run it started, in the same write, so the agent's job waits for that run and then sends the agent the run's report as the result of its call.
  A run that ended meanwhile gives its stored report at once.
  A run that another call resumed meanwhile gives the report its own attempt ended with, and is left to that other call.
  This holds for an orderly restart and for a killed process alike, and however many restarts the wait lasts.
  Stopping the agent while its job waits cancels the run inside.
- The agent is not continued when that cannot be proven: the call reserved no run before MindRoom stopped, the script file it named has changed since, or a call of another tool was cut short in the same turn.
  Its call then ends `indeterminate`, the run inside stays under that call with its report in `get_workflow` of that run, and cancelling the outer run cancels it.
  A script's own `tool("workflow", "start_workflow", ...)` call that a restart cut short ends `indeterminate` too.
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

- With a worker backend (Docker or Kubernetes), each run's script runs in a dedicated worker of its own, as a [background script](background-scripts.md#worker-and-network-requirements) does, and never falls back to the host when that backend is down.
- Without a worker backend, the script runs as a child process of MindRoom on Linux, only when `defaults.allow_shell_without_worker` is `true` or `MINDROOM_SANDBOX_EXECUTION_MODE` selects [local execution](background-scripts.md#local-execution).
  Otherwise `start_workflow` fails with `Workflows run code. Route this agent to a worker, or set defaults.allow_shell_without_worker: true to run it on this machine.`
- The script never runs inside the MindRoom process itself.
