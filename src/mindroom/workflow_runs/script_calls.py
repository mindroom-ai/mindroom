"""Serve a workflow attempt's script calls: admission, replay, node receipts, run_info, sources, log, and finish."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mindroom.delegation.state import RUN_SUBAGENT, SubagentPersona
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import ScriptToolGrant, utc_timestamp
from mindroom.workflow_runs.agent_nodes import (
    delegated_request,
    dispatch_agent,
    load_follow_up_child,
    schema_failure,
    wait_for_agent,
)
from mindroom.workflow_runs.coordination import ADMISSIONS, wait_for_change
from mindroom.workflow_runs.graph import node_label
from mindroom.workflow_runs.limits import fits_ceiling
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    SERVED_RUN_STATES,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
)
from mindroom.workflow_runs.peers import (
    script_op,
    session_turn_message,
    withdraw_unread,
)
from mindroom.workflow_runs.replay import (
    DIRTY_DIGEST,
    OWNER_NOT_READY,
    decide,
    node_digest,
    receipt,
    result_digest,
    split_key,
)
from mindroom.workflow_runs.saved import WORKFLOWS_DIR, validate_workflow_name
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from mindroom.delegation.state import DelegationChild
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.coordination import RunCoordinator
    from mindroom.workflow_runs.dispatch import NodeDispatcher
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.interactions import RunInteractions
    from mindroom.workflow_runs.models import NodeReceipt, NodeRequest

_CHANGED_WITHOUT_EDIT = (
    "Call n{seq} changed between attempts without a script edit. It probably contains the time or a random value; "
    "use now() or random() from mindroom.workflow."
)
_NO_SESSIONS = 'A workflow script cannot call delegate.{function} yet. Start a fresh agent with agent() or tool("delegate", "run_subagent", ...).'
_SESSION_BUSY = "Session '{name}' is already in a turn. Wait for it or spawn another agent."
_STOPPING = "The agent this call replaces is stopping. Try again in a moment."


@dataclass(frozen=True, slots=True)
class _TurnContext:
    """Resolved session, live sibling turns, earlier turns, last completed turn, and digest for one `turn` call."""

    session: WorkflowNode
    in_attempt: list[WorkflowNode]
    earlier: list[WorkflowNode]
    prev: WorkflowNode | None
    digest: str


def _transcript_prompt(earlier: list[tuple[str, str]], message: str) -> str:
    """Build the first prompt of a fresh session child from saved earlier exchanges and the new *message*."""
    sections = ["Earlier conversation:", *(f"User: {asked}\nAssistant: {answered}" for asked, answered in earlier)]
    return "\n\n".join(sections) + f"\n\n{message}"


def _tool_grant(request: dict[str, object]) -> tuple[ScriptToolGrant, dict[str, object]]:
    """Return the function a tool call's request names and its arguments; a request that names neither is refused."""
    arguments = request.get("arguments")
    if not isinstance(arguments, dict) or not all(
        isinstance(request.get(name), str) for name in ("toolkit", "function")
    ):
        msg = "A tool call needs a toolkit, a function and its arguments."
        raise WorkflowRunStoreError(msg)
    return ScriptToolGrant(str(request["toolkit"]), str(request["function"])), arguments


def _timeout(meta: dict[str, object]) -> float | None:
    """Return the seconds of work an agent call gives its turn, which is no part of what the call is."""
    seconds = meta.get("timeout")
    return float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None


def _members(meta: dict[str, object]) -> tuple[str, ...]:
    """Return the ordered lanes a group's request names."""
    members = meta.get("members")
    if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
        msg = "A group call needs the list of its members."
        raise WorkflowRunStoreError(msg)
    return tuple(str(member) for member in members)


def _value(args: dict[str, object]) -> object:
    """Make the value a `now()` or `random()` call of a script gets, once: a resume returns the saved one."""
    if args.get("op") == "now":
        return datetime.now(UTC).isoformat()
    if args.get("op") == "random":
        return secrets.SystemRandom().random()
    msg = f"A value call is now or random, not {args.get('op')!r}."
    raise WorkflowRunStoreError(msg)


class ScriptCalls:
    """Serve the attempt's script: admit node calls, poll receipts, and handle run_info, source, log, and finish."""

    def __init__(
        self,
        engine: WorkflowEngine,
        live: RunCoordinator,
        ask: RunInteractions,
        dispatch: NodeDispatcher,
        *,
        authorize: Callable[..., ToolRuntimeContext | str | None],
        observe_exit: Callable[[str, str], Coroutine[None, None, None]],
    ) -> None:
        self._engine = engine
        self.live = live
        self.ask = ask
        self.dispatch = dispatch
        self._authorize = authorize
        self._observe_exit = observe_exit

    async def run_info(self, attempt: ScriptRunRecord) -> dict[str, object]:
        """Return what the attempt's script starts from."""
        run = await self._current_run(attempt)
        return {"run_id": run.run_id, "attempt": run.attempt, "args": run.args}

    async def resolve_source(self, context: ToolRuntimeContext, arguments: Mapping[str, Any]) -> bytes:
        """Return the script a start names: inline source, workspace file, or saved workflow."""
        source, path, name = arguments.get("source"), arguments.get("path"), arguments.get("name")
        if sum(value is not None for value in (source, path, name)) != 1:
            msg = "Give exactly one of source, path, or name."
            raise WorkflowRunStoreError(msg)
        if name is not None:
            try:
                validate_workflow_name(str(name))
            except ValueError as exc:
                raise WorkflowRunStoreError(str(exc)) from exc
            path = f"{WORKFLOWS_DIR}/{name}.py"
        return await self.live.scripts.resolve_source(
            context,
            source=source,
            path=path,
            create_workspace=name is None,
        )

    async def saved_source(self, attempt: ScriptRunRecord, name: str) -> tuple[bytes, str]:
        """Return the saved workflow bytes and SHA-256 for *name*, snapshotted once per attempt."""
        run = await self._current_run(attempt)
        try:
            validate_workflow_name(name)
        except ValueError as exc:
            raise WorkflowRunStoreError(str(exc)) from exc
        if (existing := await asyncio.to_thread(self.live.store.get_source, run.run_id, run.attempt, name)) is not None:
            return existing
        context = self._authorize(run)
        if context is None:
            raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
        if isinstance(context, str):
            raise WorkflowRunStoreError(context)
        try:
            raw = await self.resolve_source(context, {"name": name})
        except ValueError as exc:
            missing = isinstance(exc.__cause__, FileNotFoundError)
            if missing:
                msg = f"Saved workflow {name!r} not found."
                raise LookupError(msg) from exc
            raise WorkflowRunStoreError(str(exc)) from exc
        sha256 = hashlib.sha256(raw).hexdigest()
        return await asyncio.to_thread(
            self.live.store.put_source,
            run.run_id,
            run.attempt,
            run.epoch,
            name,
            raw,
            sha256,
        )

    async def submit_node(self, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        """Save and dispatch one call of the attempt's script, or return the receipt of the node its key has."""
        run = await self._current_run(attempt)
        # The engine owns a call from its insert to its dispatch, so a caller that goes away strands no running node.
        return await asyncio.shield(self.live.spawn(ADMISSIONS, self._admit(run, attempt, request)))

    async def _admit(self, asked: WorkflowRun, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        # Held from the lookup to the dispatch: two requests with one key cannot both find it free, and the run
        # cannot end in between.
        async with (condition := self.live.condition(asked.run_id)):
            result = await self._admit_gated(self.live.store.get_run(asked.run_id), asked.epoch, attempt, request)
            condition.notify_all()
            return result

    async def _admit_gated(
        self,
        run: WorkflowRun,
        epoch: int,
        attempt: ScriptRunRecord,
        request: NodeRequest,
    ) -> NodeReceipt:
        """Decide and save one call for the attempt that began with *epoch*, which every write here names."""
        if run.epoch != epoch or run.state not in SERVED_RUN_STATES or run.run_id in self.live.stopping:
            # Before anything is kept for the attempt: one that the run left behind leaves nothing behind here.
            msg = f"Run {run.run_id} takes no more calls from attempt {attempt.run_id}."
            raise SupersededAttemptError(msg)
        turn_ctx = self._turn_context(run, request.key, request.args) if request.kind == "turn" else None
        digest = node_digest(request.kind, request.args) if turn_ctx is None else turn_ctx.digest
        members = _members(request.meta) if request.kind == "group" else None
        saved, previous, reused = await self._resolve_saved(run, epoch, request, turn_ctx, digest, members)
        if reused is not None:
            return reused
        return await self._dispatch_new(run, epoch, attempt, request, turn_ctx, digest, members, saved, previous)

    async def _resolve_saved(
        self,
        run: WorkflowRun,
        epoch: int,
        request: NodeRequest,
        turn_ctx: _TurnContext | None,
        digest: str,
        members: tuple[str, ...] | None,
    ) -> tuple[WorkflowNode | None, WorkflowNode | None, NodeReceipt | None]:
        """Apply the replay rules to any saved row at `request.key`; return `(saved, previous, reused_receipt)`."""
        saved = self.live.store.get_node(run.run_id, request.key)
        if saved is None:
            return None, None, None
        if saved.attempt == run.attempt and not saved.stale:
            return saved, None, receipt(await self.ask.asked(run, saved, epoch))
        live, handle = False, None
        if saved.state in LIVE_NODE_STATES:
            live, handle = await self.dispatch.reconcile(run, saved, epoch), saved.handle
            saved = cast("WorkflowNode", self.live.store.get_node(run.run_id, request.key))
        cached = request.meta.get("cached") is not False
        action = decide(saved, digest, cached=cached)
        if action != "run":
            return saved, None, await self._reuse_saved(run, epoch, saved, request.meta, members, action)
        if live and saved.kind == "ask":
            await self.ask.close_card(run, saved, "cancelled")
        if live and handle is not None:
            await cast("AgentRuntime", self.live.agents()).jobs.request_cancel(handle.ref)
            raise ScriptRuntimeUnavailableError(_STOPPING)
        marked = saved.digest in ("", DIRTY_DIGEST)
        previous = saved if saved.state is WorkflowNodeState.COMPLETED and not marked else None
        lineage_reran = turn_ctx is not None and any(not turn.cached for turn in turn_ctx.earlier)
        if (
            previous is not None
            and previous.digest != digest
            and cached
            and not lineage_reran
            and run.source_digest == run.previous_source_digest
        ):
            msg = _CHANGED_WITHOUT_EDIT.format(seq=previous.seq)
            await asyncio.to_thread(self.live.store.append_log, run.run_id, epoch, msg)
        return saved, previous, None

    async def _reuse_saved(
        self,
        run: WorkflowRun,
        epoch: int,
        saved: WorkflowNode,
        meta: dict[str, object],
        members: tuple[str, ...] | None,
        action: str,
    ) -> NodeReceipt:
        """Adopt a saved node for this attempt (`hit`, `attach`, or `raise_indeterminate`)."""
        stamp: dict[str, object] = {"attempt": run.attempt, "stale": False, "cached": action == "hit"}
        if (phase := cast("str | None", meta.get("phase"))) != saved.phase:
            stamp["phase"] = phase
        if regrouped := members != saved.members:
            stamp["members"] = members
        await self.live.stamp(saved, epoch, dirty_after=regrouped, **stamp)
        if action == "raise_indeterminate":
            return replace(receipt(saved), state=WorkflowNodeState.FAILED)
        if action == "attach":
            if saved.kind == "ask":
                await self.ask.asked(run, saved, epoch)
            else:
                wait_for_agent(self._engine, run, saved, epoch)
            await self.live.restate(run.run_id)
        return replace(receipt(saved), cached=action == "hit")

    async def _dispatch_new(
        self,
        run: WorkflowRun,
        epoch: int,
        attempt: ScriptRunRecord,
        request: NodeRequest,
        turn_ctx: _TurnContext | None,
        digest: str,
        members: tuple[str, ...] | None,
        saved: WorkflowNode | None,
        previous: WorkflowNode | None,
    ) -> NodeReceipt:
        """Create and dispatch (or save as settled/failed) a new generation of `request.key`."""
        key, kind, meta = request.key, request.kind, request.meta
        lane, index = split_key(key)
        generation = 1 if saved is None else saved.generation + 1
        grant, arguments = _tool_grant(request.args) if kind == "tool" else (None, request.args)
        delegated = grant is not None and grant.toolkit_name == "delegate"
        shown, asked = ("agent", delegated_request(arguments)) if delegated else (kind, request.args)
        node_name = cast("str | None", meta.get("name"))
        if turn_ctx is not None:
            node_name = turn_ctx.session.name or node_name
        elif shown == "session":
            node_name = cast("str | None", asked.get("name")) or node_name
        node = WorkflowNode(
            run_id=run.run_id,
            key=key,
            kind=shown,
            lane=lane,
            idx=index,
            digest=digest,
            request=asked,
            generation=generation,
            state=WorkflowNodeState.COMPLETED,
            attempt=run.attempt,
            label=node_label(shown, asked, meta.get("label") or asked.get("name")),
            name=node_name,
            phase=cast("str | None", meta.get("phase")),
            members=members,
            session_key=None if turn_ctx is None else turn_ctx.session.key,
            started_at=utc_timestamp(),
        )
        if (error := self._new_call_error(run, node, grant, delegated, turn_ctx)) is not None:
            failed = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=node.started_at)
            return receipt(await asyncio.to_thread(self.live.store.put_node, failed, epoch, dirty_after=True))
        if saved is not None and shown in ("agent", "turn", "send"):
            await asyncio.to_thread(withdraw_unread, self.live.store, run.run_id, key, saved.generation)
        if shown in ("agent", "turn"):
            if grant is not None:
                return await self.dispatch.ask_gate(run, attempt, node, (grant, arguments))
            follow_up = None
            if turn_ctx is not None:
                mail = partial(
                    session_turn_message,
                    self.live.store,
                    run.run_id,
                    turn_ctx.session,
                    key,
                    generation,
                    str(asked["message"]),
                )
                turn_msg = await asyncio.to_thread(mail, prev_turn=turn_ctx.prev)
                follow_up, prompt = await self._prepare_turn(run, turn_ctx, saved, turn_msg)
                node = replace(node, request={**asked, "prompt": prompt, "effective_message": turn_msg})
            dispatch = partial(dispatch_agent, self._engine, run, timeout=_timeout(meta), follow_up=follow_up)
            return await self.live.enter(run, node, dispatch)
        if grant is not None:
            cached = meta.get("cached") is not False
            same_as = (
                result_digest(await asyncio.to_thread(self.live.store.whole_result, previous))
                if previous is not None and not cached
                else None
            )
            start = partial(self.dispatch.dispatch_tool, run, attempt, tool_call=(grant, arguments), same_as=same_as)
            return await self.live.enter(run, node, start, dirty_after=same_as is None)
        return await self._save_immediate(run, epoch, node, request)

    async def _save_immediate(
        self,
        run: WorkflowRun,
        epoch: int,
        node: WorkflowNode,
        request: NodeRequest,
    ) -> NodeReceipt:
        """Save a non-dispatched call (`ask`, `send`, `inbox`, `state`, `value`, `error`, `group`, `session`)."""
        kind = request.kind
        if kind == "ask":
            self.ask.validate_question_options(request.args.get("options"))
            over = await self.live.check_budget(run)
            ask_node = replace(
                node,
                state=WorkflowNodeState.QUEUED if over else WorkflowNodeState.WAITING,
                waiting_on=None if over else "user",
                ended_at=None,
            )
            ask_node = await asyncio.to_thread(self.live.store.put_node, ask_node, epoch, dirty_after=True)
            if ask_node.state is WorkflowNodeState.WAITING:
                await self.live.restate(run.run_id)
            return receipt(await self.ask.asked(run, ask_node, epoch))
        if kind in ("send", "inbox", "state"):
            try:
                call = partial(script_op, self.live.store, run.run_id, node.key, node.generation, kind, request.args)
                done, value = await asyncio.to_thread(call)
            except WorkflowRunStoreError as exc:
                node = replace(node, state=WorkflowNodeState.FAILED, error={"kind": "failed", "message": str(exc)})
            else:
                if not done:
                    waiting = replace(node, state=WorkflowNodeState.WAITING, waiting_on="peer", ended_at=None)
                    wait_node = await asyncio.to_thread(self.live.store.put_node, waiting, epoch, dirty_after=True)
                    self.live.spawn(run.run_id, self.ask.wait_inbox(run, wait_node, epoch, float(value)))
                    return receipt(wait_node)
                node = replace(node, result=value)
        elif kind == "value":
            node = replace(node, result=_value(request.args))
        elif kind == "error":
            message = f"{request.args.get('type')}: {request.args.get('message')}"
            node = replace(node, state=WorkflowNodeState.FAILED, error={"kind": "error", "message": message})
        elif kind not in ("group", "session"):
            msg = f"This MindRoom cannot run a '{kind}' call in a workflow yet."
            raise WorkflowRunStoreError(msg)
        done_node = replace(node, ended_at=node.started_at)
        return receipt(await asyncio.to_thread(self.live.store.put_node, done_node, epoch, dirty_after=True))

    def _new_call_error(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        grant: ScriptToolGrant | None,
        delegated: bool,
        turn_ctx: _TurnContext | None,
    ) -> dict[str, object] | None:
        """Return a terminal error dict if a new tool/agent/turn call is refused before dispatch, else None."""
        if grant is not None:
            access = self._authorize(run, grant)
            if access is None:
                raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
            if isinstance(access, str):
                return {"kind": "failed", "message": access}
        if delegated and grant is not None and grant.function_name != RUN_SUBAGENT:
            return {"kind": "refused", "message": _NO_SESSIONS.format(function=grant.function_name)}
        if (
            node.kind == "turn"
            and turn_ctx is not None
            and any(other.key != node.key and other.state in LIVE_NODE_STATES for other in turn_ctx.in_attempt)
        ):
            return {"kind": "failed", "message": _SESSION_BUSY.format(name=node.name or turn_ctx.session.key)}
        if node.kind in ("agent", "turn"):
            schema_src = {**turn_ctx.session.request, **node.request} if turn_ctx is not None else node.request
            if (bad_schema := schema_failure(schema_src)) is not None:
                return {"kind": "schema", "message": bad_schema}
        return None

    def _turn_context(self, run: WorkflowRun, key: str, args: dict[str, object]) -> _TurnContext:
        """Resolve a `turn` call's session, its earlier turns in this attempt, and its lineage-hashed digest."""
        session_key, turn_num = args.get("session"), args.get("turn")
        session = self.live.store.get_node(run.run_id, session_key) if isinstance(session_key, str) else None
        if session is None or session.kind != "session":
            msg = f"Unknown session '{session_key}'."
            raise WorkflowRunStoreError(msg)
        if type(turn_num) is not int or turn_num < 1:
            msg = "Session turn number must be a whole number of at least 1."
            raise WorkflowRunStoreError(msg)
        in_attempt = [
            turn
            for turn in self.live.store.session_turns(run.run_id, session.key)
            if turn.attempt == run.attempt and not turn.stale
        ]
        earlier = sorted(
            (turn for turn in in_attempt if turn.key != key and int(turn.request["turn"]) < turn_num),
            key=lambda turn: (int(turn.request["turn"]), turn.seq),
        )
        prev = next((t for t in reversed(earlier) if t.state is WorkflowNodeState.COMPLETED), None)
        lineage = [
            (session.digest, session.generation),
            *((turn.key, turn.digest, turn.generation) for turn in earlier),
        ]
        digest = result_digest({"kind": "turn", "args": args, "lineage": lineage})
        return _TurnContext(session, in_attempt, earlier, prev, digest)

    async def _prepare_turn(
        self,
        run: WorkflowRun,
        turn_ctx: _TurnContext,
        saved: WorkflowNode | None,
        message: str,
    ) -> tuple[DelegationChild | None, str]:
        """Decide whether a session turn continues the saved child or starts a fresh child with a transcript."""
        completed = [turn for turn in turn_ctx.earlier if turn.state is WorkflowNodeState.COMPLETED]
        prev = turn_ctx.prev
        prev_sub = prev.links.get("subagent_id") if prev is not None else None
        if prev is not None and isinstance(prev_sub, str):
            context = self._authorize(run)
            if context is None:
                raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
            if not isinstance(context, str):
                child = await load_follow_up_child(context, run, prev_sub)
                allowed = {(prev.key, prev.generation)}
                if (
                    saved is not None
                    and saved.state in (WorkflowNodeState.FAILED, WorkflowNodeState.CANCELLED)
                    and not saved.links.get("made")
                ):
                    allowed.add((saved.key, saved.generation))
                fits = not (
                    run.persona
                    and child is not None
                    and child.child_agent_name == run.agent_name
                    and not fits_ceiling(child.persona, SubagentPersona.from_dict(run.persona))
                )
                if child is not None and (child.workflow_node_key, child.workflow_generation) in allowed and fits:
                    return child, message
        if not completed:
            return None, message
        history = [
            (
                str(turn.request.get("effective_message") or turn.request["message"]),
                res
                if isinstance(res := await asyncio.to_thread(self.live.store.whole_result, turn), str)
                else json.dumps(res),
            )
            for turn in completed
        ]
        return None, _transcript_prompt(history, message)

    async def node_receipt(
        self,
        attempt: ScriptRunRecord,
        node_id: str,
        *,
        wait: float,
        known_state: str | None = None,
    ) -> NodeReceipt:
        """Return a node's receipt, held up to *wait* seconds until it settles or enters/leaves `waiting`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            # Asked again after each wait: the end of the attempt ends its calls too, and those are not its to read.
            run = await self._current_run(attempt)
            async with (condition := self.live.condition(run.run_id)):
                node = self.node_by_id(run.run_id, node_id)
                remaining = deadline - loop.time()
                waiting_changed = known_state is not None and (
                    (node.state is WorkflowNodeState.WAITING) != (known_state == WorkflowNodeState.WAITING)
                )
                if node.state not in LIVE_NODE_STATES or waiting_changed or remaining <= 0:
                    return receipt(node)
                await wait_for_change(condition, remaining)

    async def node(self, attempt: ScriptRunRecord, node_id: str) -> WorkflowNode:
        """Return one node of the attempt's run, for its whole result."""
        return self.node_by_id((await self._current_run(attempt)).run_id, node_id)

    async def append_log(self, attempt: ScriptRunRecord, text: str) -> None:
        """Add one progress line to the attempt's run."""
        run = await self._current_run(attempt)
        await asyncio.to_thread(self.live.store.append_log, run.run_id, run.epoch, text)

    async def finish(self, attempt: ScriptRunRecord, *, result: object, error: str | None) -> None:
        """Store what the script's `main` returned or raised; the run ends when the attempt's process has.

        Also for a run that shows `waiting`: a call an earlier attempt left with a person, which this attempt's
        script did not reach, takes nothing from the report of the attempt the run serves.
        """
        run = await self._current_run(attempt)
        report = partial(self.live.store.update_run, run.run_id, run.epoch, when=SERVED_RUN_STATES)
        await asyncio.to_thread(report, result=result, error=error, finished_at=utc_timestamp())
        self.live.spawn(run.run_id, self._observe_exit(run.run_id, attempt.run_id))

    async def _current_run(self, attempt: ScriptRunRecord) -> WorkflowRun:
        """Return the run *attempt* is the current attempt of."""
        if attempt.workflow_run_id is None:
            msg = f"{attempt.run_id} is not a workflow attempt."
            raise LookupError(msg)
        async with self.live.condition(attempt.workflow_run_id):
            run = self.live.store.get_run(attempt.workflow_run_id)
        if run.epoch != self.live.launched.get(attempt.run_id):
            msg = f"Attempt {attempt.run_id} is no longer the attempt of {run.run_id}."
            raise SupersededAttemptError(msg)
        return run

    def node_by_id(self, run_id: str, node_id: str) -> WorkflowNode:
        """Return the saved node *node_id* of *run_id*, raising `LookupError` when missing."""
        node = self.live.store.node_by_id(run_id, node_id)
        if node is None:
            msg = f"Run {run_id} has no call {node_id}."
            raise LookupError(msg)
        return node
