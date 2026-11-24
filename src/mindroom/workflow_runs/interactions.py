"""Human questions (script and child ask_user, cards, answers) and peer send/wait for workflow runs."""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Literal, cast

from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import utc_timestamp
from mindroom.workflow_runs.coordination import wait_for_change
from mindroom.workflow_runs.graph import node_label
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
)
from mindroom.workflow_runs.notices import RETRY_SECONDS
from mindroom.workflow_runs.peers import NodeRef, read_call
from mindroom.workflow_runs.peers import send as send_peer_message
from mindroom.workflow_runs.questions import MAX_OPTION_BYTES, MAX_OPTIONS, show_question
from mindroom.workflow_runs.replay import node_digest, result_digest
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.coordination import RunCoordinator

_NOT_ASKED = "The question could not be posted in the run's thread yet. Try again in a moment."
_NOT_ANSWERED = "The run ended before the question was answered."
_TOO_MANY_OPTIONS = "A question takes at most 20 options of at most 200 bytes each."


class RunInteractions:
    """Human questions (cards and answers) and peer send/wait, releasing and retaking slots via the coordinator."""

    def __init__(
        self,
        live: RunCoordinator,
        authorize: Callable[[WorkflowRun], ToolRuntimeContext | str | None],
    ) -> None:
        self.live = live
        self._authorize = authorize

    def poster(self, run: WorkflowRun) -> nio.AsyncClient | None:
        """Return the client of the bot that serves *run*, which posts for it, or None while the run may not act."""
        context = self._authorize(run)
        return None if context is None or isinstance(context, str) else context.client

    async def asked(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> WorkflowNode:
        """Send the card of a question that waits and has none yet, and return the node with it.

        While the card cannot be sent the request fails as a transport error does, which the script's SDK repeats.
        """
        if node.kind != "ask" or node.state is not WorkflowNodeState.WAITING or "event_id" in node.links:
            return node
        client = self.poster(run)
        event_id = None if client is None else await show_question(client, run, node)
        if event_id is None:
            raise ScriptRuntimeUnavailableError(_NOT_ASKED)
        links = {**node.links, "event_id": event_id}
        await self.live.stamp(node, epoch, links=links)
        return replace(node, links=links)

    async def close_card(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        status: Literal["answered", "cancelled"],
        answer: str | None = None,
        retries: tuple[float, ...] = (),
    ) -> None:
        """Show on a question's card that it was answered or that nobody can answer it any more; best effort."""
        if (client := self.poster(run)) is not None:
            await show_question(client, run, node, status, answer, retries)

    async def retry_question_cards(self, root: str) -> None:
        """Post any unposted waiting question cards across *root* and its live descendants."""

        def live_tree(run_id: str) -> list[str]:
            try:
                run = self.live.store.get_run(run_id)
            except WorkflowRunNotFoundError:
                return []
            if run.state in TERMINAL_RUN_STATES:
                return []
            ids = [run_id]
            for child in self.live.store.child_runs(run_id):
                ids.extend(live_tree(child.run_id))
            return ids

        for run_id in await asyncio.to_thread(live_tree, root):
            async with self.live.condition(run_id):
                with suppress(WorkflowRunNotFoundError):
                    run = self.live.store.get_run(run_id)
                    if run.state in TERMINAL_RUN_STATES:
                        continue
                    for node in await asyncio.to_thread(self.live.store.unposted_questions, run_id):
                        with suppress(ScriptRuntimeUnavailableError, SupersededAttemptError):
                            await self.asked(run, node, run.epoch)

    @staticmethod
    def validate_question_options(options: object) -> None:
        """Validate question *options*, rejecting non-list, empty, or oversized option values."""
        if options is None:
            return
        if not (isinstance(options, list) and options and all(isinstance(o, str) and o.strip() for o in options)):
            msg = "options must be a list of strings, such as ['Yes', 'No']."
            raise WorkflowRunStoreError(msg)
        if len(options) > MAX_OPTIONS or any(len(o.encode()) > MAX_OPTION_BYTES for o in options):
            raise WorkflowRunStoreError(_TOO_MANY_OPTIONS)

    async def _wait_child_question(self, run_id: str, ask_key: str) -> str:
        condition = self.live.condition(run_id)
        while True:
            async with condition:
                cur = await asyncio.to_thread(self.live.store.get_node, run_id, ask_key)
                run = await asyncio.to_thread(self.live.store.get_run, run_id)
                ended = run_id in self.live.stopping or run.state in TERMINAL_RUN_STATES
                if cur is None or cur.state is WorkflowNodeState.CANCELLED or ended:
                    raise WorkflowRunStoreError(_NOT_ANSWERED)
                if cur.state is WorkflowNodeState.COMPLETED:
                    return str(cur.result)
                if "event_id" in cur.links:
                    await wait_for_change(condition)
                    continue
            posted = False
            with suppress(ScriptRuntimeUnavailableError, SupersededAttemptError):
                posted = "event_id" in (await self.asked(run, cur, run.epoch)).links
            if not posted:
                async with condition:
                    await wait_for_change(condition)

    async def ask_child_question(
        self,
        run_id: str,
        parent: WorkflowNode,
        question: str,
        options: list[str] | None,
    ) -> str:
        """Post (or reattach to) an agent's own question card, release its slot while waiting, and return the answer."""
        self.validate_question_options(options)
        ask_key = f"{parent.key}?{parent.generation}:{result_digest([question, options])[:12]}"
        req: dict[str, object] = {"question": question, "options": options}
        condition, cancelled = self.live.condition(run_id), False
        async with condition:
            run, saved = self.live.store.get_run(run_id), self.live.store.get_node(run_id, ask_key)
            if saved is not None and saved.state in (WorkflowNodeState.COMPLETED, WorkflowNodeState.WAITING):
                if saved.attempt != run.attempt or saved.stale:
                    await self.live.stamp(saved, run.epoch, ignore=True, attempt=run.attempt, stale=False)
                ask_node = saved
            elif run.state in TERMINAL_RUN_STATES or run_id in self.live.stopping:
                raise WorkflowRunStoreError(_NOT_ANSWERED)
            else:
                gen = 1 if saved is None else saved.generation + 1
                ask_node = WorkflowNode(
                    run_id=run_id,
                    key=ask_key,
                    kind="ask",
                    lane=parent.lane,
                    idx=parent.idx,
                    digest=node_digest("ask", req),
                    request=req,
                    generation=gen,
                    state=WorkflowNodeState.WAITING,
                    waiting_on="user",
                    attempt=run.attempt,
                    label=node_label("ask", req),
                    phase=parent.phase,
                    started_at=utc_timestamp(),
                )
                ask_node = await asyncio.to_thread(self.live.store.put_node, ask_node, run.epoch, dirty_after=False)
            if ask_node.state is WorkflowNodeState.COMPLETED:
                return str(ask_node.result)
            self.live.drop_slot(run_id, parent.key)
            await self.live.restate(run_id)
            condition.notify_all()
        runtime, job_id = self.live.agents(), parent.links.get("job_id")
        wait_job = (
            (runtime.jobs, job_id)
            if runtime is not None and isinstance(job_id, str) and runtime.jobs.retained_job(job_id)
            else None
        )
        try:
            if wait_job is None:
                return await self._wait_child_question(run_id, ask_key)
            jobs, jid = wait_job
            await jobs.save_adapter(jid, wait="user")
            async with jobs.human_decision_wait(jid):
                return await self._wait_child_question(run_id, ask_key)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                # Cleared only on normal/error exit (not CancelledError), so shutdown keeps wait="user" for recovery.
                if wait_job is not None:
                    await wait_job[0].save_adapter(wait_job[1], wait=None)
                await self.live.reacquire_place(run_id, parent.key)

    async def answer(self, run_id: str, key: str, generation: int, answer: str) -> bool:
        """Complete the question at *key* with a person's *answer*; false when that generation no longer waits.

        The answer is saved under the run's lock and epoch, as every write of the run is, also while no attempt
        runs: the next attempt reads it back and does not ask again.
        """
        async with (condition := self.live.condition(run_id)):
            run, node = self.live.store.get_run(run_id), self.live.store.get_node(run_id, key)
            waits = node is not None and (node.state, node.waiting_on) == (WorkflowNodeState.WAITING, "user")
            # An answer is an act of the run, to a card somebody was shown: both are checked as they are now.
            context = self._authorize(run)
            asked = waits and "event_id" in node.links and context is not None and not isinstance(context, str)
            if run.state in TERMINAL_RUN_STATES or node is None or node.generation != generation or not asked:
                return False
            if not await self.live.settle(node, run.epoch, state=WorkflowNodeState.COMPLETED, result=answer):
                return False
            await self.live.restate(run_id)
            condition.notify_all()
        # Without the lock, which no send to the room holds up for long.
        await self.close_card(run, node, "answered", answer, RETRY_SECONDS)
        return True

    async def peer_send(self, run_id: str, *, op_id: str, sender: NodeRef, to: str, text: str) -> dict[str, object]:
        """Send one peer message in a single transaction and wake any waiters of *run_id*."""
        send_call = partial(send_peer_message, self.live.store, run_id, op_id=op_id, sender=sender)
        result = await asyncio.to_thread(send_call, to=to, text=text)
        async with (condition := self.live.condition(run_id)):
            condition.notify_all()
        return result

    async def wait_for_messages(
        self,
        run_id: str,
        reader: NodeRef,
        wait_seconds: float,
        *,
        op_id: str,
        names: tuple[str, ...],
    ) -> dict[str, object]:
        """Read unread messages, or wait on *run_id*'s condition without holding a concurrency slot."""
        if isinstance(wait_seconds, bool) or not math.isfinite(wait_seconds) or wait_seconds < 0:
            msg = "wait_seconds must be a finite non-negative number."
            raise WorkflowRunStoreError(msg)
        read_fn = partial(read_call, self.live.store, run_id, op_id=op_id, reader=reader, names=names)
        condition, deadline, cancelled = self.live.condition(run_id), self.live.clock() + wait_seconds, False
        async with condition:
            if (found := await asyncio.to_thread(read_fn, record_empty=wait_seconds <= 0)) is not None:
                return found
            if self.live.run_ended(run_id):
                return {"messages": []}
            node = self.live.store.get_node(run_id, reader.key)
            same = node if node is not None and node.generation == reader.generation else None

            async def stamp_state(state: WorkflowNodeState, waiting_on: str | None = None) -> None:
                if same is None:
                    return
                with suppress(WorkflowRunNotFoundError):
                    if (cur := self.live.store.get_run(run_id)).state not in TERMINAL_RUN_STATES:
                        await self.live.stamp(same, cur.epoch, ignore=True, state=state, waiting_on=waiting_on)

            await stamp_state(WorkflowNodeState.WAITING, "peer")
            self.live.drop_slot(run_id, reader.key)
            await self.live.restate(run_id)
            condition.notify_all()
        try:
            async with condition:
                result = await self._await_peer_mail(condition, run_id, read_fn, wait_seconds, deadline)
                await stamp_state(WorkflowNodeState.QUEUED)
                await self.live.restate(run_id)
                condition.notify_all()
        except asyncio.CancelledError:
            cancelled = True
            raise
        else:
            return result
        finally:
            if not cancelled:
                await self.live.reacquire_place(run_id, reader.key)
                await stamp_state(WorkflowNodeState.RUNNING)
                await self.live.notify(run_id)

    async def _await_peer_mail(
        self,
        condition: asyncio.Condition,
        run_id: str,
        read_fn: Callable[..., dict[str, object] | None],
        wait_seconds: float,
        deadline: float,
    ) -> dict[str, object]:
        while True:
            if (found := await asyncio.to_thread(read_fn, record_empty=False)) is not None:
                return found
            if self.live.run_ended(run_id):
                return {"messages": []}
            if (remaining := deadline - self.live.clock()) <= 0:
                note = f"No message arrived within {wait_seconds:g} s."
                return cast("dict[str, object]", await asyncio.to_thread(read_fn, record_empty=True, empty_note=note))
            await wait_for_change(condition, remaining)

    async def wait_inbox(self, run: WorkflowRun, node: WorkflowNode, epoch: int, wait_seconds: float) -> None:
        """Wait for a script's `inbox()` message call and settle its node when mail arrives or the deadline expires."""
        reader = NodeRef("script", 1, "script")
        op_id = f"{node.key}:{node.generation}"
        read_fn = partial(read_call, self.live.store, run.run_id, op_id=op_id, reader=reader, names=("script",))
        condition, deadline = self.live.condition(run.run_id), self.live.clock() + wait_seconds
        async with condition:
            outcome = await self._await_peer_mail(condition, run.run_id, read_fn, wait_seconds, deadline)
        await self.live.settle(node, epoch, state=WorkflowNodeState.COMPLETED, result=outcome, ignore=True)
        await self.live.notify(run.run_id)
