"""The graph of a workflow run as ``io.mindroom.workflow`` frames, sent by the publisher of the run's account and room.

A frame carries the run's meta and either a whole snapshot of the graph, fitted to the frame size, or what changed
since the frame before it. A run's live text and tool calls are not here: its rows name the sub-agent runs whose
``io.mindroom.subagents`` frames show them.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.live_frames.frames import new_publisher_id
from mindroom.live_frames.subagents import RunFrameStream, publish_stream, stop_stream
from mindroom.live_frames.transport import LiveSendOutcome, frame_fits
from mindroom.matrix.event_types import WORKFLOW_FRAME_EVENT_TYPE
from mindroom.workflow_runs.faults import capture
from mindroom.workflow_runs.graph import REPLACE, Graph, fit_snapshot, graph_delta
from mindroom.workflow_runs.models import TERMINAL_RUN_STATES

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

# A snapshot this often lets a client that missed frames, or opened the room during the run, catch up.
_SNAPSHOT_INTERVAL_SECONDS = 10.0


@dataclass(kw_only=True)
class _RunStream(RunFrameStream[tuple[dict[str, Any], dict[str, Any]]]):
    """One workflow run's frames: its latest meta and graph view plus permission check."""

    account: str
    run: str
    meta: dict[str, Any]
    graph: Graph
    allowed: Callable[[], bool] = lambda: True

    @property
    def status(self) -> str:
        """The latest status of the run."""
        return str(self.meta["status"])

    @property
    def ended(self) -> bool:
        """Whether the run has ended."""
        return self.status in TERMINAL_RUN_STATES

    def keepalive_at(self) -> float:
        """Return when a run that has not ended owes clients its next snapshot."""
        return math.inf if self.ended else self.snapshot_at + _SNAPSHOT_INTERVAL_SECONDS

    async def send_next(self, client: nio.AsyncClient, room_id: str, now: float) -> LiveSendOutcome | None:
        """Send the latest view as the next frame; return the outcome, None when nothing changed.

        The frame is a snapshot first, after a failed send, every 10 s, at the run's end, when a row left or the
        counts of what a snapshot leaves out changed, and when a delta would not fit; otherwise a delta from what
        landed last. Every frame is checked against the frame size as it goes out to the room before it is sent.
        """
        if not self.allowed():
            stop_stream(self.account, room_id, self.run)
            return None
        meta, graph, landed = self.meta, self.graph, self.landed
        self.dirty = False
        fits = partial(frame_fits, client, room_id, event_type=WORKFLOW_FRAME_EVENT_TYPE)
        # Off the loop: fitting the view of a large run serializes it several times.
        snapshot = await asyncio.to_thread(
            fit_snapshot,
            graph,
            fits=lambda candidate: fits(self.envelope(self.run, meta, snapshot=candidate)),
        )
        frame = self.envelope(self.run, meta, snapshot=snapshot)
        delta = None if landed is None else graph_delta(landed[1], snapshot)
        if landed is not None and delta != REPLACE and not self.ended and now < self.keepalive_at():
            if delta is None and landed[0] == meta:
                return None
            changed = self.envelope(self.run, meta, delta=delta or {})
            frame = changed if fits(changed) else frame
        outcome = await self.send_frame(
            client,
            room_id,
            frame,
            now,
            txn_prefix="workflow",
            event_type=WORKFLOW_FRAME_EVENT_TYPE,
            view=(meta, snapshot),
        )
        capture("workflow_frames", {"outcome": outcome.value, "frame": frame})
        return outcome


def publish_run(
    account: str,
    room_id: str,
    run_id: str,
    meta: dict[str, Any],
    graph: Graph,
    *,
    client: Callable[[], nio.AsyncClient | None],
    homeserver_url: str,
    allowed: Callable[[], bool] = lambda: True,
) -> None:
    """Queue a run's latest view as frames from ``account`` in ``room_id``, paced with the account's other frames there.

    A run that ended gets one last snapshot, after which its stream is gone.
    """

    def put(stream: _RunStream | None) -> _RunStream:
        if stream is None:
            return _RunStream(
                account=account,
                run=run_id,
                sid=new_publisher_id(),
                meta=meta,
                graph=graph,
                allowed=allowed,
            )
        stream.meta, stream.graph, stream.allowed, stream.dirty = meta, graph, allowed, True
        return stream

    ended = meta["status"] in TERMINAL_RUN_STATES
    publish_stream(
        account,
        room_id,
        run_id,
        str(meta["status"]),
        ended=ended,
        put=put,
        client=client,
        homeserver_url=homeserver_url,
    )
