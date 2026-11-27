"""Script gateway calls, replay resolution, broker tool dispatch, delegated gates, and node reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict, cast
from uuid import NAMESPACE_OID, uuid5

from mindroom.delegation.state import RUN_SUBAGENT, SubagentPersona
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import TERMINAL_SCRIPT_RUN_STATES, ScriptToolGrant, utc_timestamp
from mindroom.script_runs.store import (
    ScriptCallNotFoundError,
    ScriptCallRateLimitError,
    ScriptRunNotFoundError,
    ScriptRunStoreError,
)
from mindroom.workflow_runs.agent_nodes import (
    delegated_request,
    dispatch_agent,
    load_follow_up_child,
    node_waits,
    schema_failure,
    settle_agent,
    wait_for_agent,
    works,
)
from mindroom.workflow_runs.faults import hold
from mindroom.workflow_runs.graph import node_label
from mindroom.workflow_runs.interactions import asked, close_card, validate_question_options, wait_inbox
from mindroom.workflow_runs.limits import fits_ceiling
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    SERVED_RUN_STATES,
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.peers import script_op, session_turn_message, valid_wait_seconds, withdraw_unread
from mindroom.workflow_runs.replay import (
    CALLING_NODE,
    DIRTY_DIGEST,
    INTERRUPTED,
    NO_AGENTS,
    OWNER_NOT_READY,
    RUN_ENDED,
    Outcome,
    call_outcome,
    changed,
    decide,
    indeterminate,
    nested_key,
    node_digest,
    receipt,
    result_digest,
    split_key,
    unless_proven,
)
from mindroom.workflow_runs.saved import WORKFLOWS_DIR, validate_workflow_name
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.delegation.state import DelegationChild
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import NodeReceipt, NodeRequest

_CHANGED_WITHOUT_EDIT = (
    "Call n{seq} changed between attempts without a script edit. It probably contains the time or a random value; "
    "use now() or random() from mindroom.workflow."
)
_NO_SESSIONS = (
    "A workflow script cannot call delegate.{function} yet. "
    'Start a fresh agent with agent() or tool("delegate", "run_subagent", ...).'
)
_SESSION_BUSY = "Session '{name}' is already in a turn. Wait for it or spawn another agent."
_STOPPING = "The agent this call replaces is stopping. Try again in a moment."
_NOT_CONTINUED_YET = "The agent of this call has not been continued yet after a restart. Try again in a moment."
_NOT_ANSWERED = "The run ended before the question was answered."
# How long a tool call above its attempt's calls per minute waits before it asks for room again.
_RATE_WAIT_SECONDS = 1.0
# How long after its report a script's exit is looked for, before the script runtime's own pass finds it.
_EXIT_POLL_SECONDS = (0.05, 0.2, 0.5, 1.0, 2.0, 5.0)


class _ReuseStamp(TypedDict, total=False):
    phase: str | None
    members: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _TurnContext:
    """Resolved session, live sibling turns, earlier turns, last completed turn, and digest for one `turn` call."""

    session: WorkflowNode
    in_attempt: list[WorkflowNode]
    earlier: list[WorkflowNode]
    prev: WorkflowNode | None
    digest: str


def _broker_call_id(node: WorkflowNode) -> str:
    return uuid5(NAMESPACE_OID, f"{node.run_id}:{node.key}:{node.generation}").hex


def _transcript_prompt(earlier: list[tuple[str, str]], message: str) -> str:
    """Build the first prompt of a fresh session child from saved earlier exchanges and the new *message*."""
    sections = ["Earlier conversation:", *(f"User: {q}\nAssistant: {a}" for q, a in earlier)]
    return "\n\n".join(sections) + f"\n\n{message}"


def _tool_grant(request: dict[str, object]) -> tuple[ScriptToolGrant, dict[str, object]]:
    """Return the function a tool call's request names and its arguments; a request that names neither is refused."""
    arguments = request.get("arguments")
    if not isinstance(arguments, dict) or not all(isinstance(request.get(k), str) for k in ("toolkit", "function")):
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


async def _current_run(engine: WorkflowEngine, attempt: ScriptRunRecord) -> WorkflowRun:
    """Return the run *attempt* is the current attempt of."""
    if attempt.workflow_run_id is None:
        msg = f"{attempt.run_id} is not a workflow attempt."
        raise LookupError(msg)
    async with engine.condition(attempt.workflow_run_id):
        run = engine.store.get_run(attempt.workflow_run_id)
    if run.epoch != engine.launched.get(attempt.run_id):
        msg = f"Attempt {attempt.run_id} is no longer the attempt of {run.run_id}."
        raise SupersededAttemptError(msg)
    return run


def node_by_id(engine: WorkflowEngine, run_id: str, node_id: str) -> WorkflowNode:
    """Return the saved node *node_id* of *run_id*, raising `LookupError` when missing."""
    saved = engine.store.node_by_id(run_id, node_id)
    if saved is None:
        msg = f"Run {run_id} has no call {node_id}."
        raise LookupError(msg)
    return saved


async def run_info(engine: WorkflowEngine, attempt: ScriptRunRecord) -> dict[str, object]:
    """Return what the attempt's script starts from."""
    run = await _current_run(engine, attempt)
    return {"run_id": run.run_id, "attempt": run.attempt, "args": run.args}


async def resolve_source(engine: WorkflowEngine, context: ToolRuntimeContext, arguments: Mapping[str, Any]) -> bytes:
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
    return await engine.scripts.resolve_source(context, source=source, path=path, create_workspace=name is None)


async def saved_source(engine: WorkflowEngine, attempt: ScriptRunRecord, name: str) -> tuple[bytes, str]:
    """Return the saved workflow bytes and SHA-256 for *name*, snapshotted once per attempt."""
    run = await _current_run(engine, attempt)
    try:
        validate_workflow_name(name)
    except ValueError as exc:
        raise WorkflowRunStoreError(str(exc)) from exc
    if (existing := await asyncio.to_thread(engine.store.get_source, run.run_id, run.attempt, name)) is not None:
        return existing
    context = engine.authorize(run)
    if context is None:
        raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
    if isinstance(context, str):
        raise WorkflowRunStoreError(context)
    try:
        raw = await resolve_source(engine, context, {"name": name})
    except ValueError as exc:
        missing = isinstance(exc.__cause__, FileNotFoundError)
        if missing:
            msg = f"Saved workflow {name!r} not found."
            raise LookupError(msg) from exc
        raise WorkflowRunStoreError(str(exc)) from exc
    sha256 = hashlib.sha256(raw).hexdigest()
    return await asyncio.to_thread(engine.store.put_source, run.run_id, run.attempt, run.epoch, name, raw, sha256)


async def submit_node(engine: WorkflowEngine, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
    """Save and dispatch one call of the attempt's script, or return the receipt of the node its key has."""
    run = await _current_run(engine, attempt)
    # The engine owns a call from its insert to its dispatch, so a caller that goes away strands no running node.
    return await asyncio.shield(engine.spawn(None, admit(engine, run, attempt, request)))


async def admit(
    engine: WorkflowEngine,
    asked_run: WorkflowRun,
    attempt: ScriptRunRecord,
    request: NodeRequest,
) -> NodeReceipt:
    """Admit one call under the run's lock and wake waiters once recorded."""
    # Held from the lookup to the dispatch: two requests with one key cannot both find it free, and the run
    # cannot end in between.
    async with (condition := engine.condition(asked_run.run_id)):
        result = await _admit_gated(engine, engine.store.get_run(asked_run.run_id), asked_run.epoch, attempt, request)
        condition.notify_all()
        return result


async def _admit_gated(
    engine: WorkflowEngine,
    run: WorkflowRun,
    epoch: int,
    attempt: ScriptRunRecord,
    request: NodeRequest,
) -> NodeReceipt:
    """Decide and save one call for the attempt that began with *epoch*, which every write here names."""
    if run.epoch != epoch or run.state not in SERVED_RUN_STATES or engine.is_stopping(run.run_id):
        # Before anything is kept for the attempt: one that the run left behind leaves nothing behind here.
        msg = f"Run {run.run_id} takes no more calls from attempt {attempt.run_id}."
        raise SupersededAttemptError(msg)
    turn_ctx = _turn_context(engine, run, request.key, request.args) if request.kind == "turn" else None
    digest = node_digest(request.kind, request.args) if turn_ctx is None else turn_ctx.digest
    members = _members(request.meta) if request.kind == "group" else None
    saved, previous, reused = await _resolve_saved(engine, run, epoch, request, turn_ctx, digest, members)
    if reused is not None:
        return reused
    return await _dispatch_new(engine, run, epoch, attempt, request, turn_ctx, digest, members, saved, previous)


async def _resolve_saved(
    engine: WorkflowEngine,
    run: WorkflowRun,
    epoch: int,
    request: NodeRequest,
    turn_ctx: _TurnContext | None,
    digest: str,
    members: tuple[str, ...] | None,
) -> tuple[WorkflowNode | None, WorkflowNode | None, NodeReceipt | None]:
    """Apply the replay rules to any saved row at `request.key`; return `(saved, previous, reused_receipt)`."""
    saved = engine.store.get_node(run.run_id, request.key)
    if saved is None:
        return None, None, None
    if saved.attempt == run.attempt and not saved.stale:
        return saved, None, receipt(await asked(engine, run, saved, epoch))
    live, handle = False, None
    if saved.state in LIVE_NODE_STATES:
        live, handle = await reconcile(engine, run, saved, epoch), saved.handle
        saved = cast("WorkflowNode", engine.store.get_node(run.run_id, request.key))
    cached = request.meta.get("cached") is not False
    action = decide(saved, digest, cached=cached)
    if action != "run":
        return saved, None, await _reuse_saved(engine, run, epoch, saved, request.meta, members, action)
    if live and saved.kind == "ask":
        await close_card(engine, run, saved, "cancelled")
    if live and handle is not None:
        await cast("AgentRuntime", engine.agents()).jobs.request_cancel(handle.ref)
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
        await asyncio.to_thread(
            engine.store.append_log,
            run.run_id,
            epoch,
            _CHANGED_WITHOUT_EDIT.format(seq=previous.seq),
        )
    return saved, previous, None


async def _reuse_saved(
    engine: WorkflowEngine,
    run: WorkflowRun,
    epoch: int,
    saved: WorkflowNode,
    meta: dict[str, object],
    members: tuple[str, ...] | None,
    action: str,
) -> NodeReceipt:
    """Adopt a saved node for this attempt (`hit`, `attach`, or `raise_indeterminate`)."""
    extra: _ReuseStamp = {}
    if (phase := cast("str | None", meta.get("phase"))) != saved.phase:
        extra["phase"] = phase
    if regrouped := members != saved.members:
        extra["members"] = members
    await engine.stamp(
        saved,
        epoch,
        dirty_after=regrouped,
        attempt=run.attempt,
        stale=False,
        cached=action == "hit",
        **extra,
    )
    if action == "raise_indeterminate":
        return replace(receipt(saved), state=WorkflowNodeState.FAILED)
    if action == "attach":
        if saved.kind == "ask":
            await asked(engine, run, saved, epoch)
        else:
            wait_for_agent(engine, run, saved, epoch)
        await engine.restate(run.run_id)
    return replace(receipt(saved), cached=action == "hit")


def _new_call_error(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    grant: ScriptToolGrant | None,
    delegated: bool,
    turn_ctx: _TurnContext | None,
) -> dict[str, object] | None:
    """Return a terminal error dict if a new tool/agent/turn call is refused before dispatch, else None."""
    if grant is not None:
        access = engine.authorize(run, grant)
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


async def _dispatch_new(
    engine: WorkflowEngine,
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
    shown, asked_args = ("agent", delegated_request(arguments)) if delegated else (kind, request.args)
    node_name = cast("str | None", meta.get("name"))
    if turn_ctx is not None:
        node_name = turn_ctx.session.name or node_name
    elif shown == "session":
        node_name = cast("str | None", asked_args.get("name")) or node_name
    node = WorkflowNode(
        run_id=run.run_id,
        key=key,
        kind=shown,
        lane=lane,
        idx=index,
        digest=digest,
        request=asked_args,
        generation=generation,
        state=WorkflowNodeState.COMPLETED,
        attempt=run.attempt,
        label=node_label(shown, asked_args, meta.get("label") or asked_args.get("name")),
        name=node_name,
        phase=cast("str | None", meta.get("phase")),
        members=members,
        session_key=None if turn_ctx is None else turn_ctx.session.key,
        started_at=utc_timestamp(),
    )
    if (error := _new_call_error(engine, run, node, grant, delegated, turn_ctx)) is not None:
        failed = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=node.started_at)
        return receipt(await asyncio.to_thread(engine.store.put_node, failed, epoch, dirty_after=True))
    if saved is not None and shown in ("agent", "turn", "send"):
        await asyncio.to_thread(withdraw_unread, engine.store, run.run_id, key, saved.generation)
    if shown in ("agent", "turn"):
        if grant is not None:
            return await _ask_gate(engine, run, attempt, node, (grant, arguments))
        follow_up = None
        if turn_ctx is not None:
            mail = partial(
                session_turn_message,
                engine.store,
                run.run_id,
                turn_ctx.session,
                key,
                generation,
                str(asked_args["message"]),
            )
            turn_msg = await asyncio.to_thread(mail, prev_turn=turn_ctx.prev)
            follow_up, prompt = await _prepare_turn(engine, run, turn_ctx, saved, turn_msg)
            node = replace(node, request={**asked_args, "prompt": prompt, "effective_message": turn_msg})
        start = partial(dispatch_agent, engine, run, timeout=_timeout(meta), follow_up=follow_up)
        return await engine.enter(run, node, start)
    if grant is not None:
        cached = meta.get("cached") is not False
        same_as = (
            result_digest(await asyncio.to_thread(engine.store.whole_result, previous))
            if previous is not None and not cached
            else None
        )
        start = partial(_dispatch_tool, engine, run, attempt, tool_call=(grant, arguments), same_as=same_as)
        return await engine.enter(run, node, start, dirty_after=same_as is None)
    return await _save_immediate(engine, run, epoch, node, request)


async def _save_immediate(
    engine: WorkflowEngine,
    run: WorkflowRun,
    epoch: int,
    node: WorkflowNode,
    request: NodeRequest,
) -> NodeReceipt:
    """Save a non-dispatched call (`ask`, `send`, `inbox`, `state`, `value`, `error`, `group`, `session`)."""
    kind = request.kind
    if kind == "ask":
        validate_question_options(request.args.get("options"))
        over = await engine.check_budget(run)
        ask_node = replace(
            node,
            state=WorkflowNodeState.QUEUED if over else WorkflowNodeState.WAITING,
            waiting_on=None if over else "user",
            ended_at=None,
        )
        ask_node = await asyncio.to_thread(engine.store.put_node, ask_node, epoch, dirty_after=True)
        if ask_node.state is WorkflowNodeState.WAITING:
            await engine.restate(run.run_id)
        return receipt(await asked(engine, run, ask_node, epoch))
    if kind in ("send", "inbox", "state"):
        try:
            done, value = await asyncio.to_thread(
                script_op,
                engine.store,
                run.run_id,
                node.key,
                node.generation,
                kind,
                request.args,
            )
        except WorkflowRunStoreError as exc:
            node = replace(node, state=WorkflowNodeState.FAILED, error={"kind": "failed", "message": str(exc)})
        else:
            if not done:
                waiting = replace(node, state=WorkflowNodeState.WAITING, waiting_on="peer", ended_at=None)
                wait_node = await asyncio.to_thread(engine.store.put_node, waiting, epoch, dirty_after=True)
                engine.spawn(run.run_id, wait_inbox(engine, run, wait_node, epoch, valid_wait_seconds(value)))
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
    return receipt(await asyncio.to_thread(engine.store.put_node, done_node, epoch, dirty_after=True))


def _turn_number(turn: WorkflowNode) -> int:
    value = turn.request["turn"]
    if type(value) is not int:
        msg = "Session turn number must be a whole number of at least 1."
        raise WorkflowRunStoreError(msg)
    return value


def _turn_context(engine: WorkflowEngine, run: WorkflowRun, key: str, args: dict[str, object]) -> _TurnContext:
    """Resolve a `turn` call's session, its earlier turns in this attempt, and its lineage-hashed digest."""
    session_key, turn_num = args.get("session"), args.get("turn")
    session = engine.store.get_node(run.run_id, session_key) if isinstance(session_key, str) else None
    if session is None or session.kind != "session":
        msg = f"Unknown session '{session_key}'."
        raise WorkflowRunStoreError(msg)
    if type(turn_num) is not int or turn_num < 1:
        msg = "Session turn number must be a whole number of at least 1."
        raise WorkflowRunStoreError(msg)
    in_attempt = [
        turn
        for turn in engine.store.session_turns(run.run_id, session.key)
        if turn.attempt == run.attempt and not turn.stale
    ]
    earlier = sorted(
        (turn for turn in in_attempt if turn.key != key and _turn_number(turn) < turn_num),
        key=lambda turn: (_turn_number(turn), turn.seq),
    )
    prev = next((t for t in reversed(earlier) if t.state is WorkflowNodeState.COMPLETED), None)
    lineage = [(session.digest, session.generation), *((t.key, t.digest, t.generation) for t in earlier)]
    digest = result_digest({"kind": "turn", "args": args, "lineage": lineage})
    return _TurnContext(session, in_attempt, earlier, prev, digest)


async def _prepare_turn(
    engine: WorkflowEngine,
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
        context = engine.authorize(run)
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
            if isinstance(res := await asyncio.to_thread(engine.store.whole_result, turn), str)
            else json.dumps(res),
        )
        for turn in completed
    ]
    return None, _transcript_prompt(history, message)


async def _ask_gate(
    engine: WorkflowEngine,
    run: WorkflowRun,
    attempt: ScriptRunRecord,
    node: WorkflowNode,
    tool_call: tuple[ScriptToolGrant, dict[str, object]],
) -> NodeReceipt:
    """Save the agent call a script made through `tool()`, and ask the broker whether that tool call may be made.

    The node is saved as a queued one is, with no handle: nothing is started before the gate answers, so a
    restart or a stop meanwhile finds a call that never ran. The caller holds the run's lock.
    """
    queued = replace(node, state=WorkflowNodeState.QUEUED)
    node = await asyncio.to_thread(engine.store.put_node, queued, run.epoch, dirty_after=True)
    if not await engine.check_budget(run):
        gate = partial(_broker_outcome, engine, attempt, node, run.epoch, tool_call, gate_only=True)
        engine.spawn(run.run_id, start_gated(engine, run, node, gate))
    return receipt(node)


async def start_gated(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    gate: Callable[[], Awaitable[Outcome]],
) -> None:
    """Start the agent of a call the broker let pass, in its turn among the run's calls; a refusal is its end."""
    outcome = await gate()
    async with engine.condition(run.run_id):
        now = await asyncio.to_thread(engine.store.get_run, run.run_id)
        if now.epoch != run.epoch or engine.is_stopping(run.run_id):
            # The run ended while the gate was open: its end settled the call, and nothing of it starts now.
            return
        if outcome[0] is WorkflowNodeState.COMPLETED:
            if await engine.check_budget(now):
                return
            engine.enqueue(run, node.key, partial(dispatch_agent, engine, run, node, timeout=None))
        else:
            await engine.settle_node(node, run.epoch, state=outcome[0], error=outcome[2], ignore=True)
    await engine.notify(run.run_id)


async def _dispatch_tool(
    engine: WorkflowEngine,
    run: WorkflowRun,
    attempt: ScriptRunRecord,
    node: WorkflowNode,
    *,
    tool_call: tuple[ScriptToolGrant, dict[str, object]],
    same_as: str | None,
) -> NodeReceipt:
    """Save a tool node with the handle of its call, then run the call as the engine's own task.

    With *same_as*, the digest of its earlier result, the call's lane is dirty only if it answers something else;
    without, it ran because a rule said so, and its lane is dirty whatever it answers. The attempt's epoch is
    the run's here, and the call's task keeps it.
    """
    call_id = _broker_call_id(node)
    # Saved before the dispatch, so a crash after the tool ran reads the broker's receipt for this call.
    handle = DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False, same_as=same_as)
    node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle)
    saved = await asyncio.to_thread(engine.store.save_dispatched, node, run.epoch, dirty_after=same_as is None)
    if saved is None:
        # The node that waited for its place has an end by now: its run ended.
        raise SupersededAttemptError(RUN_ENDED)
    node = saved
    call = partial(_broker_outcome, engine, attempt, node, run.epoch, tool_call)
    # The call has its task before anything else may fail: a saved call nobody runs would never end.
    engine.spawn(run.run_id, _run_tool_node(engine, node, run.epoch, call))
    # A run that waited for a person works again from here.
    await engine.restate(run.run_id)
    return receipt(node)


async def _broker_outcome(
    engine: WorkflowEngine,
    attempt: ScriptRunRecord,
    node: WorkflowNode,
    epoch: int,
    tool_call: tuple[ScriptToolGrant, dict[str, object]],
    *,
    gate_only: bool = False,
) -> Outcome:
    """Run one broker call for *node* within the rate limit and return its node outcome."""
    waits = partial(node_waits, engine, node, epoch, gate_only=gate_only)
    call = partial(
        engine.broker.execute_for_workflow,
        attempt,
        call_id=_broker_call_id(node),
        grant=tool_call[0],
        arguments=tool_call[1],
        waits=waits,
        gate_only=gate_only,
    )
    while True:
        try:
            return call_outcome(node, await call())
        except ScriptCallRateLimitError:
            await asyncio.sleep(_RATE_WAIT_SECONDS)
        except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
            return WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}


async def _run_tool_node(
    engine: WorkflowEngine,
    node: WorkflowNode,
    epoch: int,
    call: Callable[[], Awaitable[Outcome]],
) -> None:
    """Run one tool call and settle its node."""
    # A `workflow(action="start", ...)` this call makes starts a run inside this one, under this node.
    CALLING_NODE.set((node.run_id, node.key, node.generation))
    try:
        outcome = await call()
        await hold("before_settle")
        # A run that moved on meanwhile reads the call's receipt itself.
        with suppress(SupersededAttemptError):
            await asyncio.to_thread(_settle_broker, engine, node, epoch, outcome)
        await engine.release_slot(node.run_id, node.key)
    except BaseException:
        engine.drop_slot(node.run_id, node.key)
        raise


def settle_from_handle(engine: WorkflowEngine, node: WorkflowNode, epoch: int) -> WorkflowNodeState:
    """Settle a node an ended attempt left unsettled from the broker receipt its handle names; return its state.

    The work is never done again here: a finished receipt is copied, and a call that never reached its tool fails
    as safe to run again. *epoch* is the run's as whoever settles the node found it: the attempt that reached
    the node again, or the look that ends the attempt.
    """
    assert node.handle is not None
    script_run_id, _, call_id = node.handle.ref.partition(":")
    try:
        outcome = call_outcome(node, engine.scripts.store.get_call(script_run_id, call_id))
    except ScriptCallNotFoundError:
        # No claim proves the call never started only while the attempt's records are still there to hold one.
        try:
            engine.scripts.store.get_run(script_run_id)
            started: bool | None = False
        except ScriptRunNotFoundError:
            started = None
        outcome = unless_proven(node, started, (WorkflowNodeState.FAILED, None, INTERRUPTED))
    return _settle_broker(engine, node, epoch, outcome)


def _settle_broker(engine: WorkflowEngine, node: WorkflowNode, epoch: int, outcome: Outcome) -> WorkflowNodeState:
    if outcome[0] is WorkflowNodeState.COMPLETED and (interrupted := _nested_interrupted(engine, node)) is not None:
        outcome = interrupted
    state, result, error = outcome
    engine.store.settle_node(
        node.run_id,
        node.key,
        node.generation,
        epoch,
        state=state,
        result=result,
        error=error,
        dirty_after=changed(node, outcome),
    )
    return state


def _is_nested_workflow_start(request: dict[str, object]) -> bool:
    if request.get("toolkit") != "workflow":
        return False
    function = request.get("function")
    if function in {"workflow_write", "workflow"}:
        arguments = request.get("arguments")
        return isinstance(arguments, dict) and arguments.get("action") == "start"
    return function == "start_workflow"


def _nested_interrupted(engine: WorkflowEngine, node: WorkflowNode) -> Outcome | None:
    """Return a non-completed outcome when this generation's nested run ended interrupted."""
    if not _is_nested_workflow_start(node.request):
        return None
    for child in engine.store.child_runs(node.run_id):
        if child.parent_node_key != node.key or child.state is not WorkflowRunState.INTERRUPTED:
            continue
        expected = nested_key((node.run_id, node.key, node.generation), child.source.encode(), child.args)
        if child.owner == expected:
            reason = child.error or f"Nested run {child.run_id} ended interrupted."
            return WorkflowNodeState.CANCELLED, None, {"kind": "interrupted", "message": reason}
    return None


async def reconcile(  # noqa: C901
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    stop: bool = False,
) -> bool:
    """Settle a node that an ended attempt left live from what its handle names; return whether its job still runs.

    A job that still runs is left as it is, for the attempt that reaches its node to wait for. With *stop* the
    run has ended and `_stop_agents` waited for the job: it is read as it stands, and so is one that did not
    end in time. The caller holds the run's lock.
    """
    if node.kind == "ask":
        # A question outlives its attempt and not its run: nothing of it ran, and its card says it is closed.
        if stop:
            error = {"kind": "cancelled", "message": _NOT_ANSWERED}
            await engine.settle_node(node, epoch, state=WorkflowNodeState.CANCELLED, error=error)
            await close_card(engine, run, node, "cancelled")
        return not stop
    if node.handle is None or node.handle.kind == "run":
        # Still queued for a place when its attempt or its run ended: it started nothing. Or the group of a run
        # inside this one, which does not outlive it and is reached only as it ends: the end of that run
        # settles the group, or this does.
        end = WorkflowNodeState.CANCELLED if stop else WorkflowNodeState.FAILED
        error = {"kind": "cancelled", "message": RUN_ENDED} if stop else INTERRUPTED
        if node.handle is not None:
            await engine.cancel(node.handle.ref, reason=RUN_ENDED)
        await engine.settle_node(node, epoch, state=end, error=error)
        return False
    if node.handle.kind != "job":
        await asyncio.to_thread(settle_from_handle, engine, node, epoch)
        return False
    runtime, job_id = engine.agents(), node.handle.ref
    if runtime is None and not stop:
        # Nobody can say how the job stands: the script's SDK asks again.
        raise ScriptRuntimeUnavailableError(NO_AGENTS)
    if runtime is not None and not stop and any(job.job_id == job_id for job in runtime.jobs.awaiting_continuation()):
        # A restart stopped the job, and whether it goes on is not decided: until then it is neither live nor
        # ended, and the call must not attach to a job that may yet end without having done anything.
        raise ScriptRuntimeUnavailableError(_NOT_CONTINUED_YET)
    if runtime is not None and works(runtime, job_id):
        if not stop:
            return True
        await runtime.jobs.request_cancel(job_id)
    try:
        await settle_agent(engine, run, node, epoch, block=False)
        if not stop:
            await engine.check_budget(engine.store.get_run(run.run_id))
    except ScriptRuntimeUnavailableError:
        if not stop:
            # The lock is not held while access resolves: the script's SDK asks again, and a Stop gets in.
            raise
        # A Stop waits for nobody's access and for no job runtime: the turn it may not read may have done
        # anything. A handle without the mark that its job was handed over proves nothing either, since the job
        # starts before that mark is saved.
        state, _, error = indeterminate(node)
        await engine.settle_node(node, epoch, state=state, error=error)
    return False


async def node_receipt(
    engine: WorkflowEngine,
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
        run = await _current_run(engine, attempt)
        async with (condition := engine.condition(run.run_id)):
            saved = node_by_id(engine, run.run_id, node_id)
            remaining = deadline - loop.time()
            waiting_changed = known_state is not None and (
                (saved.state is WorkflowNodeState.WAITING) != (known_state == WorkflowNodeState.WAITING)
            )
            if saved.state not in LIVE_NODE_STATES or waiting_changed or remaining <= 0:
                return receipt(saved)
            await engine.wait_for_change(condition, remaining)


async def node_record(engine: WorkflowEngine, attempt: ScriptRunRecord, node_id: str) -> WorkflowNode:
    """Return one node of the attempt's run, for its whole result."""
    return node_by_id(engine, (await _current_run(engine, attempt)).run_id, node_id)


async def append_log(engine: WorkflowEngine, attempt: ScriptRunRecord, text: str) -> None:
    """Add one progress line to the attempt's run."""
    run = await _current_run(engine, attempt)
    await asyncio.to_thread(engine.store.append_log, run.run_id, run.epoch, text)


async def _observe_exit(engine: WorkflowEngine, run_id: str, script_run_id: str) -> None:
    """End the run soon after its script reported: look for the exit instead of waiting for the next pass."""
    for delay in _EXIT_POLL_SECONDS:
        await asyncio.sleep(delay)
        if (await engine.scripts.reconcile_durable(run_id=script_run_id)).state in TERMINAL_SCRIPT_RUN_STATES:
            # Only the run that reported: another run's ended attempt is the next pass's to judge.
            with suppress(WorkflowRunNotFoundError):
                run = await asyncio.to_thread(engine.store.get_run, run_id)
                if run.script_run_id == script_run_id:
                    await engine.settle(run)
            break


async def finish(engine: WorkflowEngine, attempt: ScriptRunRecord, *, result: object, error: str | None) -> None:
    """Store what the script's `main` returned or raised; the run ends when the attempt's process has.

    Also for a run that shows `waiting`: a call an earlier attempt left with a person, which this attempt's
    script did not reach, takes nothing from the report of the attempt the run serves.
    """
    run = await _current_run(engine, attempt)
    report = partial(engine.store.update_run, run.run_id, run.epoch, when=SERVED_RUN_STATES)
    await asyncio.to_thread(report, result=result, error=error, finished_at=utc_timestamp())
    engine.spawn(run.run_id, _observe_exit(engine, run.run_id, attempt.run_id))
