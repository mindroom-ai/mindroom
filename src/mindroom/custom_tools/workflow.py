"""The `workflow` toolkit: start a workflow run as a background job, read it, cancel it, and resume it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import Counter
from dataclasses import fields, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.background_tasks import run_blocking_or_compensate
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.config.resolution import authored_tool_entries
from mindroom.constants import RuntimePaths  # noqa: TC001  # resolved by tool contract introspection
from mindroom.delegation.lifecycle import running_child_id, running_child_run
from mindroom.matrix.event_types import WORKFLOW_ANSWER_EVENT_TYPE
from mindroom.matrix.state import resolve_room_aliases
from mindroom.script_runs.manager import workflow_placement
from mindroom.tool_jobs.control import job_stopped_by_shutdown
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    JobSpec,
    ManagedJobToolkit,
    call_job_id,
    get_background_runtime,
    workflow_job_standing,
)
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, toolkit_is_background_excluded
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.metadata import resolve_tool_settings
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_hooks import dispatch_external_tool_hooks
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.
from mindroom.workflow_runs.answers import handle_workflow_answer
from mindroom.workflow_runs.limits import WorkflowLimits, ceiling, limit_reached, run_usage
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    TERMINAL_RUN_STATES,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
    new_workflow_run_id,
)
from mindroom.workflow_runs.peers import all_messages, all_state
from mindroom.workflow_runs.replay import calling_node, nested_key, result_digest
from mindroom.workflow_runs.saved import list_saved, save_source
from mindroom.workflow_runs.store import WorkflowRunNotFoundError, WorkflowRunStoreError, owned_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import nio

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode

_START = "start_workflow"
_RESUME = "resume_workflow"
# The spec's teaching text, cut to what this release runs: each later release adds the lines for what it adds. The
# list of models follows it.
_START_DESCRIPTION = """\
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
        draft = agent("Write a report from these notes:\\n\\n" + "\\n\\n".join(notes))
        for _ in range(3):                       # a bounded loop with an independent check
            check = agent("List factual problems in this report.\\n\\n" + draft,
                          schema={"type": "object", "required": ["problems"],
                                  "properties": {"problems": {"type": "array", "items": {"type": "string"}}}})
            if not check["problems"]:
                break
            draft = agent(f"Fix these problems: {check['problems']}\\n\\n{draft}")
        return draft

    def review_loop(task, test_command):         # two agents that keep their conversation, and a hard check
        dev, reviewer = spawn("dev"), spawn("reviewer", system="Review diffs. Reply APPROVE or list problems.")
        dev.ask(f"Implement and commit: {task}")
        for _ in range(5):
            tests, review = shell(test_command), reviewer.ask("Review `git diff main`.")
            if tests.ok and review.startswith("APPROVE"):
                return review
            dev.ask(f"Fix and commit.\\n\\nTests:\\n{tests.output}\\n\\nReview:\\n{review}")
        return "Not approved after 5 rounds:\\n" + review

    def negotiate(feature):                      # two peers negotiating via messages, one shared-state writer
        parallel([
            lambda: agent(f"Propose a JSON API for {feature} to 'frontend' via send_peer_message; poll read_peer_messages(20) "
                          "up to 4 rounds. Once agreed, call set_shared_state('contract', <dict>) and send DONE.", name="backend"),
            lambda: agent("Poll read_peer_messages(20) up to 4 rounds for 'backend', reply via send_peer_message with feedback "
                          "or AGREE, and stop on DONE. Leave shared state to 'backend'.", name="frontend"),
        ])
        return state.get("contract")             # or have another agent call get_shared_state("contract")

- agent(prompt, *, schema=None, name=None, agent=None, model=None, system=None, tools=None, skills=None, knowledge=None,
        profile=None, minimal=False, timeout=None, output_path=None, dedicated_worker=False, resource_profile=None)
  runs one agent turn and returns its answer: text, or parsed JSON when schema (a JSON Schema dict) is given.
  With schema the agent is asked again until its answer matches, up to 5 turns; then AgentError with kind "schema",
  or "indeterminate" when one of its turns used a tool. schema and output_path cannot be used together.
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
  fn may make several calls and may itself call foreach or parallel. At most max_concurrent agents and tools run at once.
- s = spawn(name, ...options of agent) keeps one agent's conversation: s.ask(message, *, schema=None, timeout=None)
  sends the next message and returns the answer. A second ask while the session is in a turn fails.
  Ask two sessions at once with parallel([lambda: a.ask(x), lambda: b.ask(y)]).
- ask_user(question, options=None) asks the user in the thread and returns the answer: one of options (a list of strings),
  or free text without options. Only that branch waits, for as long as it takes; parallel branches keep running.
- shell(command) runs a command with your shell tool and returns .ok, .exit_code and .output. tool(toolkit, function, **arguments) calls any other tool of yours.
  shell() returns at most the last 50 KiB of output; redirect to a file and read it for more.
- phase(title) groups the calls that follow; log(text) shows a progress line. run(name, args) calls a saved workflow inside this one.
- Agents in a run can talk to each other: each automatically has the tools send_peer_message(to, text),
  read_peer_messages(wait_seconds=0), list_peers(), get_shared_state(key=None), set_shared_state(key, value),
  and ask_user(question, options=None), addressed by the name you gave them ("*" reaches every unfinished agent,
  "script" reaches the script).
  Say in each prompt whom to talk to. read_peer_messages(wait_seconds=0) and inbox(wait_seconds=0) return only
  unread messages since the last read as {messages: [{from, text, sent_at}], note?} and advance the cursor;
  waiting frees the slot and ends when mail arrives or after wait_seconds with note "No message arrived within <N> s."
  Prefer short waits in a bounded loop (e.g. 10 to 30 s per wait for a fixed number of rounds) over one long wait.
  Messages to a peer that has not started yet are held until it reads. The script joins in with send(to, text),
  inbox(wait_seconds=0), state.get(key, default=None) and state.set(key, value); agents read and write the same state.
  set_shared_state(key, value) and state.set(key, value) take any JSON value (pass the value directly; don't json.dumps it)
  with last-write-wins semantics, so pick one writer per key. get_shared_state(key) returns {key, found, value}
  (or {key: value, ...} when key is omitted), whereas script state.get(key, default=None) returns the value directly.

Every agent, tool and shell call is saved with its result. The Python between the calls runs again on every resume,
so do writes (files, git, HTTP) through shell(), tool() or an agent, never with plain Python.
resume_workflow reuses saved calls: in each sequence of calls, those before the first changed call are reused; that call and the
calls after it run again, and so does everything after a foreach or parallel in which an item ran again. Unchanged items are reused.
Resume retries only calls proven not to have started. A call that may have written (a tool that raised, an agent that made
tool calls) is indeterminate: check its effect, then name it in rerun.
Use now() and random() from the SDK instead of time and random when the value goes into a prompt.
shell(..., cached=False) and tool(..., cached=False) run again on every resume: use it for a check of something outside the run.
The call returns {run_id, job_id, status, title} at once, and you receive the run's report in full when it ends.
Where the call cannot be a background job, it waits and returns the report itself.
A run that completed although calls failed says so in the first line of the report.
get_workflow(run_id) shows each call and what the run cost: cost_usd for the whole run, attempt_cost_usd for this attempt.
Nothing caps a run's cost unless you pass max_cost_usd: then, once the run has cost that much, no further call starts,
running ones finish, and the run ends interrupted; resume it with a higher max_cost_usd.
An agent that needs an approval waits for the user; it does not fail.
"""
_OWN_RUN = "A call of a run cannot cancel that run or one it is inside. Return from main() or raise to end the script."
_AT_MOST_ONE_SOURCE = "Give at most one of source or path."
_NEEDS_JOBS = "Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow."


_FAILED = frozenset({WorkflowNodeState.FAILED, WorkflowNodeState.INDETERMINATE})


class WorkflowTools(Toolkit, ManagedJobToolkit):
    """Start, read and cancel the workflow runs of one agent and requester."""

    def __init__(
        self,
        runtime_config: Config | None = None,
        agent_name: str | None = None,
        current_room_id: str | None = None,
        runtime_paths: RuntimePaths | None = None,
        max_concurrent_runs: int = 8,
        max_runtime_hours: float = 72,
        max_tool_calls_per_minute: int = 600,
        max_concurrent_agents: int | None = None,
        large_run_notice_usd: float = 10,
    ) -> None:
        self.limits = WorkflowLimits(
            max_concurrent_runs=max_concurrent_runs,
            max_runtime_hours=max_runtime_hours,
            max_tool_calls_per_minute=max_tool_calls_per_minute,
            max_concurrent_agents=max_concurrent_agents,
            large_run_notice_usd=large_run_notice_usd,
        )
        # The models of this deployment, named as `run_subagent` names them, end the description.
        models = sorted((runtime_config.models if runtime_config is not None else {}).items())
        levels = [f"{name}@{model.reasoning.levels[-1]}" for name, model in models if model.reasoning]
        effort = f" (add an effort as in {levels[0]})" if levels else ""
        self._start_description = f"{_START_DESCRIPTION}\nModels: {', '.join(name for name, _ in models)}{effort}."
        # And the agents a run may name: those this agent may delegate to, and those configured for this room.
        agents = runtime_config.agents if runtime_config is not None else {}
        others = list(agents[agent_name].delegate_to) if agent_name in agents else []
        if runtime_paths is not None:
            rooms = {name: resolve_room_aliases(other.rooms, runtime_paths) for name, other in agents.items()}
            others += [name for name in rooms if name not in (agent_name, *others) and current_room_id in rooms[name]]
        if others:
            self._start_description += f"\nAgents you can run with agent=: {', '.join(others)}."
        tools = [
            self.start_workflow,
            self.get_workflow,
            self.cancel_workflow,
            self.resume_workflow,
            self.list_workflow_runs,
            self.save_workflow,
        ]
        super().__init__(name="workflow", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Describe `start_workflow` with the teaching text and this deployment's models."""
        return replace(function, description=self._start_description) if function.name == _START else function

    async def start_workflow(
        self,
        source: str | None = None,
        path: str | None = None,
        name: str | None = None,
        args: dict[str, Any] | None = None,
        title: str | None = None,
        max_concurrent: int = 16,
        resource_profile: ScriptResourceProfileName | None = None,
        max_cost_usd: float | None = None,
    ) -> str:
        """Run a workflow where the call cannot be a background job: wait for the run and return its report.

        A call that is refused fails with the sentence that says why, as every function of this toolkit does.

        Args:
            source: Inline Python source.
            path: Workspace-relative path of a Python file.
            name: Name of a saved workflow; list_workflow_runs shows them and their args.
            args: JSON object passed to main(args).
            title: Three to six words shown on the run's card.
            max_concurrent: How many agents run at once. Raise it for large fan-outs.
            resource_profile: Worker size for the script itself.
            max_cost_usd: Stop the run when it has spent this much; resume it with a higher limit.

        """
        arguments = {
            "source": source,
            "path": path,
            "name": name,
            "args": args,
            "title": title,
            "max_concurrent": max_concurrent,
            "resource_profile": resource_profile,
            "max_cost_usd": max_cost_usd,
        }
        engine, context = _waiting_runtime()
        outer = _outer_call()
        # A run inside a run is found by its script, which is read once, here, for its key and for the run.
        if outer is None:
            text = None
            origin = _inline_origin(context)
        else:
            text = await engine.calls.resolve_source(context, arguments)
            origin = nested_key(outer, text, arguments.get("args"))
        stopped = (
            _resumable_child(engine, outer, text, arguments)
            if outer is not None and text is not None and running_child_run() is None
            else None
        )
        if stopped is not None and outer is not None:
            action = partial(self._continue_child, engine, context, origin, outer, stopped, arguments)
        else:
            action = partial(self._reserve, engine, context, origin, arguments, outer=outer, source=text)
        return await engine.delivery(await engine.attend(origin, action, context, self.limits))

    async def get_workflow(
        self,
        run_id: str,
        nodes: Literal["active", "failed", "all"] = "active",
        messages: bool = False,
    ) -> str:
        """Return a run's status, cost, open questions, failed calls and nodes. nodes is "active" (default), "failed" or "all"; messages=True adds peer messages and shared state.

        Args:
            run_id: The run's ID, as start_workflow returned it.
            nodes: Which calls to list: "active", "failed" or "all".
            messages: When True, include full peer message history and shared state.

        """
        engine, context = _runtime()
        run = _owned(engine, context, run_id)
        listed = await asyncio.to_thread(engine.store.nodes, run.run_id, light=True)
        shown = {"active": LIVE_NODE_STATES, "failed": _FAILED, "all": frozenset(WorkflowNodeState)}[nodes]
        # What each call used, over every time it ran; a call that asked no model has none.
        summary = await asyncio.to_thread(engine.store.usage_summary, run.run_id, attempt=run.attempt)
        used = summary.by_node
        status: dict[str, Any] = {
            "run_id": run.run_id,
            "title": run.title,
            "status": run.state.value,
            "attempt": run.attempt,
            "counts": dict(Counter(node.state.value for node in listed)),
            "usage": await asyncio.to_thread(run_usage, engine.store, run.run_id, root_summary=summary),
            "waiting": [
                {"id": f"n{node.seq}", "label": node.label, "waiting_on": node.waiting_on}
                for node in listed
                if node.state is WorkflowNodeState.WAITING
            ],
            "failed": [
                {"id": f"n{node.seq}", "label": node.label, "error": node.error}
                for node in listed
                if node.state in _FAILED
            ],
            "nodes": [
                _public_node(node) | ({"usage": used[node.key]} if node.key in used else {})
                for node in listed
                if node.state in shown
            ],
        }
        if messages:
            status["messages"] = await asyncio.to_thread(all_messages, engine.store, run.run_id)
            status["shared_state"] = await asyncio.to_thread(all_state, engine.store, run.run_id)
        if run.state in TERMINAL_RUN_STATES:
            # The report, or why there is none, also for a run whose caller is gone.
            status["result"] = run.outcome_text()
        return json.dumps(status, indent=2)

    async def cancel_workflow(self, run_id: str) -> str:
        """Stop a run and its agents. Finished calls stay saved, so resume_workflow can continue it.

        Args:
            run_id: The run's ID, as start_workflow returned it.

        """
        engine, context = _runtime()
        run = _owned(engine, context, run_id)
        # The end of a run waits until its calls have stopped, so a call inside it would wait for itself.
        inside = (_outer_call() or (None,))[0]
        while inside is not None:
            if inside == run.run_id:
                raise WorkflowRunStoreError(_OWN_RUN)
            inside = engine.store.get_run(inside).parent_run_id
        ended = await engine.cancel(run.run_id, reason="Cancelled by the agent.")
        return json.dumps({"run_id": ended.run_id, "status": ended.state.value})

    async def resume_workflow(
        self,
        run_id: str,
        source: str | None = None,
        path: str | None = None,
        rerun: list[str] | None = None,
        max_cost_usd: float | None = None,
    ) -> str:
        """Continue a stopped or failed run, or run a finished one again; saved calls are reused as start_workflow explains. Pass source or path to run an edited script, rerun=[node ids] to make calls run again, such as one that ended indeterminate, and a higher max_cost_usd to continue a run stopped at its cost limit.

        Args:
            run_id: The run's ID, as start_workflow returned it.
            source: Inline Python source that replaces the run's script.
            path: Workspace-relative path of a Python file that replaces the run's script.
            rerun: IDs of saved calls to run again, such as ["n4"].
            max_cost_usd: New cost limit in USD; must exceed what the run has spent when it stopped at its limit.

        """
        engine, context = _waiting_runtime()
        arguments = {
            "run_id": run_id,
            "source": source,
            "path": path,
            "rerun": rerun,
            "max_cost_usd": max_cost_usd,
        }
        origin = _inline_origin(context)
        run, request = await _resumption(engine, context, arguments, self.limits, owner=origin)
        if run.owner != origin and (report := run.claims.get(origin)) is not None:
            # This call took the run before and another has it now: it gets the report of its own attempt.
            return report
        # The call owns the run it takes, as an inline start owns the run it reserves: its leaving stops that run.
        reopen = partial(engine.reopen, run.run_id, context, limits=self.limits, owner=origin, **request)
        return await engine.delivery(await engine.attend(origin, reopen, context, self.limits))

    async def list_workflow_runs(self) -> str:
        """List this conversation's recent runs and your saved workflows (name, description, args)."""
        engine, context = _runtime()
        runs = await asyncio.to_thread(
            engine.store.list_runs,
            agent_name=context.agent_name,
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=context.resolved_thread_id,
            top_level_only=True,
            limit=20,
        )
        try:
            workspace = await asyncio.to_thread(context.agent_workspace_root, create=False)
            saved = await asyncio.to_thread(list_saved, workspace)
        except (OSError, ValueError) as exc:
            raise WorkflowRunStoreError(str(exc)) from exc
        payload = {
            "runs": [
                {
                    "run_id": run.run_id,
                    "title": run.title,
                    "status": run.state.value,
                    "attempt": run.attempt,
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                }
                for run in runs
            ],
            "saved": [{"name": item.name, "description": item.description, "args": item.args} for item in saved],
        }
        return json.dumps(payload, indent=2)

    async def save_workflow(self, run_id: str, name: str, description: str | None = None) -> str:
        """Save a run's script as workflows/<name>.py so start_workflow(name=...) can run it again.

        A saved file documents itself for list_workflow_runs with its module docstring's first line and an optional
        literal `ARGS = {"arg": "meaning"}`.

        Args:
            run_id: The run's ID, as start_workflow returned it.
            name: Name of the saved workflow (1 to 64 lowercase letters, digits, hyphens, or underscores, starting with a letter or digit).
            description: Optional one-line description added as the file's docstring when it has none.

        """
        engine, context = _runtime()
        run = _owned(engine, context, run_id)
        try:
            workspace = await asyncio.to_thread(context.agent_workspace_root, create=True)
            await asyncio.to_thread(save_source, workspace, name, run.source, description)
        except (OSError, ValueError) as exc:
            raise WorkflowRunStoreError(str(exc)) from exc
        return json.dumps({"name": name, "path": f"workflows/{name}.py"})

    async def managed_job(
        self,
        function_name: str,
        arguments: dict[str, Any],
        *,
        job_id: str,
        runtime: ToolJobRuntime,
    ) -> JobSpec | str | None:
        """Return the job of the run a managed `start_workflow` or `resume_workflow` call starts, or why not.

        Nothing runs here: a start writes its intent, a resume is only checked, and the job's operation, which runs
        only once the runtime admitted the job, takes a resumed run and launches every attempt. The other functions
        run as any tool's call does (None).
        """
        if function_name not in (_START, _RESUME):
            return None
        try:
            engine, context = _runtime()
            # The plugins' hooks of a call, which the job's own task does not run for it: a decline reserves nothing.
            hooks = partial(
                dispatch_external_tool_hooks,
                execution_identity=context.execution_identity,
                config=context.config,
                runtime_paths=context.runtime_paths,
                tool_name=function_name,
                arguments=arguments,
            )
            if (declined := await hooks(before=True, registered_toolkit="workflow")) is not None:
                await hooks(before=False, result=declined, blocked=True)
                return declined
            after = partial(hooks, before=False)
            if function_name == _RESUME:
                return await _resume_job(engine, context, arguments, self.limits, job_id, runtime, after=after)
            run = await self._reserve(engine, context, job_id, arguments, job_id=job_id)

            async def refused() -> object:
                return await asyncio.to_thread(engine.store.delete_intent, run.run_id, job_id, run.epoch)

        except ValueError as exc:
            return str(exc)
        return _run_job(engine, run, job_id, context, self.limits, after=after, refused=refused)

    async def _reserve(
        self,
        engine: WorkflowEngine,
        context: ToolRuntimeContext,
        origin: str,
        arguments: Mapping[str, Any],
        job_id: str | None = None,
        outer: tuple[str, str, int] | None = None,
        source: bytes | None = None,
    ) -> WorkflowRun:
        """Check one start and write its intent, or return the run *origin* reserved before; a refusal raises.

        *job_id* names the job the run is reserved for, which no call that waits for its run itself has. *outer* is
        the call of a run that this start is made in: the new run is a run inside that one, under that call, and
        *source* is its script as its key was made from.
        """
        title, max_concurrent = arguments.get("title"), arguments.get("max_concurrent", 16)
        if not isinstance(max_concurrent, int) or max_concurrent < 1:
            msg = "max_concurrent must be a whole number of at least 1."
            raise WorkflowRunStoreError(msg)
        max_cost = _validated_max_cost_usd(arguments.get("max_cost_usd"))
        # The operator's ceiling is the most a start may ask for.
        max_concurrent = min(max_concurrent, self.limits.max_concurrent_agents or max_concurrent)
        placement = workflow_placement(context.config, context.runtime_paths)
        if placement not in ("worker", "local"):
            raise WorkflowRunStoreError(placement)
        text = (source or await engine.calls.resolve_source(context, arguments)).decode()
        hours = self.limits.max_runtime_hours
        intent = WorkflowRun(
            run_id=new_workflow_run_id(),
            agent_name=context.agent_name,
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=context.resolved_thread_id,
            execution_identity=serialize_tool_execution_identity(context.execution_identity),
            # The tools a narrowed agent was left with are the run's, whatever its agent has, and a run inside a
            # run has no more than that run.
            persona=ceiling(context, engine.store.get_run(outer[0]).persona if outer else None),
            # Kept whole: a frame that shows it shortens it.
            title=str(title) if title else None,
            source=text,
            source_digest=hashlib.sha256(text.encode()).hexdigest(),
            args=arguments.get("args"),
            max_concurrent=max_concurrent,
            max_cost_usd=max_cost,
            resource_profile=arguments.get("resource_profile"),
            runtime_seconds_left=hours * 3600 if hours else None,
            origin_job_id=origin,
            owner=origin,
            job_id=job_id,
            parent_run_id=outer[0] if outer else None,
            parent_node_key=outer[1] if outer else None,
            # The agents the run runs are subagents of the one that starts it, one level below it. A script that
            # starts a run is no agent: its run's agents are as deep as its own.
            depth=child.child.depth
            if (child := running_child_run()) is not None
            else engine.store.get_run(outer[0]).depth
            if outer
            else 0,
        )
        store = engine.store

        def take_back(reserved: tuple[WorkflowRun, bool]) -> None:
            if reserved[1]:
                store.delete_intent(reserved[0].run_id, origin, 0)

        # On a thread, where it waits for the store's writer. A call cancelled meanwhile takes back the intent it
        # wrote; the engine keeps an inline caller's cancellation away from here and ends the run itself.
        reserve = partial(store.reserve_run, intent, max_active=self.limits.max_concurrent_runs)
        run, created = await run_blocking_or_compensate(reserve, take_back)
        if outer is not None and created:
            await engine.nest(run)
        return run

    async def _continue_child(
        self,
        engine: WorkflowEngine,
        context: ToolRuntimeContext,
        origin: str,
        outer: tuple[str, str, int],
        child: WorkflowRun,
        arguments: Mapping[str, Any],
    ) -> WorkflowRun:
        """Reopen an interrupted nested run under its outer node and refresh its group node."""
        reopened = await engine.reopen(
            child.run_id,
            context,
            max_cost_usd=_validated_max_cost_usd(arguments.get("max_cost_usd")),
            limits=self.limits,
            owner=origin,
            epoch=child.epoch,
            persona=ceiling(context, engine.store.get_run(outer[0]).persona),
        )
        await engine.nest(reopened)
        return reopened


def _resumable_child(
    engine: WorkflowEngine,
    outer: tuple[str, str, int],
    source: bytes,
    arguments: Mapping[str, Any],
) -> WorkflowRun | None:
    """Return the latest interrupted nested run of *outer*'s node with matching script and args, if any."""
    digest, args_digest = hashlib.sha256(source).hexdigest(), result_digest(arguments.get("args"))
    children = [
        child
        for child in engine.store.child_runs(outer[0])
        if child.parent_node_key == outer[1]
        and child.source_digest == digest
        and result_digest(child.args) == args_digest
    ]
    return children[0] if children and children[0].state is WorkflowRunState.INTERRUPTED else None


async def _resume_job(
    engine: WorkflowEngine,
    context: ToolRuntimeContext,
    arguments: Mapping[str, Any],
    limits: WorkflowLimits,
    job_id: str,
    runtime: ToolJobRuntime,
    after: Callable[..., Awaitable[object]] | None = None,
) -> JobSpec:
    """Return the job that resumes the run *arguments* name, for a tool call or a person's Resume; a refusal raises.

    The one check of a resume. A call or an event delivered again names the job that already holds what was
    accepted, and is checked no more.
    """
    known = runtime.retained_job(job_id)
    accepted = known.adapter.get("resume") if known is not None else None
    run, request = await _resumption(engine, context, arguments, limits, accepted, owner=job_id)
    return _run_job(engine, run, job_id, context, limits, after=after, resume=request)


async def _resumption(
    engine: WorkflowEngine,
    context: ToolRuntimeContext,
    arguments: Mapping[str, Any],
    limits: WorkflowLimits,
    accepted: dict[str, Any] | None = None,
    *,
    owner: str,
) -> tuple[WorkflowRun, dict[str, Any]]:
    """Check one resume and return its run and the request its job is admitted with; a refusal raises.

    The request names the epoch the run has now, so it takes the run only if nothing else did since.
    *accepted* is the request this same call was admitted with before, which is returned unchecked, and so is
    the call that finds the run its own already, or among those that took it: *owner*, the job or the waiting
    call, took it before, and the write that takes a run refuses it a second time.
    """
    source, path, rerun = arguments.get("source"), arguments.get("path"), arguments.get("rerun") or ()
    max_cost = _validated_max_cost_usd(arguments.get("max_cost_usd"))
    if source is not None and path is not None:
        raise WorkflowRunStoreError(_AT_MOST_ONE_SOURCE)
    run = _owned(engine, context, str(arguments.get("run_id")))
    if accepted is not None or owner in (run.owner, *run.claims):
        return run, accepted or {}
    engine.resumable(run.run_id, context, rerun, limits, max_cost_usd=max_cost)
    delivered = ("absent", "stopped", "ended")
    if run.job_id is not None and workflow_job_standing(context.runtime_paths, run.job_id) not in delivered:
        msg = f"Run {run.run_id} is still delivering its report. Try again in a moment."
        raise WorkflowRunStoreError(msg)
    edited = source is not None or path is not None
    text = (await engine.scripts.resolve_source(context, source=source, path=path)).decode() if edited else None
    # What the resume may hand on: the turn's persona, and no more than the run its call is made in keeps.
    outer = _outer_call()
    persona = ceiling(context, engine.store.get_run(outer[0]).persona if outer else None)
    request: dict[str, Any] = {"source": text, "rerun": list(rerun), "epoch": run.epoch, "persona": persona}
    if max_cost is not None:
        request["max_cost_usd"] = max_cost
    return run, request


def _validated_max_cost_usd(value: object) -> float | None:
    """Return *value* as a positive finite USD amount, None when omitted, or raise."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < math.inf:
        msg = "max_cost_usd must be a positive amount in USD."
        raise WorkflowRunStoreError(msg)
    return float(value)


async def handle_workflow_event(
    room: nio.MatrixRoom,
    event: nio.UnknownEvent,
    *,
    agent_name: str,
    engine: Callable[[], WorkflowEngine],
) -> None:
    """Act on a person's workflow event for the bot of *agent_name*: an answer to a question, or a Resume."""
    handle = handle_workflow_answer if event.type == WORKFLOW_ANSWER_EVENT_TYPE else _handle_workflow_control
    await handle(room, event, agent_name=agent_name, engine=engine)


async def _handle_workflow_control(
    room: nio.MatrixRoom,
    event: nio.UnknownEvent,
    *,
    agent_name: str,
    engine: Callable[[], WorkflowEngine],
) -> bool:
    """Resume the run a person's control event names, as `resume_workflow` does, and return whether it did.

    Every bot in the room gets the event. Only `{run, action: "resume"}` from the run's requester, in the run's room
    and thread, to the run's agent does anything: the run is checked as the tool's resume is and gets a new job, named
    after the event so that the event delivered again starts nothing. Anything else changes and sends nothing.
    """
    content, sender = event.source.get("content"), event.source.get("sender")
    if not isinstance(content, dict) or content.get("action") != "resume":
        return False
    relation = content.get("m.relates_to")
    threaded = isinstance(relation, dict) and relation.get("rel_type") == "m.thread"
    where = (agent_name, sender, room.room_id, relation.get("event_id") if threaded else None)
    workflows = engine()
    try:
        run = workflows.store.get_run(str(content.get("run")))
        if (workflows.served_by(run), run.owner_user_id, run.room_id, run.thread_root_event_id) != where:
            return False
        context = workflows.authorize(run)
        if (
            context is None
            or isinstance(context, str)
            or (jobs := get_background_runtime(context.runtime_paths)) is None
        ):
            return False
        limits = await asyncio.to_thread(workflow_limits, context, run.agent_name)
        job_id = hashlib.sha256(f"workflow_control:{event.event_id}".encode()).hexdigest()
        if job_id in (run.owner, *run.claims):
            # The event took the run before, and its job is no longer kept: delivered again, it starts nothing.
            return False
        async with jobs.admission(job_id):
            max_cost = content.get("max_cost_usd")
            if max_cost is None and run.max_cost_usd is not None:
                spent = (await asyncio.to_thread(run_usage, workflows.store, run.run_id))["cost_usd"]
                if limit_reached(spent, run.max_cost_usd):
                    max_cost = spent + run.max_cost_usd
            arguments = {"run_id": run.run_id, "max_cost_usd": max_cost}
            job = await _resume_job(workflows, context, arguments, limits, job_id, jobs)
            # In the owner's context, which the job's task keeps, as the job of a call's resume has it.
            with tool_runtime_context(context):
                _, claim = await jobs.start_spec(job_id, job, owner=context.execution_identity, depth=run.depth)
            # Nobody waits for this job, so nobody keeps its outcome: its report is a follow-up in the thread.
            await jobs.release_wait(job_id, claim)
    except ValueError:
        # No such run, a run that cannot be resumed now, or a job the runtime does not admit: nothing was started.
        return False
    return True


def _run_job(
    engine: WorkflowEngine,
    run: WorkflowRun,
    job_id: str,
    context: ToolRuntimeContext,
    limits: WorkflowLimits,
    *,
    after: Callable[..., Awaitable[object]] | None = None,
    refused: Callable[[], Awaitable[object]] | None = None,
    resume: dict[str, Any] | None = None,
) -> JobSpec:
    """Return the job of *run*: it starts each attempt the run waits for, and ends as the run ends.

    While the job is unfinished only its operation starts an attempt of the run, in *context* and under *limits* as
    `engine.admit` allows, so a Stop of the job cancels the one thing that could start one. *after* gets the report
    for the plugins' hooks, and *refused* undoes a start the runtime did not admit. *resume* is the accepted request
    of a resume, saved with the job so that a restart goes on with exactly it: the operation takes the run with it
    first, unless the run is this job's already.
    """
    run_id = run.run_id

    async def operation() -> BackgroundOutcome:
        launch = partial(engine.launch, run_id, context, limits, owner=job_id)
        try:
            if resume is not None:
                await engine.reopen(run_id, context, limits=limits, owner=job_id, job_id=job_id, **resume)
            outcome = await _workflow_job_outcome(engine, await engine.wait(run_id, launch, job_id))
        except WorkflowRunStoreError as exc:
            # Another call took the run: this job owns nothing of it, waits for none of its reports, and says so.
            return BackgroundOutcome("failed", str(exc), encode_result_payload(ToolResultPayload(value=str(exc))))
        if after is not None:
            await after(result=outcome.result)
        return outcome

    def handle(job: BackgroundJob) -> str:
        return json.dumps({"job_id": job.job_id, "run_id": run_id, "status": job.status, "title": run.title})

    return JobSpec(
        tool_name="workflow",
        kind="workflow",
        toolkit_name="workflow",
        # Never changed afterwards, so a redelivered call names the same job and reattaches to it.
        adapter={"workflow_job": job_id, "workflow": run_id} | ({"resume": resume} if resume is not None else {}),
        operation=operation,
        cancel=partial(stopped_workflow_job, engine),
        refused=refused,
        handle=handle,
    )


async def stopped_workflow_job(
    engine: WorkflowEngine,
    job: BackgroundJob,
    *,
    live: bool = True,
) -> BackgroundOutcome | None:
    """Stop the run of a stopped job and return how the job ends: as its run ended, while that run is still its own.

    A shutdown (`live=True`, `job_stopped_by_shutdown`) cancels no live run, so the next start can continue it, while
    a run that has already ended settles its job immediately instead of keeping it running across the restart. A
    restart recovery (`live=False`) of a running job defers to `continued_workflow_job`; a Stop that a restart found
    saved (`not job_stopped_by_shutdown`) returns the report of a run that had already ended.
    """
    if not live and job_stopped_by_shutdown():
        return None
    run_id = str(job.adapter.get("workflow"))
    try:
        run = engine.store.get_run(run_id)
        if live and not job_stopped_by_shutdown():
            run = await engine.cancel(run_id, owner=job.job_id, **engine.job_stop(run, "The run's job was cancelled."))
    except WorkflowRunNotFoundError:
        return None
    # A run of another owner is left: a resume that never took it, or a job whose run was resumed since.
    ended = run.owner == job.job_id and run.state in TERMINAL_RUN_STATES
    return await _workflow_job_outcome(engine, run) if ended else None


async def continued_workflow_job(job: BackgroundJob) -> JobSpec | BackgroundOutcome:
    """Return what the job of a workflow run goes on as after a restart stopped it, or its outcome if it already ended.

    Called in the tool context of the job's conversation, once the job runtime granted the job again. The job waits
    for its run again and can cancel it, so every attempt the run waits for starts there: the first, the next once
    the engine's pass recorded the old one's end, and that of a resume that was admitted and stopped before it took
    the run. A run that has already ended settles the job at once, and one that is gone raises, which ends the job.
    """
    engine, context = _runtime()
    run = engine.store.get_run(str(job.adapter.get("workflow")))
    if run.owner == job.job_id and run.state in TERMINAL_RUN_STATES:
        return await _workflow_job_outcome(engine, run)
    limits = workflow_limits(context, run.agent_name)
    return _run_job(engine, run, job.job_id, context, limits, resume=job.adapter.get("resume"))


def workflow_limits(context: ToolRuntimeContext, agent_name: str) -> WorkflowLimits:
    """Return the limits the agent's `workflow` entry sets now, for a run that goes on with no call of the toolkit."""
    entries = authored_tool_entries(context.config, agent_name)
    overrides = next((entry.tool_config_overrides for entry in entries if entry.name == "workflow"), None)
    target = context.resolve_worker_target()
    allowed = context.config.get_worker_grantable_credentials() if target.worker_scope is not None else None
    settings = resolve_tool_settings(
        "workflow",
        context.runtime_paths,
        tools=config_plugins(context.config, context.runtime_paths).tools,
        worker_target=target,
        allowed_shared_services=allowed,
        tool_config_overrides=overrides,
    )
    names = {limit.name for limit in fields(WorkflowLimits)}
    limits: dict[str, Any] = {name: value for name, value in settings.values.items() if name in names}
    return WorkflowLimits(**limits)


async def _workflow_job_outcome(engine: WorkflowEngine, run: WorkflowRun) -> BackgroundOutcome:
    """Return how the job of an ended run ends: with what the run delivers."""
    if run.state not in TERMINAL_RUN_STATES:
        raise ValueError(run.state)
    status = cast('Literal["completed", "failed", "cancelled", "interrupted"]', run.state.value)
    text = await engine.delivery(run)
    return BackgroundOutcome(status, text, encode_result_payload(ToolResultPayload(value=text)))


def _public_node(node: WorkflowNode) -> dict[str, object]:
    return {
        "id": f"n{node.seq}",
        "kind": node.kind,
        "label": node.label,
        "status": node.state.value,
        "error": node.error,
        **({"links": node.links} if node.links else {}),
    }


def _runtime() -> tuple[WorkflowEngine, ToolRuntimeContext]:
    context = get_tool_runtime_context()
    if context is None or context.orchestrator is None:
        msg = "Workflows need an active room context."
        raise WorkflowRunStoreError(msg)
    return context.orchestrator.workflow_engine, context


def _waiting_runtime() -> tuple[WorkflowEngine, ToolRuntimeContext]:
    """Return the runtime of a call that waits for its run itself, which only a call inside another agent's turn may."""
    engine, context = _runtime()
    if running_child_id() is None and not _jobs_enabled(context):
        # A top-level call is a job or nothing.
        raise WorkflowRunStoreError(_NEEDS_JOBS)
    return engine, context


def _owned(engine: WorkflowEngine, context: ToolRuntimeContext, run_id: str) -> WorkflowRun:
    return owned_run(engine.store, run_id, agent_name=context.agent_name, requester_id=context.requester_id)


def _jobs_enabled(context: ToolRuntimeContext) -> bool:
    config, paths = context.config, context.runtime_paths
    return background_tool_jobs_enabled(config, paths) and not toolkit_is_background_excluded("workflow", config, paths)


def _outer_call() -> tuple[str, str, int] | None:
    """Return the run, the node and its generation of the workflow call this call is made in, if it is made in one.

    That is the turn of an agent a run started, or a script's own `tool()` call.
    """
    child = running_child_run()
    if child is None or child.child.workflow_run_id is None:
        return calling_node()
    return child.child.workflow_run_id, str(child.child.workflow_node_key), child.child.workflow_generation or 1


def _inline_origin(context: ToolRuntimeContext) -> str:
    """Return the key a call that waits for its run owns that run by: its owner, its model run, and the call.

    A delivery of the same call finds its run again by it. A provider's call ID alone is never a key, so a call
    without its run gets a key of its own and is never found again.
    """
    call = current_tool_call()
    if call is None or not call.context.run_id or not call.context.call_id:
        return f"inline:{uuid4().hex}"
    return "inline:" + call_job_id(context.execution_identity, call.context.run_id, call.context.call_id)
