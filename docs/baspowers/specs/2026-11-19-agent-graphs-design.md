# Agent graphs: workflows as Python scripts

Date: 2026-11-19. Status: decided by Bas (approach B), revised after review rounds 1 to 3, locked, ready to build.

## Intent

An agent can hand a task to many agents at once and keep the plan in code: fan out, verify, loop until a check passes, let agents talk to each other, ask the user, and report once.
The orchestration is a Python script the agent writes, runs in the background, and can save and rerun.
The user watches the run as a live graph in the thread and can cancel or resume it.

Said by Bas: workflows are Python scripts on the existing script runtime, calling agents through an SDK.
Said by Bas: v1 has parallel and foreach, structured outputs, background runs with cancel, resume by replay, `ask` and approval pauses, a live graph in MindRoom Chat, and saved workflows with arguments.
Said by Bas: design for power, not safety, and make it obvious for agents and for users.
Assumed: the user-facing word is "workflow"; "agent graph" names the picture of a run.

Success means four things.
An agent writes a correct workflow from the `start_workflow` tool description alone.
A run of hundreds of agents survives a restart, a missing approval, and a bug in its own script without losing finished work.
No write happens twice because of a restart or a resume.
The user always sees what runs, what waits for them, and what it costs.

## Mental model

For the user: a workflow is a small program your agent writes that puts many agents to work at once.
It runs in the background while you keep chatting, shows every agent as a box in a live graph, stops to ask you when it needs an answer or an approval, and ends with one report.
You can cancel it at any time, and a stopped run continues where it left off.
Everything workflow agents say or send each other is visible to the room, like sub-agent output today; run workflows in a private room for private work.

For the agent: a workflow is a Python file with `def main(args)`.
`agent(prompt)` runs one agent turn and returns its answer.
Everything else is ordinary Python.
Every finished call is saved, so a resumed run reuses what is done.

## Approaches considered

1. **Python script on the script runtime (chosen by Bas).** Loops, branches and data flow are plain Python, the runtime already has restart recovery, cancel, a tool gateway and approval cards, and models write Python well.
2. **Declarative YAML DAG.** Rejected: every control structure (when, repeat, foreach, check) needs its own schema and its own bugs, and conversations between agents do not fit a static graph.
3. **Hybrid (YAML with script steps).** Rejected: two languages to learn and two engines to keep correct.

## The `workflow` toolkit

Six functions.
`start_workflow` carries the whole teaching text; the others are one-liners.

```text
start_workflow(source=None, path=None, name=None, args=None, title=None, max_concurrent=16, max_cost_usd=None, resource_profile=None)
get_workflow(run_id, nodes="active", messages=False)
cancel_workflow(run_id)
resume_workflow(run_id, source=None, path=None, rerun=None)
list_workflow_runs()
save_workflow(run_id, name, description=None)
```

No name clashes with `dynamic_workflow`, so an agent can have both toolkits while it migrates.

### Exact description of `start_workflow`

The text is built per agent, as `run_subagent`'s is: the last two lines list the configured models and the agents this agent may run.

```text
Run a workflow: a Python script that puts many agents to work in the background.

Use it when a task needs many agents, the same step over many items, an independent check, or a loop with a hard stop.
Give exactly one of `source` (inline Python), `path` (a file in your workspace), or `name` (a saved workflow, `workflows/<name>.py`).
The script defines `def main(args):` and returns the final report, a string or a JSON value. MindRoom calls `main`; do not call it yourself.

    from mindroom.workflow import (agent, foreach, parallel, spawn, ask_user, shell, tool, phase, log,
                                   run, send, inbox, state, now, random, PartialFailure)

    def main(args):
        phase("Research")                        # the same step over many items, in parallel
        try:
            notes = foreach(args["topics"], lambda t: agent(f"Research {t}. Cite sources."))
        except PartialFailure as e:              # some items failed: keep the rest, or do not catch and the run fails
            notes = e.ok
        phase("Write")
        draft = agent("Write a report from these notes:\n\n" + "\n\n".join(notes))
        for _ in range(3):                       # a bounded loop with an independent check
            check = agent("List factual problems in this report.\n\n" + draft,
                          schema={"type": "object", "required": ["problems"],
                                  "properties": {"problems": {"type": "array", "items": {"type": "string"}}}})
            if not check["problems"]:
                break
            draft = agent(f"Fix these problems: {check['problems']}\n\n{draft}")
        return draft

    def review_loop(task, test_command):         # two agents that keep their conversation, and a hard check
        dev, reviewer = spawn("dev"), spawn("reviewer", system="Review diffs. Reply APPROVE or list problems.")
        dev.ask(f"Implement and commit: {task}")
        for _ in range(5):
            tests, review = shell(test_command), reviewer.ask("Review `git diff main`.")
            if tests.ok and review.startswith("APPROVE"):
                return review
            dev.ask(f"Fix and commit.\n\nTests:\n{tests.output}\n\nReview:\n{review}")
        return "Not approved after 5 rounds:\n" + review

- agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None, tools=None, skills=None, knowledge=None,
        profile=None, minimal=False, timeout=None, output_path=None, dedicated_worker=False, resource_profile=None)
  runs one agent turn and returns its answer: text, or parsed JSON when `schema` (a JSON Schema) is given.
  The agent is a fresh copy of you with all your tools and no memory of this conversation: put everything it needs in the prompt.
  agent="other" runs that configured agent with its own tools. name is only a label and the address for messages; it does not pick an agent.
  model: one of the models below. system: replaces the system prompt. tools, skills, knowledge: lists that narrow a copy of you.
  profile: a subagents/<profile>.md file. minimal=True: a cheap copy with one shell tool. timeout: seconds, then the turn is cancelled.
  output_path: also write the answer to this workspace file. dedicated_worker=True: its own container. resource_profile: a worker size.
  A failed turn raises AgentError.
- foreach(items, fn) calls fn(item) for every item in parallel and returns the results in order.
  parallel([fn1, fn2]) does the same for different functions; for a loop over items use foreach, not a list of lambdas.
  If any call fails, both wait for the others and then raise PartialFailure: .ok holds the results that succeeded,
  .results every position, .errors the failures by position. Not caught, it fails the run.
- s = spawn(name, ...options of agent) keeps one agent's conversation: s.ask(message) sends the next message and returns the answer.
  Ask two sessions at once with parallel([lambda: a.ask(x), lambda: b.ask(y)]).
- ask_user(question, options=None) asks the user in the thread and returns the answer. Only that branch waits; parallel branches keep running.
- shell(command) runs a command with your shell tool and returns .ok, .exit_code and .output. tool(toolkit, function, **arguments) calls any other tool of yours.
- phase(title) groups the calls that follow in the graph; log(text) shows a progress line. run(name, args) calls a saved workflow inside this one.
- Agents in a run can talk to each other: each has the tools send_peer_message(to, text) and read_peer_messages(wait_seconds),
  addressed by the name you gave them. Say in each prompt whom to talk to. The script joins in with send(to, text),
  inbox(wait_seconds), state.get(key) and state.set(key, value); agents read and write the same state.

Every agent, tool and shell call is saved with its result. The Python between the calls runs again on every resume,
so do writes (files, git, HTTP) through shell(), tool() or an agent, never with plain Python.
resume_workflow reuses saved calls: in each sequence of calls, those before the first changed call are reused; that call and the
calls after it run again, and so does everything after a foreach or parallel in which an item ran again. Unchanged items are reused.
Use now() and random() from the SDK instead of time and random when the value goes into a prompt.
shell(..., cached=False) and tool(..., cached=False) run again on every resume: use it for a check of something outside the run.
The run shows as a live graph in the thread, and you receive its report when it ends.
An agent that needs an approval waits for the user; it does not fail.

Models: haiku, opus, sol (add an effort as in opus@high).
Agents you can run with agent=: researcher, reviewer.

Args:
    source: Inline Python source.
    path: Workspace-relative path of a Python file.
    name: Name of a saved workflow.
    args: JSON object passed to main(args).
    title: Three to six words shown on the run's card.
    max_concurrent: How many agents run at once. Raise it for large fan-outs.
    max_cost_usd: Pause and ask the user when the run has spent this much.
    resource_profile: Worker size for the script itself.
```

### The other five descriptions

```text
get_workflow: Return a run's status, cost, open questions, failed calls and nodes. nodes is "active" (default), "failed" or "all"; messages=True adds peer messages and shared state.
cancel_workflow: Stop a run and its agents. Finished calls stay saved, so resume_workflow can continue it.
resume_workflow: Continue a stopped or failed run, or run a finished one again; saved calls are reused as start_workflow explains. Pass source or path to run an edited script, and rerun=[node ids] to make calls run again, such as one that ended indeterminate.
list_workflow_runs: List this conversation's recent runs and your saved workflows (name, description, args).
save_workflow: Save a run's script as workflows/<name>.py so start_workflow(name=...) can run it again.
```

`start_workflow` returns `{run_id, job_id, status, title}` at once; it accepts the standard `background_after` argument to wait for a short run.
The report reaches the calling agent through the existing background-job delivery (notice, `job(action="collect")`, follow-up turn), in full.
A run that ended with caught failures says so in the first line of the delivery: `3 calls failed (n4, n9, n12); get_workflow(run_id, nodes="failed") shows them.`
A failed run delivers the last 4 KiB of the script's output and traceback, so the agent can fix the script and call `resume_workflow`.
Where a call cannot become a background job (inside a workflow agent, a minimal agent, a voice call), `start_workflow` waits and returns the report itself.
With `background_tool_jobs.enabled: false`, or with `workflow` among that setting's excluded toolkits, a top-level call refuses: `Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow.`

## The SDK: `mindroom.workflow`

Stdlib only, like `mindroom.script_sdk`.
These are the complete signatures and docstrings.

```python
def agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None, tools=None, skills=None,
          knowledge=None, profile=None, minimal=False, timeout=None, output_path=None,
          dedicated_worker=False, resource_profile=None):
    """Run one agent turn and return its answer.

    Returns text, or parsed JSON matching `schema` (a JSON Schema dict).
    The agent is a fresh copy of the workflow's owner with all its tools, unless `agent` names another configured agent.
    `system`, `tools`, `skills`, `knowledge` or `profile` (a `subagents/<profile>.md` file) shape a copy of the owner.
    `model` is any configured model name, such as "haiku" or "opus@high".
    `name` labels the node and is the address other agents message; it does not select an agent.
    `output_path` also writes the answer to that workspace file; `dedicated_worker` and `resource_profile` are those of run_subagent.
    Raises AgentError when the turn fails, is cancelled, is denied, or times out after `timeout` seconds.
    """

def foreach(items, fn, *, max_concurrent=None):
    """Call fn(item) for every item in parallel and return the results in item order.

    When calls fail, the others still finish, then PartialFailure is raised.
    Finished items are remembered by value, so adding or reordering items on resume reuses them.
    Items that are not JSON values are remembered by position.
    """

def parallel(fns, *, max_concurrent=None):
    """Call every zero-argument function in parallel and return the results in order; raises PartialFailure like foreach."""

def spawn(name, **options) -> "Session":
    """Create an agent that keeps its conversation; takes the options of agent() except prompt and schema."""

class Session:
    def ask(self, message, *, schema=None, timeout=None):
        """Send the next message to this agent and return its answer; it remembers the earlier turns."""

def ask_user(question, options=None):
    """Show the question in the thread and return the answer; only the calling branch waits.

    `options` is a list of strings; the answer is one of them. Without options the answer is free text.
    The question waits as long as it takes, across restarts.
    """

def shell(command, *, timeout=3600, tail=2000, cached=True) -> "ShellResult":
    """Run a command with the owner's shell tool; returns ShellResult(ok, exit_code, output) and never raises on a non-zero exit.

    `output` is the last `tail` lines. With cached=False the command runs again on every resume.
    """

def tool(toolkit, function, /, *, cached=True, **arguments):
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
    """Message an agent of this run by name; "*" reaches every agent that has not finished."""

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
    """kind: failed | cancelled | denied | timeout | schema | refused | indeterminate."""

class ToolError(WorkflowError):
    """kind: failed | denied | indeterminate (the call may have run; check its effect, then resume with rerun=[...])."""

class PartialFailure(WorkflowError):
    """Some calls of a foreach() or parallel() failed.

    ok: the results that succeeded, in order.
    results: one entry per position; a failed position holds None, so check `errors`, since None can also be a real result.
    errors: {position: exception}. items: the foreach items.
    """
```

### Errors that say what to do

| Mistake | Message |
|---|---|
| `parallel([agent(a), agent(b)])` | `parallel() takes functions. Write parallel([lambda: agent(a), lambda: agent(b)]) or use foreach(items, fn).` |
| `parallel([lambda: agent(p) for p in prompts])` | `These functions capture the same loop variable, so each would see its last value. Use foreach(prompts, lambda p: agent(p)).` |
| No `main` | `Define def main(args): and return the report. MindRoom calls it.` |
| An SDK call at module level, or the script's own `main()` call | `Call agent() inside main(args). MindRoom calls main; remove your own call.` raised at that call, before any agent starts |
| SDK call from a raw thread | `Call agent() from main or inside foreach()/parallel(). Raw threads are not tracked, so resume could not match their calls.` |
| `agent(..., system_prompt=...)`, `agent_name=`, `task=` | `agent() has no option 'system_prompt'. Did you mean system?` |
| Unknown agent or model | `Unknown model 'opus5'. Configured models: haiku, opus, sol.` (the existing delegation refusal text), as kind `refused` |
| Invalid schema | `schema is not valid JSON Schema: <path>: <reason>.` raised before any agent starts |
| Schema never met | `Agent 'verify-3' did not return JSON matching the schema after 5 attempts. Last error: 'problems' is a required property.` |
| A request over 1 MiB | `Prompt is 1,403,112 bytes, limit 1 MiB. Write the inputs to a workspace file and pass the path.` |
| Message to a finished agent | `'critic' has finished and will not read this. Use spawn() to keep an agent you want to talk to.` |

`if __name__ == "__main__":` in a script is harmless: MindRoom loads the script as the module `__workflow__`, so the guard is false.
When `name` equals a configured agent's name and `agent` is not set, the run logs: `name="researcher" only labels the node. Use agent="researcher" to run that agent.`

## Example workflows

Each is a complete script, and each runs in the test suite with a scripted model.

### 1. Diamond research

```python
from mindroom.workflow import PartialFailure, agent, foreach, phase

def main(args):
    phase("Plan")
    plan = agent(f"Split this question into 4-8 independent research angles: {args['question']}",
                 schema={"type": "array", "items": {"type": "string"}})
    phase("Research")
    try:
        notes, missing = foreach(plan, lambda angle: agent(f"Research: {angle}\nReturn findings with sources.")), []
    except PartialFailure as e:
        notes, missing = e.ok, [plan[i] for i in e.errors]
    phase("Synthesize")
    return agent("Answer the question from these notes. Mark anything only one note supports.\n\n"
                 f"Question: {args['question']}\nAngles with no research (say so): {missing}\n\n" + "\n\n---\n\n".join(notes))
```

### 2. Audit with an adversarial check (maker is not checker)

Nothing is caught here, so a failed audit or verification fails the run instead of looking clean.

```python
from mindroom.workflow import agent, foreach, phase

FINDINGS = {"type": "array", "items": {"type": "object", "required": ["file", "line", "claim"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "claim": {"type": "string"}}}}
VERDICT = {"type": "object", "required": ["real", "reason"],
           "properties": {"real": {"type": "boolean"}, "reason": {"type": "string"}}}

def main(args):
    phase("Audit")
    per_file = foreach(args["files"], lambda f: agent(f"Audit {f} for {args['issue']}.", schema=FINDINGS))
    findings = [x for group in per_file for x in group]
    phase("Verify")
    verdicts = foreach(findings, lambda x: agent(
        f"Try to disprove this finding. Read the code first.\n{x}", schema=VERDICT, model=args.get("checker_model")))
    real = [x | {"reason": v["reason"]} for x, v in zip(findings, verdicts) if v["real"]]
    return {"confirmed": real, "rejected": len(findings) - len(real)}
```

### 3. Implement, review, fix until the tests pass

```python
from mindroom.workflow import ask_user, phase, shell, spawn

def main(args):
    wt = args["worktree"]
    dev = spawn("dev")
    reviewer = spawn("reviewer", model=args.get("review_model"),
                     system="You review diffs. Reply APPROVE or list blocking problems.")
    phase("Implement")
    dev.ask(f"Implement this in {wt} with tests, and commit:\n{args['task']}")
    round, limit = 0, 5
    while True:
        round += 1
        phase(f"Round {round}")
        tests = shell(f"cd {wt} && {args['test_command']}")
        review = reviewer.ask(f"Review `git diff main` in {wt}.")
        if tests.ok and review.strip().startswith("APPROVE"):
            return f"Done in {round} round(s).\n\n{review}"
        if round == limit:
            if ask_user(f"{round} rounds did not converge. Keep going?", ["Stop here", "Five more rounds"]) == "Stop here":
                return f"Stopped after {round} rounds.\n\nTests:\n{tests.output}\n\nReview:\n{review}"
            limit += 5
        dev.ask(f"Fix and commit.\n\nTest output:\n{tests.output}\n\nReview:\n{review}")
```

### 4. Panel and judge

```python
from mindroom.workflow import agent, foreach, phase

SCORE = {"type": "object", "required": ["score", "why"],
         "properties": {"score": {"type": "number"}, "why": {"type": "string"}}}

def main(args):
    phase("Draft")
    plans = foreach(args["models"], lambda m: agent(f"Write a plan for: {args['task']}", model=m, name=f"planner-{m}"))
    phase("Judge")
    scores = foreach(plans, lambda p: agent("Score this plan 1-10. Be harsh.\n\n" + p, schema=SCORE))
    best = max(range(len(plans)), key=lambda i: scores[i]["score"])
    phase("Merge")
    others = "\n\n".join(p for i, p in enumerate(plans) if i != best)
    return agent(f"Improve the winning plan with the best ideas of the others.\n\nWinner:\n{plans[best]}\n\nOthers:\n{others}")
```

### 5. Loop until dry

A finder that fails raises, so "no new bugs" always means every finder answered.

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
        new = {i["id"]: i["text"] for batch in batches for i in batch if i["id"] not in found}
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
    return spawn("judge").ask("Decide, and say why.\n\n" + "\n\n---\n\n".join(turns))
```

The agents talk directly, with no script in the middle (a team):

```python
from mindroom.workflow import agent, parallel, phase, state

def backend(feature):
    return agent(f"Build the API for: {feature}\nBefore you write code, agree on the JSON contract with the agent named "
                 "'frontend': send_peer_message('frontend', ...), then read_peer_messages(wait_seconds=600) for its reply. "
                 "Save the agreed contract with set_shared_state('contract', ...).", name="backend")

def frontend(feature):
    return agent(f"Build the UI for: {feature}\nThe agent named 'backend' will propose a JSON contract: "
                 "read_peer_messages(wait_seconds=600), push back where it does not fit the UI, then build against it.",
                 name="frontend")

def main(args):
    phase("Build")
    api, ui = parallel([lambda: backend(args["feature"]), lambda: frontend(args["feature"])])
    phase("Integrate")
    return agent(f"Report where either side breaks this contract.\n\nContract: {state.get('contract')}"
                 f"\n\nBackend:\n{api}\n\nFrontend:\n{ui}")
```

### Agents that edit the same repository

Separate agents do not get separate checkouts on their own.
The documented idiom is one git worktree per item, made by the script, with the path in the prompt, and one merge step at the end:

```python
def fix(issue):
    path = f"../wt-{issue['id']}"
    shell(f"git worktree add {path} -b fix/{issue['id']}")
    agent(f"Work only in {path}. Fix and commit: {issue['text']}")
    return f"fix/{issue['id']}"

branches = foreach(issues, fix)
merge = agent(f"Merge these branches into the current branch one by one and resolve conflicts: {branches}")
```

`dedicated_worker=True` gives an agent its own container when files outside git must not collide either.

## Communication between agents

Yes, it is possible, in three ways, and all three are in v1.

### 1. Through the script (sessions)

`spawn()` returns a session and `Session.ask()` continues it with `continue_subagent` semantics: same child, same history, same model.
The script decides who hears what, which fits debates, critique loops and maker and checker pairs.
In the graph a session is one node with a turn count; opening it shows its turns.

### 2. Directly (peer messages and shared state)

Every agent of a run gets the run-scoped toolkit `workflow_peers` automatically.

```text
send_peer_message(to, text)        to is an agent's name, "*" for every agent that has not finished, or "script"
read_peer_messages(wait_seconds=0) unread messages as [{from, text, sent_at}]; waits for the first one
list_peers()                       name, status and label of every agent in the run
get_shared_state(key=None)         one value, or all values
set_shared_state(key, value)       last write wins
ask_user(question, options=None)   the same question card as the script's ask_user
```

Delivery semantics:

- A message is a durable row in the run store, ordered by a run-wide sequence number, with one read cursor per recipient node.
- **Addressing.** A mailbox belongs to a name, not to a node.
  A message to a name no agent has yet is held and delivered when an agent of that name starts, so the order in which parallel branches start does not matter; the sender is told `held: true`.
  Several agents may share a name (a `foreach` of reviewers named `reviewer`); each gets its own copy, and `*` reaches every agent that has not finished.
  A message to a name whose agents have all finished returns the error from the table above instead of vanishing.
- **Exactly once.** Each of these operations has a stable identity and its result is saved in the same transaction as its effect (the row, the cursor, the value).
  For an agent the identity is its node, generation, the child's model run ID, the tool call ID and a digest of the operation and its arguments, so a provider retry returns the saved result and a reused call ID with other arguments is a new operation.
  For the script, `send`, `inbox`, `state.get` and `state.set` are nodes with lane keys like every other SDK call, so a replayed `send` does not send again and a replayed `inbox` returns the same messages.
- **Reruns.** When a node runs again as a new generation, its unread outgoing messages of the old generation are withdrawn and its own read cursor starts at zero, so the fresh agent sees what its predecessor saw.
- **Waiting does not hold a slot.** `read_peer_messages(wait_seconds=N)` and `inbox(wait_seconds=N)` wake on the first message; while they wait the node gives up its place in the run's concurrency limit and shows as waiting on `peer`.
  A lane of the script that is blocked in a waiting call also gives its thread to the next lane, so a fan-out at `max_concurrent=1` cannot block its own sender.
  N has no ceiling: no provider request is open while a tool call waits.
- **No silent deadlock.** Every SDK request reports how many lanes of the script can still act (not started yet, or running Python between calls).
  When every unfinished agent waits for a message, nothing is queued or running, and the newest report says zero, every wait returns at once with no messages and the note `Every agent is waiting for a message and nobody is left to send one.`
- A session that is between turns keeps its mailbox; queued messages are put in front of its next `ask` as "Messages received since your last turn".
- A message or a state value is at most 1 MiB, the request limit; larger data goes in a workspace file whose path is sent.
- Messages and shared state survive restarts and resume with the run.

In the graph a message is an edge from sender to recipient with a count.
The texts and shared state are as visible as the agents' own output (see Visibility): each agent's Sub-agents view shows its sends and reads, and `get_workflow(run_id, messages=True)` returns the full history to the run's owner.
Not in v1: pushing a message into a busy agent's next tool result (it reads its inbox when it chooses), and messages between different runs.

### 3. With people and with the room

The script's `ask_user` and the agents' `ask_user` tool post the same question card and wait.
Workflow agents keep `matrix_message`, so they can post in the originating thread or message another room agent as today; a reply to such a message starts an ordinary turn outside the run and is not drawn in the graph.

## What the user sees

1. **Start card.** The agent's reply contains a card: title, status, phases, `12 done, 8 running, 1 waiting`, cost so far and projected, elapsed time, and the buttons Graph and Stop.
2. **Jobs bar.** One row per run with the same numbers; the run's agents are not listed one by one there.
3. **Graph panel.** Phases top to bottom, fan-out groups with a progress count, one chip per agent with status, model, tokens, cost and time, and curved edges for messages.
   Selecting a chip shows its prompt label, result preview and error, with Open (the existing Sub-agents view of that agent's live text and tool calls) and Stop (cancel only that agent).
4. **Questions.** `ask_user` shows a card with option buttons or a text box; the answer is visible to the room afterwards.
   Its plain-text fallback carries a short ID that no other run or rerun shares: `Question q3fa91c: Deploy to production? 1. Yes 2. No. Answer with !answer q3fa91c <number or text>.`
   `!answer <text>` with no ID works only when the sender has exactly one open question in the thread; otherwise the reply lists the open ones.
   Every answer gets feedback: the card changes to "Answered", or a notice says why not (`already answered`, `not for you`, `no longer open`, `not one of the options`).
5. **Approvals.** A workflow agent that needs an approval shows the existing approval card, named after that agent; its chip and the run turn to Waiting for you.
   The run waits for the decision; the operator's card expiry (`tool_approval.timeout_days`, 7 days by default) still applies, and an expired card counts as a denial of that call.
6. **Large run notice.** When a run has scheduled more than 25 agents or its projected cost passes 10 USD, the card shows `Large run` once, with the projection; it never pauses the run.
   With `max_cost_usd` the run pauses at that amount and asks: continue with the same amount again, or stop.
7. **End.** The card turns Done, Failed, Cancelled or Interrupted with totals, a failed run's first error line, and "3 failed" when a finished run caught failures; the calling agent replies with the report.
   The card of a finished run still opens its graph.
8. **Cancel and resume.** Stop on the card, the jobs bar or the panel, `!cancel_job`, or asking the agent all cancel the run.
   A stopped or failed run offers Resume; a finished run offers Run again, which reuses every unchanged call.
   New arguments mean a new run, through the agent.
   After a MindRoom restart, runs continue on their own.
9. **Saved workflows.** A saved workflow is the file `workflows/<name>.py` in the agent's workspace.
   The user runs one by asking ("run review-branch on main"); `!workflows` lists the saved ones and the running ones.

## Internals

### Where the script runs

- **Worker backend configured (Docker, Kubernetes):** each attempt runs in its own dedicated worker, exactly as a background script does today; the shared static runner is refused, as it is for scripts.
  When that backend is down or refuses, the run does not start (or its attempt ends `interrupted` and is retried); it never falls back to the host.
- **No worker backend configured:** the script runs as a child process of the primary under the primary's shell supervisor, on Linux, when `defaults.allow_shell_without_worker` is true or the sandbox mode is explicitly local.
  That is the operator's existing statement that code may run as the primary's user, so there is no new setting.
  Otherwise `start_workflow` refuses: `Workflows run code. Route this agent to a worker, or set defaults.allow_shell_without_worker: true to run it on this machine.`
- **Never inside the primary process.** A script that blocks or crashes must not stop the event loop.
- **Local runs are Linux only in v1,** as scripts are: only Linux kills the script's process group when the supervisor dies; a macOS wrapper is under Later.
- Kubernetes keeps its gateway-only listener requirement; the workflow routes are mounted on both listeners explicitly.
- Every attempt runs with `PYTHONHASHSEED=0`, so a prompt built from a `set` is the same on replay.

### Runs, attempts, nodes and generations

A **run** (`wf-<32 hex>`) is the durable thing: owner (agent, requester, room, thread, execution identity, and the frozen persona when a narrowed agent started it), title, source and its digest, `args`, limits, state, result, the originating job, and the parent run and node for a nested run.
An **attempt** is one script run of the existing script runtime (`script-<32 hex>`, a `ScriptRunRecord` with `workflow_run_id` set); a run has one live attempt at a time.
A **node** is one SDK call: `agent`, `turn` (a session's `ask`), `session`, `ask`, `tool`, `value` (`now`, `random`), `send`, `inbox`, `state`, `group` (`foreach`, `parallel`, `run`).
A node has a **generation**: 1 at first, and one higher each time the node is run again on purpose (a changed call, a failed call on resume, `rerun`, `cached=False`).
A redelivered request, a restart and a reattach never change the generation.

Run states: `starting`, `running`, `waiting` (no node can run until a person acts), `completed`, `failed`, `cancelled`, `interrupted`.
Node states: `queued`, `running`, `waiting` (on `approval`, `user`, `budget` or `peer`), `completed`, `failed`, `cancelled`, `indeterminate`.

The store is SQLite at `<control_root>/workflow_runs/workflow_runs.sqlite3` with a schema version and explicit migrations from the first release.
Tables: `workflow_runs`, `workflow_nodes`, `workflow_usage`, `workflow_messages`, `workflow_cursors`, `workflow_peer_calls`, `workflow_state`, `workflow_sources` and `workflow_log`.
A node row holds its full request, not only a digest, so a restart can restore it and a person can inspect it.
Node results are stored in full; a result over 8 MiB is written to a file beside the database and the node holds a reference and the first 64 KiB.
The control store is the only authoritative copy, and its full records are read only by the run's owner.
Nothing is exported to the workspace on its own, because a workspace can be shared by several requesters; `get_workflow(run_id, nodes="all")` with the standard `mindroom_output_path` writes a run's nodes to a file when the agent asks.
Finished runs, with their messages, usage and files, are pruned after the script retention window (30 days, `MINDROOM_SCRIPT_RETENTION_SECONDS`) by the scheduled prune that already removes script runs.

**Ownership.** Every lookup by run ID goes through one function that checks the calling agent and requester against the run's owner, as scripts do; a foreign ID answers exactly like an unknown one.
That covers the six tool functions, the peer tools, shared state and `save_workflow`.

**Visibility.** One rule: peer message text and shared state have the same visibility as the agents' own output.
The Sub-agents feed already shows room members every agent's text and tool calls, redacted and capped by `delegation/activity.py`, and that includes `send_peer_message`, `read_peer_messages` and the shared-state calls of a workflow agent; a `get_workflow` result can show in the owner agent's reply previews in the same way.
Nothing here is private from the room, and no extra hiding is built.
The owner check guards the full, uncapped record (results, message history, state), and unencrypted room state still carries IDs only.

**The run's job.** The run is one background job of kind `workflow` in the existing `ToolJobRuntime`.
That gives it a jobs-bar entry, every existing cancel path, the `job` tool, and delivery of the report to the calling agent.
The order is: the tool call passes the operator's approval rules like any call (the toolkit's job hook is asked only after that gate); the run is written as an **intent** (state `starting`, attempt 0, its preassigned ID, source, args and options, no process); the job runtime admits a job whose saved fields name that run; and only the admitted operation launches the first attempt.
A refused admission deletes the intent and starts no process.
Every crash point has one owner: an intent whose job the runtime does not know after its own recovery is deleted, a `starting` run with a live job gets its first attempt from the job's continuation, and an attempt process the run does not name is retired like any inherited attempt.

**Run identity.** A run is found again only by a key that hashes the canonical owner (agent, requester, room, thread), the originating model run and the tool call ID, as job IDs are made today; a provider's call ID alone is never a key.
A hit must also match the owner, the source digest and the args, or the call is refused; it never returns another run.
The job kind has its own authority check (the agent still has `workflow`, the requester still has access), its own recovery (below), a handle with `run_id` and `title`, and delivers the whole report, not the 500-character summary of generic jobs.
Once launched, the job is never `awaiting_approval`: it stays `running` while the run waits, and it is started without a reply ID, so Stop on the starting reply does not block its continuation.
Cancelling a run revokes the attempt's capability, cancels every live node job, closes open question cards, cancels nested runs, and ends the run `cancelled`; completed nodes stay saved for a later resume.

### How the script talks to MindRoom

MindRoom launches the existing script shim, which validates the snapshot's digest and the capability token and removes the token afterwards, as today.
The trusted launch description gains a versioned `entry` field (`script` or `workflow`) that crosses the worker wire; for `workflow` the validated shim hands the source to the SDK instead of running it as `__main__`, and a worker that does not know the field refuses the launch.
The SDK loads the source as the module `__workflow__` (so tracebacks keep the user's line numbers and an `if __name__ == "__main__":` block does nothing), then calls `main(args)` and posts the return value or the traceback.
The root lane exists only while `main` runs, so an SDK call at module level fails at that call.
The SDK calls routes under `/api/script-gateway/workflow/`, authenticated by the attempt's existing capability token.

```text
GET  run                            -> {run_id, attempt, args}
GET  sources/{name}                 -> the saved workflow's bytes for run(), snapshotted once per attempt
POST nodes {key, kind, args, meta}  -> {id, state, cached, result, error}
GET  nodes/{id}?wait=25             -> the same receipt, held up to 25 s until it changes
GET  results/{id}                   -> the node's full result as JSON, streamed
POST log {text}    POST finish {result | error}
```

- A blocked `agent()` is a thread that polls `GET nodes/{id}?wait=25` with a 40 s client timeout, for hours if needed; a gateway restart looks like a retryable transport error and the SDK keeps polling.
- Only the run's current attempt is served; an older attempt gets `409 superseded` and exits, so a zombie process cannot act.
- **Sizes, per direction.** A request from the script is at most 1 MiB including its envelope, refused with the message in the error table.
  A result over 8 MiB is not inlined in the receipt: the SDK reads it from `results/{id}`, so the script always gets the complete typed value; the 64 KiB preview is only for frames and `get_workflow`.
  `finish` accepts up to 8 MiB; the report is stored on the run and delivered from there.
- The 64 KiB receipt cap of plain script calls does not apply: `tool` node results are saved in the node store like agent results.
- A node's `result` is the child's raw final answer, read from the owner-checked child run (not from the audit record, which redacts and shortens) and saved on the node before the job is acknowledged; the delegation receipt, IDs and usage are separate fields.

### Replay and resume

Every node has a **key** and a **digest**.

- The key is `lane@index`: the root lane is `""`, the n-th SDK call in a lane has index n, and `foreach` or `parallel` at index i opens child lanes `lane/i:<item>`.
  For `foreach`, `<item>` is a hash of the item's JSON value with an occurrence counter for duplicates, so adding, removing or reordering items keeps the other lanes' keys; an item that is not JSON gets its position instead, marked as positional.
  For `parallel` it is the position.
- The digest is the SHA-256 of the call's kind and arguments (prompt, schema, target, model, system, tools, profile, mode, and `name`, because it is a mailbox address), computed again by the gateway.
  A `turn` digest also covers its session and, added by the gateway, the digest and generation of each earlier turn of that session, so a turn that changed or ran again changes every later turn of the session, in whichever lane it is asked.
  A `run()` group's digest covers the hash of the bytes that are executed.
- A group node completes when it is submitted and stores its ordered member list; it is never `running`, so a restart cannot leave one half open.
  When the submitted member list differs from the saved one (an item added, removed or moved), the parent lane is dirty after the group; the lanes of the items that stayed are not.

**Dispatch handles.** Before a node's work is dispatched, the node row is written with a handle to exactly that dispatch: the broker's script run and call ID for a `tool` node, the job ID for an agent turn.
Settling a node means copying the result the handle points at.
So the two places a crash can fall between (the work finished, the node not yet updated) are repaired by reading the handle, never by doing the work again.
A handle with nothing behind it means "never started" only when the attempt's call records still exist and hold no claim for it; when they were retired or pruned, the call is `indeterminate`.
The broker's receipt records the moment the effect starts (after hooks and approval, before the tool runs), so a call that was still waiting for approval is known not to have run.

On a node request the gateway decides in this order.

1. **The same attempt already submitted this key:** it returns that node's receipt, whatever its state.
   A lost response therefore never repeats work, not even failed work.
2. **A saved node left `queued` or `running` by a dead attempt is reconciled first,** before its digest or its lane is looked at.
   A completed broker receipt or ended job is copied; a refusal, or an approval wait before the effect started, is a failure; a call whose effect started with no recorded end becomes `indeterminate`.
3. **A saved node that is `indeterminate` raises** `ToolError` or `AgentError` of that kind and nothing is dispatched, on every attempt, whatever the new digest and also in a dirty lane, until someone names the node in `resume_workflow(rerun=[...])`.
4. **A saved node with the same digest, in a lane that is not dirty here:**
   - `completed`: the saved result returns at once, marked `cached`, so nothing runs and nothing is paid.
   - still live (an open question, a job that survived): the call attaches to it through its handle.
   - `failed` or `cancelled`: the call runs again as the next generation.
5. **Otherwise** (a different digest, no saved node, a dirty lane, a node named in `rerun`): the call runs as the next generation, and any live work of the node it replaces is cancelled and its open card closed.

**Dirty lanes.** When a node at index i runs in this attempt instead of hitting, its lane is dirty from index i + 1: later calls in that lane run again, because the earlier call's effects may have changed what they would see.
The same reaches upward through joins: when any call inside a `foreach` or `parallel` ran, the calls after that group in the parent lane run again too.
Sibling items are untouched, so one failed agent in a fan-out of 500 reruns one agent and then whatever follows the fan-out, not the other 499.
A `cached=False` call always runs; when its result equals the saved one the lane does not turn dirty.

**Nodes the script no longer reaches.** When an attempt completes, nodes it did not reach (a removed item, a branch not taken) are marked stale: their live work is cancelled, they leave the graph's current view, and their usage stays in the totals.

**What replay does not cover.** Python between calls, imports, and reads of files or the network run again on every attempt, and a write made with plain Python is repeated.
The tool description says to put effects in `shell()`, `tool()` or an agent, and the docs say it again.

**Resume** starts a new attempt of the same run and replays the script from the top.
It happens in three ways: `resume_workflow` (optionally with an edited script or `rerun`), the Resume and Run again buttons, and automatically after an infrastructure interruption (runtime restart or shutdown, plugin reload, worker or supervisor loss, or an attempt that reached its own process limit while the run has working time left).
Both the tool and the button register a new job for the run when the old one has ended.
Automatic resume stops after 5 attempts in a row that finish no new node, because a script that takes the runtime down on start would otherwise loop forever.

**Restart.** There is one path, replay.

- Workflow attempts are never adopted by a new primary (they carry no recovery signature); at startup every surviving attempt process is revoked and killed before any route is served, and a late request from one gets 409.
- Then, once the job runtime owns its jobs, each unfinished run is looked at: one whose `finish` was stored settles its job from the stored report; any other gets a new attempt when its requester's access is confirmed.
  Access that cannot be checked yet (Matrix not ready) is tried again; access that was revoked ends the run `interrupted` with that reason.
- Agent jobs that were running survive as they do today: the job runtime continues the same child with a follow-up turn, and the continued job keeps its `workflow`, `node` and `generation` fields.
  The replayed script reaches that node, finds it live, and attaches.
  No finished or running work is thrown away, and no child runs twice.
- An agent turn that could not be continued (the continuation limit, a lost session) is `failed` and reruns on replay when the child had made no tool call, and `indeterminate` when it had.
- A `tool()` call whose effect started and has no recorded end is `indeterminate`, as script calls are today.
- Removing `workflow` from an agent revokes its attempts and ends its runs `interrupted`; the existing reload checks that look for the `script` tool accept either tool.

**Determinism.** Nothing throws: Python's logging and HTTP stack need the real clock.
A call whose prompt contains the time or a random value simply misses the cache and runs again.
`now()` and `random()` are `value` nodes, so their results replay.
When a completed node is replaced although the script's digest and every earlier call in its lane are unchanged, the run logs: `Call n17 changed between attempts without a script edit. It probably contains the time or a random value; use now() or random() from mindroom.workflow.`

**Sessions.** A `turn` whose digest matches the saved turn hits.
A new turn after the last saved one continues the saved child session.
A turn that changes in the middle starts a fresh child whose first prompt carries the saved prompts and answers of the earlier turns, because the script has already consumed those answers.

### How `agent()` maps to delegation

- **Target.** Resolved and authorized by the existing delegation code with a third grant, `workflow`.
  A copy of the caller is always allowed, plain or shaped with `system`, `tools`, `skills`, `knowledge` or `profile`.
  Another agent is allowed when it is in the owner's `delegate_to`, or when it is joined to the run's room and the requester may address it there.
  The rule lives in `authorize_delegation` (which sees the requester and the room) and is checked at admission, at dispatch, at every continuation and when access is revoked.
  Such an agent runs as configured, with its own tools, knowledge and memory, and `model` may still be overridden.
  The docs say that enabling `workflow` lets an agent run the room's agents.
- **One launch path.** SDK `agent()`, a session turn, a script's `tool("delegate", "run_subagent", ...)` and a nested `start_workflow` all go through the engine's child launch, so each gets job admission, native approval handling and recovery.
  A script `tool("delegate", ...)` or `tool("workflow", "start_workflow", ...)` call first passes the broker's ordinary steps for that function (live grant, hooks, the operator's approval rule) and is diverted only where the broker would execute it, so a denial launches nothing.
  The node stores the resolved target (agent, persona, tool narrowing, model), the caller's real depth and the worker scope, and recovery uses those, never the owner's current configuration.
- **Child turn.** `prepare_child_turn` with the caller's depth (it adds one itself), run as a delegation job in `ToolJobRuntime`.
  Each turn the engine starts has its own job, with the ID `sha256(owner, run, key, generation, turn)`; a schema correction is a further turn of the same generation, and a restart continuation keeps its job's ID, as the runtime continues jobs today.
  The engine starts a job once and afterwards only waits on its saved ID; it never asks the runtime to reattach by restarting.
  A result is accepted only from the node's current generation, so a late completion of a replaced node changes nothing.
- **Jobs stay inside the run.** A node job is admitted at the caller's real depth, exactly as a delegation job is, because the restart continuation requires the child one level below its job.
  One filter keeps node jobs out of follow-up turns, held-jobs notices, the `job` tool's list and room state: a job that carries `workflow` and is not of kind `workflow` is skipped, also when it ended while MindRoom was down.
  The engine holds their claims and acknowledges each ended job after saving the node.
- **Label and prompt.** The child's `task` is the node label (80 characters); the full prompt goes in `prompt=`, so a 1 MiB prompt is not copied into every job snapshot.
  Whenever a continuation needs the original prompt (a restart before the first tool call, a child stopped during its calls), it is read from the node's saved request, never from the label, through a lookup the workflow runtime registers with the delegation code.
- **Mode.** Standard by default, so the child has its full tool schemas and can pause for approval; `minimal=True` follows `run_subagent`'s rules.
- **Tools.** A plain copy inherits every tool of the caller, including `delegate`, `memory` and `workflow`, plus `workflow_peers`; `tools=[...]` narrows it.
- **Knowledge and history.** The child runs under the owner's room, thread and requester identity, so its knowledge bases, memory and Matrix tools reach what the owner's reach; the thread is not preloaded, because a fresh context is the point.
- **Model.** Any configured model per call; the requester's budget may still swap in its fallback, as for every child.
- **Result.** The node stores the child's raw final answer; a child that a config reload stopped ends with a notice instead of an answer today, and the engine treats that as interrupted, not as a result.
- **Timeout.** A node timeout cancels its job and waits for it to end before the node fails.
- **Nesting.** A child that calls `start_workflow` is not a job caller, so the call runs inline: it reserves a child run that names its parent run and node, waits, and returns the report.
  The child run's key is its parent run, node and generation plus the source digest and the args, not a call ID, because an agent continued after a restart repeats its call under a new ID: the same call attaches to the live child run or returns its stored report.
  The child run shows as a group under that agent, Stop on the parent or on the agent cancels it, its cost is counted once (below), and it has its own `max_concurrent`, so a parent at limit one cannot deadlock on its own child.
  Depth counts real delegation ancestry (`MAX_DELEGATION_DEPTH`, 3); `run(name)` is an inline call and adds none.

### Concurrency

`foreach` and `parallel` run their functions on threads in the script (one per item, at most 256 active per call; more items are fed as threads free up, and a lane blocked in a call that waits for a person or a message does not count).
The limit that matters is in the gateway: one limit per run, `max_concurrent` (default 16, any value the caller asks for unless the operator set a ceiling).
It counts running `agent`, `turn` and `tool` nodes across all lanes of that run; a node that waits for a person or for a peer message does not count.
The script broker's per-run lock becomes a per-run semaphore of size 1 for plain scripts (no change) and of the run's limit for workflows, so `tool()` and `shell()` calls in parallel lanes run in parallel.
`max_concurrent_runs` (default 8 per requester and agent) counts top-level runs only.

### Pauses for people

- **`ask_user`** creates an `ask` node in state `waiting` and posts an agent message with `io.mindroom.workflow_question` (`{run, node, question_id, requester, text, options, free_text, status, answer}`, status `open`, `answered` or `cancelled`) and the plain-text fallback.
  The message is sent with a transaction ID made from the run, node and generation, so a restart between sending and saving does not post it twice.
  The answer is the event `io.mindroom.workflow_answer` (`{run, node, option | text}`) or `!answer <question_id> ...`.
  The ID is `q` plus six hex characters of a hash of the run, node and generation, so two runs in one thread, or a question and its rerun, never share one.
  An answer counts only when it comes from the run's requester, in the run's room and the card's thread, and refers to the card of the node's current generation; every other answer gets the rejection notice (`io.mindroom.workflow_answer_rejected`, `{run, node, reason}`).
  The card is edited to show the answer and the node completes with it, so a resumed run does not ask twice.
- **An agent's `ask_user` tool** creates its own `ask` node under the agent's node, keyed by the question, and the agent's job waits in the runtime's existing human-decision wait.
  If a restart continues the agent and it asks the same question again, it gets the saved answer.
- **Approvals of an agent's call.** The node's job goes to `awaiting_approval` and shows the existing card; the node is `waiting` on `approval`.
  Across a restart there are three outcomes, tried in this order (PR B4b; before it ships, such an agent runs again when it had called no tool and is `indeterminate` otherwise).
  Restore: the paused run is saved before the stopped job's cleanup rewrites it, loaded from the child's session, and resumed with the exact decisions already given and the cards' original expiry.
  Follow-up turn: the gated call never ran, so the child is continued as a running job is and told which calls finished; it makes the gated call again and a new card appears.
  `indeterminate`: only when it cannot be excluded that a call of the child started and was not recorded.
  A child that waits in its own `ask_user` is marked so on its job, because both waits share one job status.
- **Approvals of a script `tool()` call.** The broker's card path tells the engine, so the node is `waiting` on `approval` and gives up its slot.
  The broker has claimed such a call but its effect has not started, which the receipt records, so after a restart the replay runs it as the next generation and asks again; a denial or an expired card raises `ToolError(kind="denied")`.
- **Operator policy is unchanged.** Workflow attempts use the operator's live `tool_approval` default and rules for script `tool()` calls, exactly what the owner's own turn would get; they do not use the pre-approval list that plain scripts build.
  The functions a script may call are resolved live from the owner's tools, at launch and at each call.
- **Budget.** With `max_cost_usd`, reaching the amount starts no new node, lets running ones finish, and asks through a system `ask` node.
- The run is `waiting` when nothing runs and nothing queued may start until a person acts; the script simply stays blocked in its poll.
- **Lifetime.** A run may work for `max_runtime_hours` (default 72, configurable, 0 for no limit); time spent `waiting` for a person does not count.
  The engine alone enforces it: the script runtime's own wall-clock limit for a workflow attempt is a fixed 30 days, and an attempt that reaches it while the run has working time left is resumed automatically, so a run that waits for a person for months continues when the answer comes.
  An automatic resume keeps the time already used; at the limit the run ends `interrupted` with its work saved, and a person's Resume continues it with a fresh allowance.

Considered: MindRoom already has questions answered by reactions (`mindroom/interactive.py`, Chat's `QuestionCard`), which give buttons in every Matrix client.
They are not reused, because any room member can answer one and they have no free text; a run's question is answered by its requester only, the rule approvals follow.
The numbered fallback and `!answer` cover other clients.

### Structured output

With `schema`, the gateway first checks the schema itself (`jsonschema`'s `check_schema`) and fails the call before any agent starts when it is invalid.
The child's prompt gets the schema and the instruction to end with one JSON value in a fenced block.
The gateway extracts it from the raw answer and validates it; on failure it continues the same child (cheap, the prompt is cached) with the validation error, up to 5 turns in total.
Then the call raises `AgentError(kind="schema")` with the last validation error.
No injected tool is needed, so it works in standard and minimal mode and with every provider.

### Cost

Usage is an append-only ledger: one entry per real model turn, keyed by run, node, generation and turn, so recovery cannot count a turn twice and a rerun cannot erase what an earlier generation cost.
Each entry holds that turn's own `input_tokens`, `output_tokens`, `tool_calls` and `cost_usd`; a nested run's and a nested subagent's turns are entries of their own, so nothing is counted through a parent's rolled-up total.
The run shows two sums: the whole run, and the current attempt.
A cache hit adds nothing.
A turn on a model with no price is counted as unknown, and the totals then read "at least".
The projection is `spent + average cost of all finished turns x (running + queued agent nodes)`; failed turns and schema corrections are in the average, and it only knows calls the script has already made.
The large-run notice (more than 25 agent nodes, or a projection over `large_run_notice_usd`, default 10) is advisory.
There is no hard cap by default; `max_cost_usd` pauses on the whole-run sum and asks.
Existing budgets apply unchanged: each child turn passes through the requester's budget monitor.

### Saved workflows

- A saved workflow is `workflows/<name>.py` in the owner's workspace; `<name>` matches `[a-z0-9][a-z0-9_-]{0,63}`.
  It is the agent's code, like a skill: whoever can make that agent write files can change it, and a shared workspace shares it.
  A run's stored results are not in the workspace; the full record is read through the owner check.
- Its description is the first line of the module docstring, and an optional literal `ARGS = {"base": "Branch to compare against"}` documents its arguments.
  `list_workflow_runs` reads both with `ast`, never by running the file.
- `save_workflow(run_id, name, description)` writes that run's source there with the confined, descriptor-relative, no-clobber write the delegation exports use; it adds the docstring when the file has none, and refuses to replace a different file.
- `start_workflow(name=...)` and `path=` read through the same confined read as script sources, and snapshot the bytes at start.
- `run(name, args)` asks the gateway for the file, which snapshots its bytes once per attempt; the SDK executes exactly those bytes, and their hash is in the group's digest.
- Because it is a workspace file, the agent can also write and edit workflows with its file tools.

### `tool()` and `shell()`

- A script `tool("delegate", ...)` call becomes an agent node, and `tool("workflow", "start_workflow", ...)` a nested run, through the launch path above and after the broker's gate; every other call goes through the script broker with hooks, approval rules, worker routing and audit.
- `shell()` calls the owner's `run_shell_command` through that path with `timeout=3600` and `tail=2000` unless told otherwise, and the docstring says so.
- The exit status is the command's real status: the shell tool reports it as structured metadata of the call, which the broker saves on the receipt.
  Nothing is appended to the command and no marker is parsed from its output, so a trailing comment or an `exit` cannot fake a result.

### Graph data model

A script's data dependencies are invisible, so the graph does not pretend to know them.
Its structure is what the engine does know: phase, lane (which `foreach` or `parallel` group a call belongs to), start order, sessions, and messages.

- **Room state** (`io.mindroom.background_jobs`, unencrypted, IDs only): the run's job is `{job_id, thread_id, kind: "workflow", tool: "workflow", status, started_at, workflow: "<run id>"}`.
  Node jobs are not listed in room state at all: the list holds the newest jobs that fit in 27 KB, so a hundred node jobs would push the run's own job out, and every node start would rewrite the state event.
  Stop for one agent uses the job ID from the frame, and cancel reads the job runtime, not the state.
- **Live frames** (`io.mindroom.workflow`, ephemeral, encrypted in encrypted rooms, same transport as `io.mindroom.subagents`): a full snapshot every 10 seconds while the run is active and deltas in between.

```json
{"v": 1, "run": "wf-...", "sid": "<stream id>", "seq": 12,
 "meta": {"agent": "dev", "thread_id": "$...", "title": "Audit auth module", "status": "running", "attempt": 1,
          "started_at": 1795000000, "ended_at": null, "max_concurrent": 16, "max_cost_usd": null, "large": false,
          "counts": {"queued": 4, "running": 12, "waiting": 1, "completed": 30, "failed": 1, "indeterminate": 0, "cached": 0},
          "usage": {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0, "cost_usd": 3.41, "attempt_cost_usd": 1.2,
                    "projected_cost_usd": 9.8, "cost_unknown": false}},
 "snapshot": {
   "phases": ["Audit", "Verify"],
   "groups": [{"id": "n3", "kind": "foreach", "phase": 0, "parent": null, "total": 40, "done": 28, "failed": 1}],
   "nodes": [{"id": "n17", "kind": "agent", "label": "Audit auth/session.py", "name": "agent-17", "phase": 0,
              "group": "n3", "status": "running", "waiting_on": null, "agent": "dev", "model": "opus",
              "run": "<delegation id>", "subagent": "<subagent id>", "job": "<job id>", "session": null, "turns": 1,
              "started_at": 1795000100, "ended_at": null, "cached": false, "error": null,
              "usage": {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0, "cost_usd": 0.12}}],
   "edges": [{"from": "n17", "to": "n21", "kind": "message", "count": 2}],
   "omitted": {"queued": 0, "running": 0, "waiting": 0, "completed": 0, "failed": 0, "edges": 0},
   "log": ["Round 1: 12 new, 0 known"]}}
```

- A node's `label` is its `name`, or the first line of its prompt, redacted and cut to 80 characters like a subagent title.
- **Fitting.** A frame must pass the transport's own size check, which measures the encrypted, ASCII-escaped event, not the plain JSON.
  Rows are added in order of what a person can act on: nodes waiting for a person, failed and indeterminate, running, queued, then completed, newest first; an error is cut to 200 characters in a frame.
  Whatever does not fit is counted per status in `omitted`, groups always carry their true totals, and when groups alone do not fit, inner groups fold into their parent's counts.
  Every other field is bounded in the frame, not in the run: the title and phase names are cut to 80 characters and the log to its last 20 lines of 200 characters, and when even that does not fit, phases, groups and the log are replaced by their counts.
  That last form (`meta` and `omitted` only) has a fixed size, so a frame can always be sent.
  The counts in `meta` are always the true ones, so the client can say "and 412 more queued" honestly; nothing here limits the run.
- Rows a frame left out are not paged to clients in v1 (see Later); the owner's agent reads them with `get_workflow`.
- A node's live text and tool calls are not in this frame: `run` and `subagent` join it to the existing `io.mindroom.subagents` feed.
- `sid` and `seq` order frames as they do for subagent frames: a restarted MindRoom starts a new `sid`, and a delta applies only to the frame right before it.
  A delta carries changed and new rows; when rows disappear or the phases change, the next frame is a snapshot instead.
- **The card message** carries `io.mindroom.workflow_run` (`{run, job_id, requester, title, status, counts, usage, phases, attempt, started_at, ended_at, error}`).
  It is edited when the status, the phase or the waiting state changes, and otherwise at most every 10 minutes; live numbers come from frames.
  The final edit includes the last snapshot, fitted so that the whole edit event (an edit carries the content twice, plus encryption) passes the event size limit, so a finished run's graph opens from history.
- **Control.** Stop reuses `io.mindroom.background_job_cancel` with the run's or a node's job ID and keeps its meaning: whoever may cancel jobs in the room may stop it.
  Resume is `io.mindroom.workflow_control` (`{run, action: "resume"}`), accepted from the run's requester in the run's room and thread.

### MindRoom Chat view

- **Sync filter.** Chat's `/sync` filter lists the ephemeral event types it wants (`matrix/client/clientSyncPolicy.ts`), so `io.mindroom.workflow` is added there; without it no frame ever arrives.
  The test reads the real filter definition, and an end-to-end test receives a snapshot and a delta through `/sync` as a fresh client, as a returning client, and in an encrypted room.
- No graph library: the layout is phase bands, group boxes and node chips in start order, which is the true structure, with an SVG overlay for message edges.
  The panel is lazy-loaded; the initial bundle has about 1.8 KB of headroom.
- Frame parser `matrix/protocol/workflowFrame.ts` and run store `messages/live/workflowRunStore.ts` sit beside their subagent counterparts.
- `messages/workflow-run/WorkflowRunCard.tsx` and `WorkflowQuestionCard.tsx` render the two message contents; the question card reuses the `QuestionCard` primitives.
  A sent answer disables the card until the edit or the rejection notice arrives, and after 15 seconds with neither it enables again and says so.
- `subagents/workflow/WorkflowPanel.tsx` is the graph panel (layer 8, so the jobs bar and the room can import it); `navigation/workflowPanel.ts` holds its open state, as `navigation/subagentsPanel.ts` does.
- Reused: the jobs-bar rich row cells (calls, tokens, cost, elapsed), `SubagentRunCard` styling for chips, `openSubagentsPanel` for Open, `cancelBackgroundJob` for Stop.
- The jobs bar shows one row per `kind: "workflow"` job; node jobs never reach it.
- "Workflow" already names the thread tabs in Chat code (`WorkflowTabs`); the new code uses `workflowRun` in identifiers and "Workflow" only in labels.
- All strings go in the 17 locale overlays; the PR carries placeholder-data screenshots of the card, the panel, the question card and the jobs-bar row.

## Restrictions

### Kept, each with its reason

| Restriction | Reason |
|---|---|
| Operator `tool_approval` default and rules apply to `start_workflow`, to script `tool()` calls and inside every agent | The operator's policy, unchanged in both directions. |
| Requester authorization is rechecked on every node | A run must stop acting for someone who lost access (existing check). |
| Only the requester answers questions, approves and resumes; only the owner reads a run's full stored record | The existing rule for approval cards and scripts; the room sees the graph and what the Sub-agents feed shows of every agent, as today. |
| A target agent must be the caller, in `delegate_to`, or joined to the room and addressable by the requester | A run must not reach an agent the requester could not reach from this room. |
| An indeterminate call is never issued again without `rerun` | A write must not happen twice. |
| Local runs need `allow_shell_without_worker` or local sandbox mode, and Linux | The script runs as the primary's OS user; that is the operator's existing switch for it, and only Linux kills an orphaned script. |
| One agent node cannot run the same nested workflow with the same args twice | A continued agent repeats its call under a new ID, and the run must not start twice; vary the args to run it again. |
| Budgets | Existing per-requester budget monitor, applied per child turn. |
| Depth 3 (`MAX_DELEGATION_DEPTH`) of real delegation ancestry | A workflow that starts itself would recurse forever. |
| `max_concurrent` default 16 | Provider rate limits; it is a default the caller raises. |
| `max_concurrent_runs` 8 top-level runs per requester and agent (toolkit option) | Each run holds a process or a worker. |
| `max_runtime_hours` 72 of working time (toolkit option, 0 for none) | An abandoned run must release its worker; waiting for a person does not count. |
| `max_tool_calls_per_minute` 600 for script `tool()` calls (toolkit option) | A `while True: tool(...)` loop must not starve the primary; it slows calls, it does not fail them. |
| Script never runs in the primary process | A blocking or crashing script must not stop the event loop. |
| Kubernetes needs the gateway-only listener | The existing worker boundary. |
| Script `tool()` cannot call `compact_context`, `dynamic_tools`, `slack`, or functions that control a model step | They act on the calling turn (its context, its loaded tools, its hook source), and a script has none. |
| Request 1 MiB, source 128 KiB | Primary memory and the launch snapshot; results spill to a file instead of failing. |
| SDK calls only inside `main`, `foreach` or `parallel` | Replay needs a stable lane for every call. |
| Minimal-mode agents cannot pause for approval | Existing: they have one shell tool, and approval-gated functions are not offered to them. |

### Dropped, compared with today's scripts and Dynamic Workflows

- No step, participant or agent caps (64, 8 and 16 today).
- No one-hour limit; runs last days and wait for people.
- No pre-approval list and no always-ask list for script calls (`claude_agent`, `config_manager` and `scheduler` today): the operator's rules decide, as in a turn.
- `delegate`, `memory`, `workflow`, `script`, `browser`, `claude_agent`, `agent_manager`, `contacts`, `self_config` and `skill_manage` are no longer barred, for agents or for script `tool()` calls.
  If one of them breaks without a turn, it returns to the kept list with a one-line reason in the code.
- No data-grant refusals: agents have the owner's knowledge, memory and Matrix access.
- Room agents keep their tools and may get a `model` override.
- No serialized tool calls, no 30 calls per minute, no 64 KiB cap on tool, shell or agent results, no 120-second shell limit.
- No ceiling on a peer wait and no 64 KiB message cap.

## The old toolkit

`dynamic_workflow` is not changed, and its YAML specs do not run on the new engine.
The two toolkits share no function name, so an agent can have both while it migrates.
The docs mark `dynamic_workflow` as deprecated once `workflow` has parity (saved workflows, PR B8).
It is removed after two weeks of dogfooding, on three conditions: old run records and revisions stay readable, the stored usage kind (`usage/storage.py`) and the session marker (`history/session_runs.py`) keep parsing, and report publishing, which accepts a `dynamic_workflow_run` as a report source today, accepts a workflow run as a source and still opens old ones.
The removal gives back about 3,000 source lines.

## Upgrades and packaging

- `script_runs` gains the nullable column `workflow_run_id` through the store's explicit column migration, with its `LEGACY_COMPAT` entry and test; `CREATE TABLE IF NOT EXISTS` alone would not add it.
- Delegation snapshots written before this change lack the new fields; they read as `None`.
- The workflow store migrates by schema version; a test opens a database of each earlier version.
- The launch description's `entry` field raises the worker protocol version; a test launches a workflow on a real Docker worker and checks the snapshot digest, the import path and the token cleanup.
- A test imports `mindroom.workflow` in the worker image's environment, and another calls the new routes through the gateway-only listener.
- New files a toolkit needs: `tools/__init__.py` registration, `tools_metadata.json`, the per-tool extra in `pyproject.toml`, toolkit construction in `agents.py`, the usage kind in `usage/storage.py`, `vulture_whitelist.py`, and the generated docs skill references.

## v1 and later

In v1: everything above.
Cut from v1 in review round 2: paging of rows a frame left out and a list of an edge's messages in the panel (each agent's Sub-agents view shows its sends and reads); a workflow run as a report-publishing source and `mindroom.workflow` as a plugin import; local runs on macOS (a `kqueue` parent watch); the prompt-cache stagger for agents that share a prompt prefix.
Later, in rough order of value: pushing peer messages into a busy agent's next tool result; early exit from a fan-out (the first good answer wins and the rest are cancelled); `agent(..., thread=True)` to show one agent's turn as a Matrix thread; stopping the script process while a run waits for hours and relaunching it on the answer (replay makes that possible); bundled and shared workflows; a `!workflow run` command with no model turn; stall detection with automatic agent restart.

## Testing

Four levels, and each test names its level.

- **Pure:** replay rules, graph fitting, schema helpers, the SDK against a fake gateway.
- **Engine harness** (`WorkflowStack` in `tests/workflow_helpers.py`): the real run store, the real `ToolJobRuntime` on its real store, real delegation with the scripted model of `tests/scripted_agent_helpers.py`, a fake Matrix client, and a fake script runtime in which the test plays the script by calling the engine's gateway methods.
  Everything about jobs, generations, claims, approvals and restarts runs here, because a fake child runner cannot show those faults.
- **Local integration:** the real script runtime in local mode with a real child process, for the shim entry, the SDK, placement and restart.
- **Live** (a throwaway deployment from `~/repos/dev/scripts/live.py`, `just live up`, torn down afterwards), with fault scripts kept in `tests/workflow_live/`.

The first acceptance test, before any agent feature: a script's tool call appends one line to a file; the primary is stopped after the write and before the node is settled; the run resumes; the file has one line and one report is delivered.
It runs in both variants, each stopped at a barrier the test controls and not on file visibility: before the receipt commits (the call is `indeterminate` and nothing is reported until `rerun`), and after the commit but before the node settles (the result is copied, marked cached, one report), locally and on a worker.
Other tests assert cardinalities the same way (one effect, one card, one report) and recovery states, not mock call counts.
Each example workflow above runs with the scripted model, and a blind test gives a fresh model only the tool description and runs what it writes.
Chat: vitest for the parser, store and presenter; Playwright with placeholder frames for the card, panel, question card and jobs-bar row, and through `/sync` for delivery.

## Pull requests

| PR | Content | Net source lines (estimate) |
|---|---|---|
| B1 | Run store with migrations, models, replay rules | +680 |
| B2a | SDK core, workflow attempts (placement, the launch `entry` field, live approval policy, the fixed attempt limit) | +410 |
| B2b | Engine core, gateway routes on both listeners, `tool` and `shell` nodes with dispatch handles and receipt phases | +430 |
| B3a | `start_workflow`, `get_workflow`, `cancel_workflow`, the intent and the admitted `workflow` job kind, the inline path, registration | +410 |
| B3b | Replay in the engine, `resume_workflow`, working time, restart recovery; the first acceptance test with barriers | +460 |
| B4 | Agent nodes: grant, launch path, a job per turn with generations, the job filter, continuation with the saved prompt, approval waits | +660 |
| B4b | An approval wait across a restart: restore, then a follow-up turn, then `indeterminate` | +250 |
| B5 | `foreach`, `parallel`, `PartialFailure`, concurrency, joins, the usage ledger, nested runs, the gate on diverted calls | +580 |
| B6 | Graph frames with fitting and the counts-only form, the run card, room-state rule, `io.mindroom.workflow_control` (resume) | +530 |
| B7 | `schema`, `ask_user`, `!answer`, `max_cost_usd`, the large-run notice | +560 |
| B8 | Saved workflows (`save_workflow`, `list_workflow_runs`, `name=`, `run`, `!workflows`), deprecation note | +250 |
| B9a | Sessions (`spawn`) | +250 |
| B9b | `workflow_peers` with exactly-once operations, slot release and deadlock notice, the agents' `ask_user` | +500 |
| C1 | Chat: sync filter, parser, store, run card, question card, jobs-bar row | +750 |
| C2 | Chat: graph panel with message-count edges | +700 |

Backend total about +5,970, Chat about +1,450; removing `dynamic_workflow` later gives back about 3,000.
The round 2 cuts took out about 480 backend and 150 Chat lines, and the round 2 fixes (the intent, receipt phases, the launch field, the results route, the restore path) put about 600 backend lines back.
Order: B1, B2a, B2b, B3a, B3b, B4, B4b, B5, B6 and B7 in sequence; B8 after B6; B9a after B7 and B9b after B9a; C1 is built against a pinned contract after B6 and merges after B7 and a check against real captured frames; C2 after C1 and B9b.
No PR is planned above 700 lines; one that passes its estimate by more than 40% stops for the `stopping-runaway-prs` check.
Two checkpoints report real sizes and crash-test results to Bas before work goes on: after B3b and after B5.
Each PR's live gate uses only what has shipped: tools only at B3b, sequential text agents at B4, a text-only diamond at B5, examples 1 and 2 at B7, example 3 at B9a, example 6 at B9b.
The docs page `docs/tools/workflows.md` grows inside each backend PR.

## Known limits, and what the build must prove

Each line is a claim from reading code; the named test is its proof, and its PR does not merge without it.

- **Approval wait across a restart** (Task 11b, `test_crash_on_either_side_of_an_approval_decision_does_not_repeat_a_write`): until B4b merges, an agent that waited for approval at a restart runs again when it had called no tool and otherwise ends `indeterminate`; restore is unproven until its spike passes on a graceful restart and on a kill.
- **Ambiguous writes** (Task 8, `kill_after_write.py --barrier before-receipt` and `after-receipt`): one line in the file in both cases; `indeterminate` and no report in the first, a cached copy and one report in the second.
- **Admission crash points** (Task 8, `test_crash_between_admission_and_launch_launches_once`, `test_intent_without_a_job_is_deleted`).
- **A wait longer than the attempt limit** (Task 8, `test_attempt_that_hits_its_process_limit_is_resumed_while_working_time_is_left`, and Task 18, `test_waiting_past_the_attempt_limit_then_an_answer_continues_the_run`, both on a real process).
- **The launch field on a real worker** (Task 4, `test_workflow_entry_crosses_the_worker_wire_and_the_shim_validates_before_it_dispatches`, and the Docker run of B3b's live gate).
- **A sender lane that has not started** (Task 23, `test_sender_lane_not_yet_started_is_not_a_deadlock`, with the real SDK).
- **Large typed results** (Tasks 3, 5 and 10, `test_result_over_the_inline_limit_is_fetched_in_full_and_keeps_its_type` and its two engine tests, after a restart too, and on a Docker worker in B3b's live gate).
- A provider that repeats a tool call ID with the same arguments inside one model run is treated as a retry of that call; nothing can tell the two apart.
- Rows a frame left out, and the full uncapped message history, are read through the owner's agent in v1; the room sees the capped previews of the Sub-agents feed.

## Decisions on the open questions

Round 1:

1. A workflow may run the caller, agents in `delegate_to`, and agents joined to the run's room that the requester may address.
2. `dynamic_workflow` is deprecated at parity and removed after two weeks of dogfooding, with old records, the usage kind and report publishing kept readable.
3. Local runs need no new setting: they follow `defaults.allow_shell_without_worker` or an explicit local sandbox mode, and a configured worker that is down never falls back to the host.
4. `ask_user` asks the person and `Session.ask` asks an agent.
5. `foreach` and `parallel` raise `PartialFailure` after all siblings settle, with partial results and errors by position.
6. `max_runtime_hours` is 72 by default and configurable; waiting for a person does not count, and an answer or Resume continues the run.

Round 2, decided by both reviewers at Bas's request:

7. An approval wait across a restart restores the exact paused child, then falls back to a follow-up turn, then to `indeterminate`; it is its own PR after B4.
8. The PRs are built in order with checkpoints after B3b and B5; sessions and peers stay in v1 as separate PRs.

No question is open.
