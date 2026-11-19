# Agent graphs: workflows as Python scripts

Date: 2026-11-19. Status: decided by Bas (approach B), ready to plan.

## Intent

An agent can hand a task to many agents at once and keep the plan in code: fan out, verify, loop until a check passes, let agents talk to each other, ask the user, and report once.
The orchestration is a Python script the agent writes, runs in the background, and can save and rerun.
The user watches the run as a live graph in the thread and can cancel or resume it.

Said by Bas: workflows are Python scripts on the existing script runtime, calling agents through an SDK.
Said by Bas: v1 has parallel and foreach, structured outputs, background runs with cancel, resume by replay, `ask` and approval pauses, a live graph in MindRoom Chat, and saved workflows with arguments.
Said by Bas: design for power, not safety, and make it obvious for agents and for users.
Assumed: the user-facing word is "workflow"; "agent graph" names the picture of a run.
Assumed: the old `dynamic_workflow` toolkit stays untouched in v1 and is removed after dogfooding (see "The old toolkit").

Success means three things.
An agent writes a correct workflow from the `start_workflow` tool description alone.
A run of hundreds of agents survives a restart, a missing approval, and a bug in its own script without losing finished work.
The user always sees what runs, what waits for them, and what it costs.

## Mental model

For the user: a workflow is a small program your agent writes that puts many agents to work at once.
It runs in the background while you keep chatting, shows every agent as a box in a live graph, stops to ask you when it needs an answer or an approval, and ends with one report.
You can cancel it at any time, and a stopped run continues where it left off.

For the agent: a workflow is a Python file with `def main(args)`.
`agent(prompt)` runs one agent turn and returns its answer.
Everything else is ordinary Python.
Every finished call is saved, so a resumed run skips what is done.

## Approaches considered

1. **Python script on the script runtime (chosen by Bas).** Loops, branches and data flow are plain Python, the runtime already has restart recovery, cancel, a tool gateway and approval cards, and models write Python well.
2. **Declarative YAML DAG.** Rejected: every control structure (when, repeat, foreach, check) needs its own schema and its own bugs, and conversations between agents do not fit a static graph.
3. **Hybrid (YAML with script steps).** Rejected: two languages to learn and two engines to keep correct.

## The `workflow` toolkit

Six functions.
`start_workflow` carries the whole teaching text; the others are one-liners.

```text
start_workflow(source=None, path=None, name=None, args=None, title=None, max_concurrent=16, max_cost_usd=None)
get_workflow(run_id, nodes="active")
cancel_workflow(run_id)
resume_workflow(run_id, source=None, path=None)
list_workflows()
save_workflow(run_id, name, description=None)
```

### Exact description of `start_workflow`

```text
Run a workflow: a Python script that puts many agents to work in the background.

Use it when a task needs many agents, the same step over many items, an independent check, or a loop with a hard stop.
Give exactly one of `source` (inline Python), `path` (a file in your workspace), or `name` (a saved workflow, `workflows/<name>.py`).
The script defines `def main(args):` and returns the final report, a string or a JSON value. MindRoom calls `main`; do not call it yourself.

    from mindroom.workflow import agent, foreach, parallel, spawn, ask_user, shell, tool, phase, log

    def main(args):
        phase("Research")
        notes = foreach(args["topics"], lambda t: agent(f"Research {t}. Cite sources.", model="haiku"))
        phase("Write")
        draft = agent("Write a report from these notes:\n\n" + "\n\n".join(n for n in notes if n))
        check = agent("List factual problems in this report.\n\n" + draft,
                      schema={"type": "object", "required": ["problems"],
                              "properties": {"problems": {"type": "array", "items": {"type": "string"}}}})
        if check["problems"]:
            draft = agent(f"Fix these problems: {check['problems']}\n\n{draft}")
        return draft

- agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None, tools=None, profile=None, minimal=False, timeout=None)
  runs one agent turn and returns its answer: text, or parsed JSON when `schema` (a JSON Schema) is given.
  By default the agent is a fresh copy of you with all your tools. `agent="other"` runs that agent with its own tools.
  A failed turn raises AgentError.
- foreach(items, fn) calls fn(item) for every item in parallel; parallel([fn1, fn2]) runs functions in parallel.
  Both return results in order, with None for a call that failed.
- s = spawn(name, ...same options as agent) keeps one agent's conversation: s.ask(message) continues it.
  Use it for debates and review loops.
- ask_user(question, options=None) pauses the run until the user answers, and returns the answer.
- shell(command) runs a command with your shell tool and returns .ok, .exit_code and .output: a hard check for loops.
- tool(toolkit, function, **arguments) calls any other tool of yours.
- phase(title) groups the calls that follow in the graph; log(text) shows a progress line.
- Agents in a run can talk to each other with their send_peer_message and read_peer_messages tools.

Use plain Python for the rest: loops, if, functions, while. Finished calls are saved, so resume_workflow skips them.
Use now() and random() from the SDK instead of time and random when the value goes into a prompt.
The run shows as a live graph in the thread, and you receive its report when it ends.
An agent that needs an approval pauses for the user; it does not fail.

Args:
    source: Inline Python source.
    path: Workspace-relative path of a Python file.
    name: Name of a saved workflow.
    args: JSON object passed to main(args).
    title: Three to six words shown on the run's card.
    max_concurrent: How many agents run at once. Raise it for large fan-outs.
    max_cost_usd: Pause and ask the user when the run has spent this much.
```

### The other five descriptions

```text
get_workflow: Return a run's status, cost, waiting questions and nodes. nodes is "active" (default), "failed" or "all".
cancel_workflow: Stop a run and its agents. Finished calls stay saved, so resume_workflow can continue it.
resume_workflow: Continue a stopped, failed or finished run. Saved calls are reused; pass source or path to run an edited script.
list_workflows: List saved workflows (name, description, args) and this conversation's recent runs.
save_workflow: Save a run's script as workflows/<name>.py so start_workflow(name=...) can run it again.
```

`start_workflow` returns `{run_id, job_id, status, title}` at once; it accepts the standard `background_after` argument to wait for a short run.
The report reaches the calling agent through the existing background-job delivery (notice, `job(action="collect")`, follow-up turn).
A failed run delivers the last 4 KiB of the script's output and traceback, so the agent can fix the script and call `resume_workflow`.

## The SDK: `mindroom.workflow`

Stdlib only, like `mindroom.script_sdk`, and built on its transport.
These are the complete signatures and docstrings.

```python
def agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None,
          tools=None, skills=None, knowledge=None, profile=None, minimal=False, timeout=None):
    """Run one agent turn and return its answer.

    Returns text, or parsed JSON matching `schema` (a JSON Schema dict).
    The agent is a fresh copy of the workflow's owner with all its tools, unless `agent` names another agent.
    `system`, `tools`, `skills`, `knowledge` or `profile` (a `subagents/<profile>.md` file) shape a copy of the owner.
    `model` is any configured model name, such as "haiku" or "opus@high".
    `name` labels the node and is the address other agents message.
    Raises AgentError when the turn fails, is cancelled, is denied, or times out after `timeout` seconds.
    """

def foreach(items, fn, *, max_concurrent=None):
    """Call fn(item) for every item in parallel and return the results in item order.

    A call that raises gives None at its position; the error shows on its node.
    Finished items are remembered by value, so adding or reordering items on resume reuses them.
    """

def parallel(fns, *, max_concurrent=None):
    """Call every zero-argument function in parallel and return the results in order, None for one that raised."""

def spawn(name, **options) -> "Session":
    """Create an agent that keeps its conversation; takes the options of agent() except prompt and schema."""

class Session:
    def ask(self, message, *, schema=None, timeout=None):
        """Send the next message to this agent and return its answer; it remembers the earlier turns."""

def ask_user(question, options=None):
    """Pause the run, show the question in the thread, and return the answer.

    `options` is a list of strings; the answer is one of them. Without options the answer is free text.
    The run waits as long as it takes, across restarts.
    """

def shell(command, *, timeout=None) -> "ShellResult":
    """Run a command with the owner's shell tool; returns ShellResult(ok, exit_code, output) and never raises on a non-zero exit."""

def tool(toolkit, function, /, **arguments):
    """Call one of the owner's tools and return its JSON result; raises ToolError when the call fails."""

def run(name, args=None):
    """Run the saved workflow `workflows/<name>.py` inside this run and return what its main(args) returns."""

def phase(title):
    """Group the calls that follow under `title` in the graph; also works as `with phase(title):`."""

def log(text):
    """Show a progress line on the run."""

def now() -> "datetime.datetime":
    """Return the current UTC time, remembered across resume."""

def random() -> float:
    """Return a random float in [0, 1), remembered across resume."""

def send(to, text):
    """Message an agent of this run by name; "*" reaches every running agent."""

def inbox(wait_seconds=0) -> list[dict]:
    """Return messages agents sent to "script", waiting up to wait_seconds for the first one."""

class state:
    """The run's shared values, also readable and writable by its agents."""
    @staticmethod
    def get(key, default=None): ...
    @staticmethod
    def set(key, value): ...

class WorkflowError(RuntimeError):
    """Base error; `kind` is a stable word and the message says what to do next."""

class AgentError(WorkflowError):
    """kind: failed | cancelled | denied | timeout | schema | refused."""

class ToolError(WorkflowError):
    """kind: failed | denied | indeterminate (the call may have run; check its effect before repeating it)."""
```

### Errors that say what to do

| Mistake | Message |
|---|---|
| `parallel([agent(a), agent(b)])` | `parallel() takes functions. Write parallel([lambda: agent(a), lambda: agent(b)]) or use foreach(items, fn).` |
| No `main` | `Define def main(args): and return the report. MindRoom calls it.` |
| The script calls `main` itself | `Remove your own main() call. MindRoom calls main(args) and reports what it returns.` |
| SDK call from a raw thread | `Call agent() from main or inside foreach()/parallel(). Raw threads are not tracked, so resume could not match their calls.` |
| Unknown agent or model | `Unknown model 'opus5'. Configured models: haiku, opus, sol.` (the existing delegation refusal text) |
| Invalid schema | `schema is not valid JSON Schema: <path>: <reason>.` raised before any agent starts |
| Schema never met | `Agent 'verify-3' did not return JSON matching the schema after 5 attempts. Last error: 'problems' is a required property.` |
| Message to a finished agent | `'critic' has finished and will not read this. Use spawn() to keep an agent you want to talk to.` |

## Example workflows

Each is a complete script.

### 1. Diamond research

```python
from mindroom.workflow import agent, foreach, phase

def main(args):
    phase("Plan")
    plan = agent(f"Split this question into 4-8 independent research angles: {args['question']}",
                 schema={"type": "array", "items": {"type": "string"}})
    phase("Research")
    notes = foreach(plan, lambda angle: agent(f"Research: {angle}\nReturn findings with sources.", model="haiku"))
    phase("Synthesize")
    return agent("Answer the question from these notes. Mark anything only one note supports.\n\n"
                 f"Question: {args['question']}\n\n" + "\n\n---\n\n".join(n for n in notes if n), model="opus")
```

### 2. Audit with an adversarial check (maker is not checker)

```python
from mindroom.workflow import agent, foreach, phase

FINDINGS = {"type": "array", "items": {"type": "object", "required": ["file", "line", "claim"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "claim": {"type": "string"}}}}
VERDICT = {"type": "object", "required": ["real", "reason"],
           "properties": {"real": {"type": "boolean"}, "reason": {"type": "string"}}}

def main(args):
    phase("Audit")
    per_file = foreach(args["files"], lambda f: agent(f"Audit {f} for {args['issue']}.", schema=FINDINGS, model="opus"))
    findings = [x for group in per_file if group for x in group]
    phase("Verify")
    verdicts = foreach(findings, lambda x: agent(
        f"Try to disprove this finding. Read the code first.\n{x}", schema=VERDICT, model="sol"))
    real = [x | {"reason": v["reason"]} for x, v in zip(findings, verdicts) if v and v["real"]]
    return {"confirmed": real, "rejected": len(findings) - len(real)}
```

### 3. Implement, review, fix until the tests pass

```python
from mindroom.workflow import agent, ask_user, phase, shell, spawn

def main(args):
    dev = spawn("dev", model="opus")
    reviewer = spawn("reviewer", model="sol", system="You review diffs. Reply APPROVE or list blocking problems.")
    phase("Implement")
    dev.ask(f"Implement this in {args['worktree']} with tests, and commit:\n{args['task']}")
    for round in range(1, 6):
        phase(f"Round {round}")
        tests = shell(f"cd {args['worktree']} && {args['test_command']}", timeout=1800)
        review = reviewer.ask(f"Review `git diff main` in {args['worktree']}.")
        if tests.ok and review.strip().startswith("APPROVE"):
            return f"Done in {round} round(s).\n\n{review}"
        dev.ask(f"Fix and commit.\n\nTest output:\n{tests.output}\n\nReview:\n{review}")
    return ask_user("Five rounds did not converge. Keep going?", ["Stop here", "Five more rounds"])
```

### 4. Panel and judge

```python
from mindroom.workflow import agent, foreach, phase

SCORE = {"type": "object", "required": ["score", "why"],
         "properties": {"score": {"type": "number"}, "why": {"type": "string"}}}

def main(args):
    phase("Draft")
    drafts = foreach(args.get("models", ["opus", "sol", "fable"]),
                     lambda m: agent(f"Write a plan for: {args['task']}", model=m, name=f"planner-{m}"))
    plans = [p for p in drafts if p]
    phase("Judge")
    scores = foreach(plans, lambda p: agent("Score this plan 1-10. Be harsh.\n\n" + p, schema=SCORE, model="sol"))
    best = max(range(len(plans)), key=lambda i: scores[i]["score"] if scores[i] else 0)
    phase("Merge")
    others = "\n\n".join(p for i, p in enumerate(plans) if i != best)
    return agent(f"Improve the winning plan with the best ideas of the others.\n\nWinner:\n{plans[best]}\n\nOthers:\n{others}")
```

### 5. Loop until dry

```python
from mindroom.workflow import agent, foreach, log, phase

ISSUES = {"type": "array", "items": {"type": "object", "required": ["id", "text"],
          "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}

def main(args):
    found = {}
    for round in range(1, args.get("max_rounds", 8) + 1):
        phase(f"Round {round}")
        known = "\n".join(f"- {text}" for text in found.values()) or "(none yet)"
        batches = foreach(args["areas"], lambda area: agent(
            f"Find bugs in {area}. Skip these known ones:\n{known}\nGive each bug a short stable id.", schema=ISSUES))
        new = {i["id"]: i["text"] for batch in batches if batch for i in batch if i["id"] not in found}
        log(f"Round {round}: {len(new)} new, {len(found)} known")
        if not new:
            break
        found |= new
    return [{"id": key, "text": text} for key, text in found.items()]
```

### 6. Agents that talk to each other

The script relays between two sessions (a debate):

```python
from mindroom.workflow import spawn

def main(args):
    pro = spawn("pro", system="Argue for the proposal. Concede what is right.")
    con = spawn("con", system="Argue against the proposal. Concede what is right.")
    turns = [args["proposal"]]
    for _ in range(3):
        turns.append(pro.ask("Respond to this:\n" + turns[-1]))
        turns.append(con.ask("Respond to this:\n" + turns[-1]))
    return spawn("judge", model="opus").ask("Decide, and say why.\n\n" + "\n\n---\n\n".join(turns))
```

The agents talk directly, with no script in the middle (a team):

```python
from mindroom.workflow import agent, parallel, phase, state

def main(args):
    phase("Build")
    api, ui = parallel([
        lambda: agent(f"Build the API for: {args['feature']}\nBefore you write code, agree on the JSON contract with "
                      "'frontend' (send_peer_message, read_peer_messages) and save it with "
                      "set_shared_state('contract', ...).", name="backend"),
        lambda: agent(f"Build the UI for: {args['feature']}\nWait for 'backend' to propose the JSON contract "
                      "(read_peer_messages), push back where it does not fit the UI, then build against it.",
                      name="frontend"),
    ])
    phase("Integrate")
    return agent(f"Report where either side breaks this contract.\n\nContract: {state.get('contract')}"
                 f"\n\nBackend:\n{api}\n\nFrontend:\n{ui}", model="sol")
```

## Communication between agents

Yes, it is possible, in three ways, and all three are in v1.

### 1. Through the script (sessions)

`spawn()` returns a session and `Session.ask()` continues it with `continue_subagent` semantics: same child, same history, same model.
The script decides who hears what, which fits debates, critique loops and maker and checker pairs.
In the graph a session is one node with a turn count; opening it shows its turns.

### 2. Directly (peer messages and shared state)

Every agent of a run gets the run-scoped toolkit `workflow_peers` automatically.

```text
send_peer_message(to, text)        to is an agent's name, "*" for every running agent, or "script"
read_peer_messages(wait_seconds=0) unread messages as [{from, text, sent_at}]; waits for the first one
list_peers()                       name, status and label of every agent in the run
get_shared_state(key=None)         one value, or all values
set_shared_state(key, value)       last write wins
ask_user(question, options=None)   the same question card as the script's ask_user
```

Delivery semantics:

- A message is a durable row in the run store, ordered by a run-wide sequence number, with one read cursor per recipient node.
- Mailboxes are keyed by name, so a message sent before its recipient starts is delivered when it starts.
- `read_peer_messages(wait_seconds=N)` blocks that tool call and wakes on the first message; N is capped at 3600 so a provider request does not sit idle forever.
- A session that is between turns keeps its mailbox; queued messages are put in front of its next `ask` as "Messages received since your last turn".
- Several agents may share a name (a `foreach` of reviewers named `reviewer`); each gets its own copy.
- A message nobody can ever read (the name has no queued, running, waiting or session node) returns the error from the table above instead of vanishing.
- A message is at most 64 KiB and a state value at most 256 KiB, because both ride in SQLite rows and frames; larger data goes in a workspace file whose path is sent.
- Messages and shared state survive restarts and resume with the run.

In the graph a message is an edge from sender to recipient with a count; `get_workflow` returns shared state.
Not in v1: pushing a message into a busy agent's next tool result (it reads its inbox when it chooses), and messages between different runs.

### 3. With people and with the room

The script's `ask_user` and the agents' `ask_user` tool post the same question card and wait.
Workflow agents keep `matrix_message`, so they can post in the thread or message another room agent as today; that starts an ordinary turn outside the run and is not drawn in the graph.

## What the user sees

1. **Start card.** The agent's reply contains a card: title, status, phases, `12 done, 8 running, 1 waiting`, cost so far and projected, elapsed time, and the buttons Graph and Stop.
2. **Jobs bar.** One row per run with the same numbers; the run's agents are not listed one by one there.
3. **Graph panel.** Phases top to bottom, fan-out groups with a progress count, one chip per agent with status, model, tokens, cost and time, and curved edges for messages.
   Selecting a chip shows its prompt label, result preview and error, with Open (the existing Sub-agents view of that agent's live text and tool calls) and Stop (cancel only that agent).
4. **Questions.** `ask_user` shows a card with option buttons or a text box; the answer is visible to the room afterwards.
   In a client without the card, `!answer <text>` answers the oldest open question in the thread.
5. **Approvals.** A workflow agent that needs an approval shows the existing approval card, named after that agent; its chip and the run turn to Waiting for you.
   Nothing times out: the run waits until the user decides, cancels, or the run's lifetime ends.
6. **Large run notice.** When a run has scheduled more than 25 agents or its projected cost passes 10 USD, the card shows `Large run` once, with the projection; it never pauses the run.
   With `max_cost_usd` the run pauses at that amount and asks: continue with the same amount again, or stop.
7. **End.** The card turns Done, Failed, Cancelled or Interrupted with totals, and the calling agent replies with the report.
   The card of a finished run still opens its graph.
8. **Cancel and resume.** Stop on the card, the jobs bar or the panel, `!cancel_job`, or asking the agent all cancel the run.
   Resume on the card, or asking the agent, continues it; saved calls are not paid for twice.
   After a MindRoom restart, runs continue on their own.
9. **Saved workflows.** A saved workflow is the file `workflows/<name>.py` in the agent's workspace.
   The user runs one by asking ("run review-branch on main"); `!workflows` lists the saved ones and the running ones.

## Internals

### Where the script runs

- **Worker backend configured (Docker, Kubernetes):** each attempt runs in its own dedicated worker, exactly as a background script does today.
- **No worker backend (a local single-user install):** the script runs as a child process of the primary under the primary's shell supervisor, on Linux and macOS, with no extra setting.
  The repository's trust model already says "no worker means full trust"; `workflow` is flagged `executes_code`, so enabling it for an agent is the operator's grant, the same as `shell`.
- **Never inside the primary process.** A script that blocks or crashes must not stop the event loop.
- macOS is refused for scripts today because only Linux kills the script's process group when the supervisor dies.
  The workflow runner closes that gap itself: a watchdog thread polls `os.getppid()` every second and kills its own process group when the parent is gone, and an orphan's capability no longer authenticates anyway.
- Kubernetes keeps its gateway-only listener requirement, because that is the existing boundary between workers and the primary API.

### Runs, attempts and nodes

A **run** (`wf-<32 hex>`) is the durable thing: owner (agent, requester, room, thread, execution identity), title, source and its digest, `args`, limits, state, result, depth, and the parent run and node for a nested run.
An **attempt** is one script run of the existing script runtime (`script-<32 hex>`, a `ScriptRunRecord` with `workflow_run_id` set); a run has one live attempt at a time.
A **node** is one SDK call: `agent`, `turn` (a session's `ask`), `session`, `ask`, `tool`, `value` (`now`, `random`), `group` (`foreach`, `parallel`, `run`).

Run states: `starting`, `running`, `waiting` (nothing runs and a person must act), `completed`, `failed`, `cancelled`, `interrupted`.
Node states: `queued`, `running`, `waiting` (on `approval`, `user` or `budget`), `completed`, `failed`, `cancelled`.

The store is SQLite at `<control_root>/workflow_runs/workflow_runs.sqlite3` with the tables `workflow_runs`, `workflow_nodes`, `workflow_messages`, `workflow_cursors`, `workflow_state` and `workflow_log`.
Node results are stored in full there, so nothing is lost to a size cap.
A write-only export in the owner's workspace, `.mindroom/workflow_runs/<run_id>/` (`source.py`, `report.md` or `result.json`, `nodes.jsonl`), lets agents read past runs with file tools; MindRoom never reads it back.
Finished runs are kept for the script retention window (30 days, `MINDROOM_SCRIPT_RETENTION_SECONDS`).

The run itself is one background job of kind `workflow` in the existing `ToolJobRuntime`.
That gives it a jobs-bar entry, every existing cancel path, the `job` tool, and delivery of the report to the calling agent, with no new machinery.
The job kind is one the runtime continues across a restart (the `continues` hook that today keeps subagent jobs), so the job stays `running` while the engine starts the next attempt.
Cancelling a run revokes the attempt's capability, cancels every live node job, closes open question cards, and ends the run `cancelled`; completed nodes stay saved for a later resume.

### How the script talks to MindRoom

MindRoom launches `source + trailer`, where the trailer calls `mindroom.workflow._run_main(globals())`: it fetches `args`, calls `main(args)`, and posts the return value or the traceback.
The SDK calls routes under `/api/script-gateway/workflow/`, authenticated by the attempt's existing capability token, so the Kubernetes gateway-only listener already covers them.

```text
GET  run                      -> {run_id, attempt, args}
POST nodes {key, kind, args, meta}  -> {id, state, cached, result, error}
GET  nodes/{id}?wait=25       -> the same receipt; long-poll until it changes
POST log {text}    POST finish {result | error}
POST messages {to, text}    GET messages?wait=N    GET state    PUT state/{key}
```

A blocked `agent()` is a thread in a long-poll, for hours if needed; a gateway restart looks like a retryable transport error and the SDK keeps polling.
Only the run's current attempt is served; an older attempt gets `409 superseded` and exits, so a zombie process cannot act.
A request is at most 1 MiB and a result at most 8 MiB, which protects the primary's memory; larger data goes through workspace files.
The 64 KiB receipt cap of plain script calls does not apply to nodes; `tool()` nodes keep it because they reuse the script broker's receipts.

### Replay and resume

Every node has a **key** and a **digest**.

- The key is `lane@index`: the root lane is `""`, the n-th SDK call in a lane has index n, and `foreach` or `parallel` at index i opens child lanes `lane/i:<item>`.
  For `foreach`, `<item>` is a hash of the item's JSON value (with an occurrence counter for duplicates), so adding, removing or reordering items keeps the other lanes' keys.
  For `parallel` it is the position.
- The digest is the SHA-256 of the call's kind and arguments (prompt, schema, target, model, system, tools, profile, mode), computed again by the gateway.

On a node request the gateway looks up the key.

- **Same digest, completed:** the saved result returns at once, marked `cached`, so no agent runs and nothing is paid.
- **Same digest, still live** (an `ask` that is open, a job that survived): the call attaches to it.
- **Different digest, or failed, cancelled or missing:** the call runs.
  If it replaced a saved node, that lane is dirty from this index on: saved nodes later in the lane, and in lanes opened at or after this index, are dropped, because an earlier call's side effects may have changed what they would see.
  Sibling lanes are untouched; they miss on their own when their prompts changed.

This is Claude Code's rule ("the first changed call and everything after it run again") made exact per lane, so one failed agent in a fan-out of 500 reruns one agent, not 499.

**Resume** starts a new attempt of the same run and replays the script from the top.
It happens in three ways: `resume_workflow` (optionally with an edited script), the Resume button, and automatically after a restart or an infrastructure interruption (runtime restart or shutdown, plugin reload, worker or supervisor loss).
An edited script needs no special handling: unchanged calls hit, changed calls and what follows them in their lane run.
Automatic resume stops after 5 attempts in a row that finish no new node, because a script that takes the runtime down on start would otherwise loop forever.
Agents that were running when the attempt died start over; a `tool()` call that was running comes back as `ToolError(kind="indeterminate")`, as script calls do today.

**Determinism.** Nothing throws: Python's logging and HTTP stack need the real clock.
A call whose prompt contains the time or a random value simply misses the cache and runs again.
`now()` and `random()` are `value` nodes, so their results replay.
When a completed node is replaced although the script's digest and every earlier call in its lane are unchanged, the run logs: `Call n17 changed between attempts without a script edit. It probably contains the time or a random value; use now() or random() from mindroom.workflow.`

**Sessions.** A `turn` whose digest matches the saved turn at that position hits.
A new turn after the last saved one continues the saved child session.
A turn that changes in the middle starts a fresh child whose first prompt carries the saved prompts and answers of the earlier turns, because the script has already consumed those answers.

### How `agent()` maps to delegation

- **Target.** Resolved and authorized by the existing delegation code with a third grant, `workflow`.
  A copy of the owner is always allowed, plain or shaped with `system`, `tools`, `skills`, `knowledge` or `profile`.
  Another agent is allowed when it is in the owner's `delegate_to` or is an agent of the run's room that the requester may address; it runs as configured, with its own tools, knowledge and memory, and `model` may still be overridden.
- **Child turn.** `prepare_child_turn` at depth `run.depth + 1`, run as a delegation job (`delegation_call_job`) in `ToolJobRuntime` with the deterministic job ID `sha256(owner, run_id, key)`.
  The job path is the one that already has everything a node needs: the Sub-agents live feed, the delegation record and receipt, approval cards that resume the exact paused run, per-agent cancel, plugin delegation hooks, and usage.
- **Mode.** Standard by default, so the child has its full tool schemas and can pause for approval; `minimal=True` follows `run_subagent`'s rules.
- **Tools.** A plain copy inherits every tool of the owner, including `delegate`, `memory` and `workflow`, plus `workflow_peers`; `tools=[...]` narrows it.
- **Knowledge and history.** The child runs under the owner's room, thread and requester identity, so its knowledge bases, memory and Matrix tools reach what the owner's reach; the thread is not preloaded, because a fresh context is the point.
- **Model.** Any configured model per call; the requester's budget may still swap in its fallback, as for every child.
- **Result.** The node stores the child's full text and its `usage` and `cost_usd` when the job settles.
- **Nesting.** A child may call `start_workflow` itself; that run has `depth + 1` and shows as a `group` node of its parent with its cost rolled up.

### Concurrency

`foreach` and `parallel` run their functions on threads in the script (one per item, at most 256 per call; more items are fed as threads free up).
The limit that matters is in the gateway: one semaphore per run, `max_concurrent` (default 16, any value the caller asks for unless the operator set a ceiling).
It counts running `agent`, `turn` and `tool` nodes across all lanes and nesting levels; a node that waits for a person does not count.
The script broker's per-run lock becomes a per-run semaphore of size 1 for plain scripts (no change) and of the run's limit for workflows, so `tool()` and `shell()` calls in parallel lanes run in parallel.

**Prompt-cache stagger.** Agents with the same target, model, mode, system prompt, tools and schema share a prompt prefix.
When several are queued at once, the first starts and the others are held until its first live frame arrives or 5 seconds pass (`prefix_stagger_seconds`, 0 turns it off); then they start together and read the cache the first one wrote.

### Pauses for people

- **`ask_user`** creates an `ask` node in state `waiting` and posts an agent message with `io.mindroom.workflow_question` (`{run, node, text, options, free_text}`) and a plain-text fallback body.
  The answer is the event `io.mindroom.workflow_answer` (`{run, node, option | text}`, related to the card) or `!answer`, accepted only from the run's requester, the rule approvals already follow.
  The card is edited to show the answer, and the node completes with it, so a resumed run does not ask twice.
- **Approvals.** The node's delegation job goes to `awaiting_approval` and shows the existing card; the node is `waiting` on `approval`.
  Workflow jobs wait with no approval timeout.
- **Budget.** With `max_cost_usd`, reaching the amount starts no new node, lets running ones finish, and asks through a system `ask` node.
- The run is `waiting` when nothing runs or is queued and at least one node waits; the script simply stays blocked in its long-poll.
- A question survives restarts: an answer that arrives while no attempt runs is saved on the node and returned on replay.
  An approval does not: the job runtime ends a job that waits for approval at a restart, so that agent starts over on resume and asks again (known limit).
- A run lives `max_runtime_hours` (default 72) from its start or its last resume, then ends `interrupted` and can be resumed.

### Structured output

With `schema`, the gateway first checks the schema itself (`jsonschema`'s `check_schema`) and fails the call before any agent starts when it is invalid.
The child's prompt gets the schema and the instruction to end with one JSON value in a fenced block.
The gateway extracts and validates it; on failure it continues the same child (cheap, the prompt is cached) with the validation error, up to 5 attempts in total.
Then the call raises `AgentError(kind="schema")` with the last validation error.
No injected tool is needed, so it works in standard and minimal mode and with every provider.

### Cost

Each agent node carries `input_tokens`, `output_tokens`, `tool_calls` and `cost_usd`, live from the subagent frames and final from the child record.
The run total is their sum plus nested runs.
The projection is `spent + average cost of finished agent nodes x (running + queued agent nodes)`; it only knows calls the script has already made.
The large-run notice (more than 25 agent nodes, or a projection over `large_run_notice_usd`, default 10) is advisory.
There is no hard cap by default; `max_cost_usd` pauses and asks.
Existing budgets apply unchanged: each child turn passes through the requester's budget monitor.

### Saved workflows

- A saved workflow is `workflows/<name>.py` in the owner's workspace; `<name>` matches `[a-z0-9][a-z0-9_-]{0,63}`.
- Its description is the first line of the module docstring, and an optional literal `ARGS = {"base": "Branch to compare against"}` documents its arguments.
  `list_workflows` reads both with `ast`, never by running the file.
- `save_workflow(run_id, name, description)` writes that run's source there, adding the docstring when the file has none; it refuses to overwrite a different file and says to pick another name or edit the file.
- `start_workflow(name=...)` snapshots the file at start.
- `run(name, args)` loads the file inside the current run and calls its `main`; its calls are nodes of a `group` in this run's graph.
- Because it is a workspace file, the agent can also write and edit workflows with its file tools.

### Graph data model

A script's data dependencies are invisible, so the graph does not pretend to know them.
Its structure is what the engine does know: phase, lane (which `foreach` or `parallel` group a call belongs to), start order, sessions, and messages.

- **Room state** (`io.mindroom.background_jobs`, unencrypted, IDs only): the run's job is `{job_id, thread_id, kind: "workflow", tool: "workflow", status, started_at, workflow: "<run id>"}`, and each agent node's subagent job gains `"workflow": "<run id>"`.
- **Live frames** (`io.mindroom.workflow`, ephemeral, encrypted in encrypted rooms, same transport as `io.mindroom.subagents`): a full snapshot every 10 seconds while the run is active and deltas in between.

```json
{"v": 1, "run": "wf-...", "seq": 12,
 "meta": {"agent": "dev", "thread_id": "$...", "title": "Audit auth module", "status": "running", "attempt": 1,
          "started_at": 1795000000, "ended_at": null, "max_concurrent": 16, "max_cost_usd": null, "large": false,
          "counts": {"queued": 4, "running": 12, "waiting": 1, "completed": 30, "failed": 1, "cached": 0},
          "usage": {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0, "cost_usd": 3.41, "projected_cost_usd": 9.8}},
 "snapshot": {
   "phases": ["Audit", "Verify"],
   "groups": [{"id": "n3", "kind": "foreach", "phase": 0, "parent": null, "total": 40}],
   "nodes": [{"id": "n17", "kind": "agent", "label": "Audit auth/session.py", "name": "agent-17", "phase": 0,
              "group": "n3", "status": "running", "waiting_on": null, "agent": "dev", "model": "opus",
              "run": "<delegation id>", "subagent": "<subagent id>", "job": "<job id>", "session": null, "turns": 1,
              "started_at": 1795000100, "ended_at": null, "cached": false, "error": null,
              "usage": {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0, "cost_usd": 0.12}}],
   "edges": [{"from": "n17", "to": "n21", "kind": "message", "count": 2}],
   "log": ["Round 1: 12 new, 0 known"]}}
```

- A node's `label` is its `name`, or the first line of its prompt, redacted and cut to 80 characters like a subagent title.
- A frame is at most 60,000 bytes, about 300 node rows.
  A larger graph sends every group with its counts, all nodes that are not completed, and the newest completed ones that fit; the client shows "and N more done".
- A node's live text and tool calls are not in this frame: `run` and `subagent` join it to the existing `io.mindroom.subagents` feed.
- **The card message** carries `io.mindroom.workflow_run` (`{run, title, status, counts, usage, phases, attempt, ended_at}`) and is edited when the run starts waiting, at most once a minute while it runs, and when it ends.
  The final edit includes the last snapshot (cut to 40 KB the same way), so a finished run's graph opens from history.
- **Control.** Stop reuses `io.mindroom.background_job_cancel` with the run's or a node's job ID, and Resume is the new event `io.mindroom.workflow_control` (`{run, action: "resume"}`), accepted from the requester.

### MindRoom Chat view

- No graph library: the layout is phase bands, group boxes and node chips in start order, which is the true structure, with an SVG overlay for message edges.
  The panel is lazy-loaded; the initial bundle has about 1.8 KB of headroom.
- Frame parser `matrix/protocol/workflowFrame.ts` and run store `messages/live/workflowRunStore.ts` sit beside their subagent counterparts.
- `messages/workflow-run/WorkflowRunCard.tsx` and `WorkflowQuestionCard.tsx` render the two message contents; the question card reuses the `QuestionCard` primitives.
- `subagents/workflow/WorkflowPanel.tsx` is the graph panel (layer 8, so the jobs bar and the room can import it); `navigation/workflowPanel.ts` holds its open state, as `navigation/subagentsPanel.ts` does.
- Reused: the jobs-bar rich row cells (calls, tokens, cost, elapsed), `SubagentRunCard` styling for chips, `openSubagentsPanel` for Open, `cancelBackgroundJob` for Stop.
- The jobs bar shows one row per `kind: "workflow"` job and hides subagent jobs that carry `workflow`.
- "Workflow" already names the thread tabs in Chat code (`WorkflowTabs`); the new code uses `workflowRun` in identifiers and "Workflow" only in labels.
- All strings go in the 17 locale overlays; the PR carries placeholder-data screenshots of the card, the panel, the question card and the jobs-bar row.

## Restrictions

### Kept, each with its reason

| Restriction | Reason |
|---|---|
| Operator `tool_approval` rules apply to `start_workflow`, to script `tool()` calls and inside every agent | The operator's policy, unchanged. |
| Requester authorization is rechecked on every node | A run must stop acting for someone who lost access (existing check). |
| Only the requester answers questions, approves and resumes | The existing rule for approval cards. |
| Budgets | Existing per-requester budget monitor, applied per child turn. |
| Depth 3 (`MAX_DELEGATION_DEPTH`) for nested runs and agents | A workflow that starts itself would recurse forever. |
| `max_concurrent` default 16 | Provider rate limits; it is a default the caller raises. |
| `max_concurrent_runs` 8 per requester and agent (toolkit option) | Each run holds a process or a worker. |
| `max_runtime_hours` 72 (toolkit option) | An abandoned run must release its worker; resume extends it. |
| `max_tool_calls_per_minute` 600 for script `tool()` calls (toolkit option) | A `while True: tool(...)` loop must not starve the primary. |
| Script never runs in the primary process | A blocking or crashing script must not stop the event loop. |
| Kubernetes needs the gateway-only listener | The existing worker boundary. |
| Script `tool()` cannot call `compact_context`, `dynamic_tools`, `slack`, or functions that control a model step | They act on the calling turn (its context, its loaded tools, its hook source), and a script has none. |
| Request 1 MiB, result 8 MiB, source 128 KiB | Primary memory and the launch snapshot; larger data goes in workspace files or imported modules. |
| SDK calls only from `main` or inside `foreach`/`parallel` | Replay needs a stable lane for every call. |
| Minimal-mode agents cannot pause for approval | Existing: they have one shell tool, and approval-gated functions are not offered to them. |

### Dropped, compared with today's scripts and Dynamic Workflows

- No step, participant or agent caps (64, 8 and 16 today).
- No one-hour limit; runs last days and wait for people.
- No pre-approval list for agent tools: agents inherit tools like `run_subagent`, and an approval pauses the node instead of failing it.
- `delegate`, `memory`, `workflow`, `script`, `browser`, `claude_agent`, `agent_manager`, `contacts`, `self_config` and `skill_manage` are no longer barred, for agents or for script `tool()` calls.
  If one of them breaks without a turn, it returns to the kept list with a one-line reason in the code.
- No extra approval layer for script `tool()` calls: the owner could make the same call in a turn, so only operator rules and a function's own confirmation gate apply.
- No data-grant refusals: agents have the owner's knowledge, memory and Matrix access.
- Room agents keep their tools and may get a `model` override.
- No serialized tool calls, no 30 calls per minute, no 64 KiB cap on agent results.
- No `MINDROOM_SANDBOX_EXECUTION_MODE` flag and no Linux-only rule for local runs.

## The old toolkit

`dynamic_workflow` is not changed and its YAML specs do not run on the new engine.
Both toolkits define `list_workflows`, so config validation refuses an agent that has both and says to remove `dynamic_workflow`.
The docs mark `dynamic_workflow` as superseded when saved workflows land.
Recommendation: delete it (about 3,000 source lines with `dynamic_workflows/`) two weeks after Bas's agents run on `workflow`, with a config error that names the replacement.

## v1 and later

In v1: everything above.
Later, in rough order of value: pushing peer messages into a busy agent's next tool result; resuming an agent that waited for approval across a restart; stopping the script process while a run waits for hours and relaunching it on the answer (replay makes that possible); bundled and shared workflows; a `!workflow run` command with no model turn; stall detection with automatic agent restart.

## Testing

- Unit: store and replay rules (hit, attach, dirty lane, item-keyed lanes, session divergence), the engine with a fake child runner (concurrency, stagger, pauses, cost, budget pause), the SDK against a fake gateway, frame fitting.
- Integration: the existing script-runtime test harness runs real scripts in local mode; each example workflow above runs with a synthetic model.
- Live (throwaway stack): the diamond with a restart in the middle, an approval pause, an `ask_user`, a cancel, a resume with an edited script, and 200 parallel synthetic agents for frame size and jobs-state size.
- Chat: vitest for the parser, store and presenter; Playwright with placeholder frames for the card, panel, question card and jobs-bar row.

## Pull requests

| PR | Content | Net source lines (estimate) |
|---|---|---|
| B1 | Run store, engine core, gateway routes, SDK core (`main`, `tool`, `shell`, `phase`, `log`), `start_workflow`, `get_workflow`, `cancel_workflow`, the `workflow` job kind, local placement | +650 |
| B2 | `agent`, `foreach`, `parallel` as delegation jobs: targets, concurrency, stagger, approval pauses, cost | +450 |
| B3 | Replay, `resume_workflow`, automatic resume, `now`, `random` | +350 |
| B4 | Graph frames, card message, jobs-state fields, `io.mindroom.workflow_control` | +350 |
| B5 | `schema`, `ask_user`, `!answer`, `max_cost_usd`, large-run notice | +400 |
| B6 | Saved workflows (`save_workflow`, `list_workflows`, `name=`, `run`, `!workflows`), sessions (`spawn`), `workflow_peers` | +500 |
| C1 | Chat: parser, store, run card, question card, jobs-bar row | +450 |
| C2 | Chat: graph panel with message edges | +500 |
| D | Docs page `docs/tools/workflows.md` grows inside B1 to B6; the last PR marks `dynamic_workflow` superseded | docs only |

Backend total about +2,700, Chat about +950; removing `dynamic_workflow` later gives back about 3,000.
Order: B1, B2, B3, B4, then C1 beside B5 and B6, then C2.
Each backend PR is usable on its own from B2 on.

## Open questions

1. Which other agents may a workflow run: `delegate_to` plus room agents the requester may address (recommended, 75%), or `delegate_to` only?
2. Remove `dynamic_workflow` after two weeks of dogfooding (recommended, 80%), or keep both?
3. Local runs with no setting whenever no worker backend is configured (recommended, 85%), or keep an explicit opt-in?
4. `ask_user` for the user and `Session.ask` for agents (recommended, 80%), or the shorter `ask` for the user as in the brief?
5. Failed items in `foreach` and `parallel` give `None`, as in Claude Code (recommended, 70%), or raise after all items finish?
6. Default `max_runtime_hours` of 72 (recommended, 65%), or no lifetime at all?
