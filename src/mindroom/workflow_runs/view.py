"""What people see of a workflow run: its card in the run's thread and its live graph frames.

The engine knows nothing of this. Every write of a run names the run here, and its view is built again from what is
saved: the frames carry the live numbers, the card follows the run's status.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.live_frames.workflow import publish_run, stop_run
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import WORKFLOW_RUN_CONTENT_KEY
from mindroom.redaction import redact_sensitive_text
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.faults import capture
from mindroom.workflow_runs.graph import LOG_LINES, Graph, fit_snapshot, graph_meta, graph_rows, projected_cost_usd
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    UNSETTLED_NODE_STATES,
    WorkflowNodeState,
    WorkflowRunState,
)
from mindroom.workflow_runs.notices import notice, notice_fits, send_notice
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun

logger = get_logger(__name__)

# How often a card is edited at most while only the run's numbers change; the frames carry those.
_CARD_SECONDS = 600.0
# How often the view of a live run is built again though nothing was written, for what its running agents used.
_REFRESH_SECONDS = 10.0
# How long writes are collected before a view is built: a burst of them is one view.
_SETTLE_SECONDS = 0.25
_LARGE_RUN_AGENTS = 25
_LARGE = "Large run: {agents} agents, about ${projected:.2f} projected."
_AGENT_KINDS = frozenset({"agent", "turn"})
_ERROR_CHARS = 200

type _Built = tuple[WorkflowRun, list[WorkflowNode], dict[str, Any], Graph]


def _unasked(node: WorkflowNode) -> bool:
    """Return whether *node* is a question that waits and whose card could not be posted yet."""
    return node.kind == "ask" and node.state is WorkflowNodeState.WAITING and "event_id" not in node.links


def card_content(run: WorkflowRun, meta: dict[str, Any], phases: list[str]) -> dict[str, Any]:
    """Return what a run's card says: who and what the run is, where it stands, its counts and what it used.

    A run that ended without completing says why in one line, the last of its error: a traceback names its error
    there.
    """
    error = None
    if run.state in TERMINAL_RUN_STATES and run.state is not WorkflowRunState.COMPLETED and run.error:
        error = redact_sensitive_text(run.error.strip().rpartition("\n")[2], max_length=_ERROR_CHARS)
    return {
        "run": run.run_id,
        "job_id": run.job_id,
        "requester": run.owner_user_id,
        **{name: meta[name] for name in ("title", "status", "counts", "usage")},
        "phases": phases,
        **{name: meta[name] for name in ("attempt", "started_at", "ended_at")},
        "error": error,
        "large": meta["large"],
    }


def card_text(content: dict[str, Any]) -> str:
    """Return the card as plain text, for a client that does not draw it."""
    counts = content["counts"]
    title = f'Workflow "{content["title"]}"' if content["title"] else "Workflow"
    numbers = f"{counts['completed']} done, {counts['running']} running, {counts['waiting']} waiting"
    stop = content["job_id"] and content["status"] not in TERMINAL_RUN_STATES
    return f"{title} {content['status']}: {numbers}." + (
        f" Stop it with !cancel_job {content['job_id']}." if stop else ""
    )


def _fitted_card(
    run: WorkflowRun,
    meta: dict[str, Any],
    graph: Graph,
    fits: Callable[[dict[str, Any]], bool],
    *,
    final: bool,
) -> dict[str, Any]:
    """Return the card's content within what one edit event holds; the card of an ended run carries its graph."""
    content = card_content(run, meta, graph.phases)
    if not final:
        return content if fits(content) else {**content, "phases": []}

    def whole(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {**content, "phases": snapshot["phases"], "graph": snapshot}

    return whole(fit_snapshot(graph, fits=lambda snapshot: fits(whole(snapshot))))


@dataclass
class RunViews:
    """Show the runs of one engine: `bot` names the account and the client that posts for a run, None while it cannot.

    `client` returns the client an account has now, None while its bot has none.
    """

    engine: WorkflowEngine
    bot: Callable[[WorkflowRun], tuple[str, nio.AsyncClient] | None]
    client: Callable[[str], nio.AsyncClient | None]
    homeserver_url: str
    live_frames: Callable[[], bool] = lambda: True
    clock: Callable[[], float] = time.monotonic
    # What the card of each run that has not ended last said: what an edit follows at once, a digest of its content,
    # and when it was sent.
    _cards: dict[str, tuple[tuple[object, ...], str, float]] = field(default_factory=dict)
    _streams: dict[str, tuple[str, str]] = field(default_factory=dict)
    _dirty: set[str] = field(default_factory=set)
    # When the view of each run is built next though nothing was written: for the numbers of a run that has not
    # ended, and for the card of an ended one that could not be sent, each time after twice as long.
    _due: dict[str, float] = field(default_factory=dict)
    _unsent: dict[str, int] = field(default_factory=dict)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Follow the store's writes from now on, and show each run whose card does not say how it ended."""
        self._loop = asyncio.get_running_loop()
        self.engine.store.on_change = self._changed
        for run_id in self.engine.store.unshown_runs():
            self._mark(run_id)
        self._task = asyncio.create_task(self._serve(), name="workflow_views")

    async def close(self) -> None:
        """Stop following the store; what was shown stays as it is."""
        self.engine.store.on_change = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _changed(self, run_id: str) -> None:
        """Note that a run changed, on whichever thread wrote it."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._mark, run_id)

    def _mark(self, run_id: str) -> None:
        self._dirty.add(run_id)
        self._wake.set()

    async def _serve(self) -> None:
        while True:
            with suppress(TimeoutError):
                next_due = min(self._due.values(), default=None)
                async with asyncio.timeout(None if next_due is None else max(0.0, next_due - time.monotonic())):
                    await self._wake.wait()
            await asyncio.sleep(_SETTLE_SECONDS)
            self._wake.clear()
            now = time.monotonic()
            due = self._dirty | {run_id for run_id, at in self._due.items() if at <= now}
            self._dirty = set()
            for run_id in due:
                try:
                    await self.show(run_id)
                except Exception:
                    # A view is best effort: a bug in building or sending one never reaches the run.
                    logger.exception("workflow_view_failed", run_id=run_id)

    async def show(self, run_id: str) -> None:
        """Build the view of a run from what is saved, queue it as the run's next frame, and post or edit its card.

        A run inside a run is shown in the run around it, and a run that never got an attempt nowhere.
        """
        built = await asyncio.to_thread(self._build, run_id)
        if built is None:
            if await self._ask_pending(run_id):
                self._due[run_id] = time.monotonic() + _REFRESH_SECONDS
            else:
                self._forget(run_id)
            return
        run, nodes, usage, graph = built
        agents = sum(node.kind in _AGENT_KINDS for node in nodes)
        access = self.engine.authorize(run)
        notice = (access[1] if isinstance(access, tuple) else WorkflowLimits()).large_run_notice_usd
        large = run.large or agents > _LARGE_RUN_AGENTS or 0 < notice < usage["projected_cost_usd"]
        if large and not run.large:
            text = _LARGE.format(agents=agents, projected=usage["projected_cost_usd"])
            await asyncio.to_thread(self._mark_large, run, text)
        meta = graph_meta(run, nodes, usage, large=large)
        shown = False
        if isinstance(access, tuple) and (bot := self.bot(run)) is not None:
            # Else the run may not act now, or nobody can post for it: its next view tries again.
            account, client = bot
            self._publish_frame(run, account, meta, graph)
            shown = await self._card(run, meta, graph, client)
            await self._ask_pending(run_id)
        else:
            self._stop_frame(run_id)
        if run.state not in TERMINAL_RUN_STATES:
            self._due[run_id] = time.monotonic() + _REFRESH_SECONDS
        elif shown:
            self._forget(run_id)
        else:
            # Nothing writes to an ended run again, so its card is sent again from here until it says the end.
            tries = self._unsent[run_id] = self._unsent.get(run_id, 0) + 1
            self._due[run_id] = time.monotonic() + min(_REFRESH_SECONDS * 2 ** min(tries, 10), _CARD_SECONDS)

    async def _ask_pending(self, run_id: str) -> bool:
        """Retry unposted question cards for *run_id* and its descendants; return whether any remain unposted."""
        pending = await asyncio.to_thread(self._unasked_runs, run_id)
        if not pending:
            return False
        for waiting_id in pending:
            await self.engine.ask_again(waiting_id)
        return bool(await asyncio.to_thread(self._unasked_runs, run_id))

    def _unasked_runs(self, run_id: str) -> list[str]:
        """Return every live run in *run_id*'s tree that has a waiting question with no card yet."""
        store = self.engine.store
        try:
            run = store.get_run(run_id)
        except WorkflowRunNotFoundError:
            return []
        if run.state in TERMINAL_RUN_STATES:
            return []
        pending = [run_id] if any(_unasked(node) for node in store.nodes(run_id, light=True)) else []
        for child in store.child_runs(run_id):
            pending.extend(self._unasked_runs(child.run_id))
        return pending

    def _publish_frame(self, run: WorkflowRun, account: str, meta: dict[str, Any], graph: Graph) -> None:
        """Queue *run*'s frame on *account*, or stop any open stream if live frames are off."""
        if not self.live_frames():
            self._stop_frame(run.run_id)
            return
        place = (account, run.room_id)
        if (old := self._streams.get(run.run_id)) is not None and old != place:
            stop_run(*old, run.run_id)
        # By the account alone, as its sub-agent frames are sent: they share one publisher, which keeps the
        # first lookup it gets, so a run that can no longer post must not end the account's other frames.
        live, url = partial(self.client, account), self.homeserver_url
        publish_run(
            account,
            run.room_id,
            run.run_id,
            meta,
            graph,
            client=live,
            homeserver_url=url,
            allowed=lambda: isinstance(self.engine.authorize(run), tuple),
        )
        self._streams[run.run_id] = place

    def _stop_frame(self, run_id: str) -> None:
        """Close *run_id*'s open frame stream when it may no longer publish."""
        if (place := self._streams.pop(run_id, None)) is not None:
            stop_run(*place, run_id)

    def _forget(self, run_id: str) -> None:
        """Keep nothing of a run whose card says how it ended, or that is gone."""
        for kept in (self._due, self._unsent, self._cards, self._streams):
            kept.pop(run_id, None)

    def _mark_large(self, run: WorkflowRun, text: str) -> None:
        """Mark the run as large, once, and say so in its log; a run that moved on is marked by its next view."""
        store = self.engine.store
        with suppress(SupersededAttemptError):
            store.update_run(run.run_id, run.epoch, when=frozenset(WorkflowRunState), large=True)
            store.append_log(run.run_id, run.epoch, text)

    def _build(self, run_id: str) -> _Built | None:
        """Read what a view of the run shows: the run, its calls without their requests, its usage and its rows."""
        store = self.engine.store
        try:
            run = store.get_run(run_id)
        except WorkflowRunNotFoundError:
            return None
        if run.parent_run_id is not None:
            self._changed(run.parent_run_id)
            return None
        if run.attempt == 0:
            return None
        nodes = store.nodes(run_id, light=True)
        used = store.node_usage(run_id)
        usage = self.engine.run_usage(run_id)
        open_agents = sum(node.kind in _AGENT_KINDS and node.state in UNSETTLED_NODE_STATES for node in nodes)
        usage["projected_cost_usd"] = projected_cost_usd(store.turn_costs(run_id), open_agents, usage["cost_usd"])
        inside = {
            f"{child.parent_node_key}/{child.run_id}": Counter(
                node.state.value for node in store.nodes(child.run_id, light=True) if node.kind != "group"
            )
            for child in store.child_runs(run_id)
        }
        graph = graph_rows(run, nodes, used, [], store.log(run_id, LOG_LINES), inside=inside)
        return run, nodes, usage, graph

    async def _card(self, run: WorkflowRun, meta: dict[str, Any], graph: Graph, client: nio.AsyncClient) -> bool:
        """Post the run's card, once, or edit it; return whether the card now says what there is to say.

        The card is posted under a transaction ID of the run, so a primary that stopped before it saved the card's
        event sends again and gets the same event. It is edited at once when the status, the phases or the waiting
        changed, and every ten minutes at most while only numbers change. The end of a run is always an edit,
        which carries the graph, fitted so that the whole edit is one event: the event a post returned may be an
        earlier post's. The run remembers that its card says the end, so the edit is sent until it landed.
        """
        final = run.state in TERMINAL_RUN_STATES
        if final and run.card_epoch == run.epoch:
            return True

        def message(content: dict[str, Any]) -> dict[str, Any]:
            return notice(run, card_text(content), WORKFLOW_RUN_CONTENT_KEY, content)

        def fits(content: dict[str, Any]) -> bool:
            return notice_fits(client, run, message(content), run.card_event_id)

        content = await asyncio.to_thread(partial(_fitted_card, run, meta, graph, fits, final=final))
        follows = (meta["status"], tuple(content["phases"]), meta["counts"]["waiting"] > 0, meta["large"])
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        shown, now, whole = self._cards.get(run.run_id), self.clock(), message(content)
        save = partial(self.engine.store.update_run, run.run_id, run.epoch, when=frozenset(WorkflowRunState))
        # A send that fails is not tried again here: the run's next view is.
        send = partial(send_notice, client, run, whole, retries=())
        event_id = run.card_event_id
        if event_id is None:
            event_id = await send(transaction_id=f"wfcard-{run.run_id}")
            if event_id is None:
                return False
            with suppress(SupersededAttemptError):
                await asyncio.to_thread(partial(save, card_event_id=event_id))
        elif not final and shown is not None and shown[0] == follows:
            if shown[1] == digest or now - shown[2] < _CARD_SECONDS:
                return True
        if (final or run.card_event_id is not None) and await send(event_id=event_id) is None:
            return False
        capture("workflow_cards", whole[WORKFLOW_RUN_CONTENT_KEY])
        self._cards[run.run_id] = (follows, digest, now)
        if final:
            with suppress(SupersededAttemptError):
                await asyncio.to_thread(partial(save, card_epoch=run.epoch))
        return True
