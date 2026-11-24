"""Human questions (script and child ask_user, cards, answers) and peer send/wait on WorkflowEngine."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Literal, cast

from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import utc_timestamp
from mindroom.workflow_runs.answers import MAX_OPTION_BYTES, MAX_OPTIONS, RETRY_SECONDS, show_question
from mindroom.workflow_runs.graph import node_label
from mindroom.workflow_runs.models import TERMINAL_RUN_STATES, WorkflowNode, WorkflowNodeState, WorkflowRun
from mindroom.workflow_runs.peers import NodeRef, read_call, valid_wait_seconds
from mindroom.workflow_runs.peers import send as send_peer_message
from mindroom.workflow_runs.replay import node_digest, result_digest
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.workflow_runs.engine import WorkflowEngine

_NOT_ASKED = "The question could not be posted in the run's thread yet. Try again in a moment."
_NOT_ANSWERED = "The run ended before the question was answered."
_TOO_MANY_OPTIONS = "A question takes at most 20 options of at most 200 bytes each."


def poster(engine: WorkflowEngine, run: WorkflowRun) -> nio.AsyncClient | None:
    """Return the client of the bot that serves *run*, which posts for it, or None while the run may not act."""
    context = engine.authorize(run)
    return None if context is None or isinstance(context, str) else context.client


async def asked(engine: WorkflowEngine, run: WorkflowRun, node: WorkflowNode, epoch: int) -> WorkflowNode:
    """Send the card of a question that waits and has none yet, and return the node with it.

    While the card cannot be sent the request fails as a transport error does, which the script's SDK repeats.
    """
    if node.kind != "ask" or node.state is not WorkflowNodeState.WAITING or "event_id" in node.links:
        return node
    client = poster(engine, run)
    event_id = None if client is None else await show_question(client, run, node)
    if event_id is None:
        raise ScriptRuntimeUnavailableError(_NOT_ASKED)
    links = {**node.links, "event_id": event_id}
    await engine.stamp(node, epoch, links=links)
    return replace(node, links=links)


async def close_card(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["answered", "cancelled"],
    answer_text: str | None = None,
    retries: tuple[float, ...] = (),
) -> None:
    """Show on a question's card that it was answered or that nobody can answer it any more; best effort."""
    if (client := poster(engine, run)) is not None:
        await show_question(client, run, node, status, answer_text, retries)


async def retry_question_cards(engine: WorkflowEngine, root: str) -> None:
    """Post any unposted waiting question cards across *root* and its live descendants."""

    def live_tree(run_id: str) -> list[str]:
        with suppress(WorkflowRunNotFoundError):
            if engine.store.get_run(run_id).state not in TERMINAL_RUN_STATES:
                return [run_id, *(cid for c in engine.store.child_runs(run_id) for cid in live_tree(c.run_id))]
        return []

    for run_id in await asyncio.to_thread(live_tree, root):
        async with engine.condition(run_id):
            with suppress(WorkflowRunNotFoundError):
                run = engine.store.get_run(run_id)
                if run.state in TERMINAL_RUN_STATES:
                    continue
                for node in await asyncio.to_thread(engine.store.unposted_questions, run_id):
                    with suppress(ScriptRuntimeUnavailableError, SupersededAttemptError):
                        await asked(engine, run, node, run.epoch)


def validate_question_options(options: object) -> None:
    """Validate question *options*, rejecting non-list, empty, or oversized option values."""
    if options is None:
        return
    if not (isinstance(options, list) and options and all(isinstance(o, str) and o.strip() for o in options)):
        msg = "options must be a list of strings, such as ['Yes', 'No']."
        raise WorkflowRunStoreError(msg)
    if len(options) > MAX_OPTIONS or any(len(o.encode()) > MAX_OPTION_BYTES for o in options):
        raise WorkflowRunStoreError(_TOO_MANY_OPTIONS)


async def _wait_child_question(engine: WorkflowEngine, run_id: str, ask_key: str) -> str:
    cond = engine.condition(run_id)
    while True:
        async with cond:
            cur = await asyncio.to_thread(engine.store.get_node, run_id, ask_key)
            run = await asyncio.to_thread(engine.store.get_run, run_id)
            if cur is None or cur.state is WorkflowNodeState.CANCELLED or engine.run_ended(run_id):
                raise WorkflowRunStoreError(_NOT_ANSWERED)
            if cur.state is WorkflowNodeState.COMPLETED:
                return str(cur.result)
            if "event_id" in cur.links:
                await engine.wait_for_change(cond)
                continue
        posted = False
        with suppress(ScriptRuntimeUnavailableError, SupersededAttemptError):
            posted = "event_id" in (await asked(engine, run, cur, run.epoch)).links
        if not posted:
            async with cond:
                await engine.wait_for_change(cond)


async def ask_child_question(
    engine: WorkflowEngine,
    run_id: str,
    parent: WorkflowNode,
    question: str,
    options: list[str] | None,
) -> str:
    """Post (or reattach to) an agent's own question card, release its slot while waiting, and return the answer."""
    validate_question_options(options)
    ask_key = f"{parent.key}?{parent.generation}:{result_digest([question, options])[:12]}"
    req: dict[str, object] = {"question": question, "options": options}
    async with (cond := engine.condition(run_id)):
        run, saved = engine.store.get_run(run_id), engine.store.get_node(run_id, ask_key)
        if saved is not None and saved.state in (WorkflowNodeState.COMPLETED, WorkflowNodeState.WAITING):
            if saved.attempt != run.attempt or saved.stale:
                await engine.stamp(saved, run.epoch, ignore=True, attempt=run.attempt, stale=False)
            ask_node = saved
        elif engine.run_ended(run_id):
            raise WorkflowRunStoreError(_NOT_ANSWERED)
        else:
            ask_node = WorkflowNode(
                run_id=run_id,
                key=ask_key,
                kind="ask",
                lane=parent.lane,
                idx=parent.idx,
                digest=node_digest("ask", req),
                request=req,
                generation=1 if saved is None else saved.generation + 1,
                state=WorkflowNodeState.WAITING,
                waiting_on="user",
                attempt=run.attempt,
                label=node_label("ask", req),
                phase=parent.phase,
                started_at=utc_timestamp(),
            )
            ask_node = await asyncio.to_thread(engine.store.put_node, ask_node, run.epoch, dirty_after=False)
        if ask_node.state is WorkflowNodeState.COMPLETED:
            return str(ask_node.result)
        engine.drop_slot(run_id, parent.key)
        await engine.restate(run_id)
        cond.notify_all()

    runtime, job_id = engine.agents(), parent.links.get("job_id")
    wait_job = (
        (runtime.jobs, job_id)
        if runtime is not None and isinstance(job_id, str) and runtime.jobs.retained_job(job_id)
        else None
    )
    # Cleared only on normal/error exit (not CancelledError), so shutdown keeps wait="user" for recovery.
    clear_wait = None if wait_job is None else partial(wait_job[0].save_adapter, wait_job[1], wait=None)
    async with engine.yield_slot(run_id, parent.key, before_reacquire=clear_wait):
        if wait_job is None:
            return await _wait_child_question(engine, run_id, ask_key)
        jobs, jid = wait_job
        await jobs.save_adapter(jid, wait="user")
        async with jobs.human_decision_wait(jid):
            return await _wait_child_question(engine, run_id, ask_key)


async def answer(engine: WorkflowEngine, run_id: str, key: str, generation: int, answer_text: str) -> bool:
    """Complete the question at *key* with a person's *answer_text*; false when that generation no longer waits.

    The answer is saved under the run's lock and epoch, as every write of the run is, also while no attempt
    runs: the next attempt reads it back and does not ask again.
    """
    async with (cond := engine.condition(run_id)):
        run, node = engine.store.get_run(run_id), engine.store.get_node(run_id, key)
        waits = node is not None and (node.state, node.waiting_on) == (WorkflowNodeState.WAITING, "user")
        # An answer is an act of the run, to a card somebody was shown: both are checked as they are now.
        context = engine.authorize(run)
        ok = waits and "event_id" in node.links and context is not None and not isinstance(context, str)
        if run.state in TERMINAL_RUN_STATES or node is None or node.generation != generation or not ok:
            return False
        if not await engine.settle_node(node, run.epoch, state=WorkflowNodeState.COMPLETED, result=answer_text):
            return False
        await engine.restate(run_id)
        cond.notify_all()
    # Without the lock, which no send to the room holds up for long.
    await close_card(engine, run, node, "answered", answer_text, RETRY_SECONDS)
    return True


async def peer_send(
    engine: WorkflowEngine,
    run_id: str,
    *,
    op_id: str,
    sender: NodeRef,
    to: str,
    text: str,
) -> dict[str, object]:
    """Send one peer message in a single transaction and wake waiters of *run_id*."""
    result = await asyncio.to_thread(
        partial(send_peer_message, engine.store, run_id, op_id=op_id, sender=sender),
        to=to,
        text=text,
    )
    async with (cond := engine.condition(run_id)):
        cond.notify_all()
    return result


async def _await_peer_mail(
    engine: WorkflowEngine,
    cond: asyncio.Condition,
    run_id: str,
    read_fn: Callable[..., dict[str, object] | None],
    wait_seconds: float,
    deadline: float,
) -> dict[str, object]:
    while True:
        if (found := await asyncio.to_thread(read_fn, record_empty=False)) is not None:
            return found
        if engine.run_ended(run_id):
            return {"messages": []}
        if (remaining := deadline - engine.clock()) <= 0:
            note = f"No message arrived within {wait_seconds:g} s."
            return cast("dict[str, object]", await asyncio.to_thread(read_fn, record_empty=True, empty_note=note))
        await engine.wait_for_change(cond, remaining)


async def wait_for_messages(
    engine: WorkflowEngine,
    run_id: str,
    reader: NodeRef,
    wait_seconds: float,
    *,
    op_id: str,
    names: tuple[str, ...],
) -> dict[str, object]:
    """Read unread peer messages, or wait on *run_id*'s condition while yielding *reader*'s slot."""
    wait_seconds = valid_wait_seconds(wait_seconds)
    read_fn = partial(read_call, engine.store, run_id, op_id=op_id, reader=reader, names=names)
    cond, deadline = engine.condition(run_id), engine.clock() + wait_seconds
    async with cond:
        if (found := await asyncio.to_thread(read_fn, record_empty=wait_seconds <= 0)) is not None:
            return found
        if engine.run_ended(run_id):
            return {"messages": []}
        node = engine.store.get_node(run_id, reader.key)
        same = node if node is not None and node.generation == reader.generation else None

        async def stamp_state(state: WorkflowNodeState, waiting_on: str | None = None) -> None:
            if same is None:
                return
            with suppress(WorkflowRunNotFoundError):
                if (cur := engine.store.get_run(run_id)).state not in TERMINAL_RUN_STATES:
                    await engine.stamp(same, cur.epoch, ignore=True, state=state, waiting_on=waiting_on)

        await stamp_state(WorkflowNodeState.WAITING, "peer")
        engine.drop_slot(run_id, reader.key)
        await engine.restate(run_id)
        cond.notify_all()

    async def resume() -> None:
        await stamp_state(WorkflowNodeState.RUNNING)
        await engine.notify(run_id)

    async with engine.yield_slot(run_id, reader.key, after_reacquire=resume):
        async with cond:
            result = await _await_peer_mail(engine, cond, run_id, read_fn, wait_seconds, deadline)
            await stamp_state(WorkflowNodeState.QUEUED)
            await engine.restate(run_id)
            cond.notify_all()
        return result


async def wait_inbox(engine: WorkflowEngine, run: WorkflowRun, node: WorkflowNode, epoch: int, wait: float) -> None:
    """Wait for a script's `inbox()` message call and settle its node when mail arrives or the deadline expires.

    `script_op` already validated *wait*; raising here would leave the node waiting in a background task.
    """
    reader = NodeRef("script", 1, "script")
    read_fn = partial(
        read_call,
        engine.store,
        run.run_id,
        op_id=f"{node.key}:{node.generation}",
        reader=reader,
        names=("script",),
    )
    cond, deadline = engine.condition(run.run_id), engine.clock() + wait
    async with cond:
        outcome = await _await_peer_mail(engine, cond, run.run_id, read_fn, wait, deadline)
    await engine.settle_node(node, epoch, state=WorkflowNodeState.COMPLETED, result=outcome, ignore=True)
    await engine.notify(run.run_id)
