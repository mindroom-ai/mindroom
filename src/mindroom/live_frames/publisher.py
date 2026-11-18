"""One streaming reply's live frames: what it last sent, and the loop that sends the next one."""

from __future__ import annotations

import asyncio
import time
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.live_frames.frames import (
    LIVE_EVENT_TYPE,
    LiveDocs,
    fitted_tools,
    live_delta,
    live_frame,
    new_publisher_id,
)
from mindroom.live_frames.transport import RETRY_BACKOFF_SECONDS, LiveSendOutcome, frame_fits, send_live_frame
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

logger = get_logger(__name__)

_FRAME_INTERVAL_SECONDS = 0.1
# A snapshot this often lets a client that missed frames, or opened the room mid-reply, catch up, and tells clients
# the reply is still live while nothing changes.
_SNAPSHOT_INTERVAL_SECONDS = 5.0


@dataclass
class LivePublisher:
    """The frames one reply has sent: its publisher id, the highest sequence number used, and what it last showed."""

    client: nio.AsyncClient
    room_id: str
    publisher_id: str = field(default_factory=new_publisher_id)
    seq: int = -1
    # What the last frame that landed showed; None makes the next frame a snapshot.
    last_sent: LiveDocs | None = None
    last_snapshot_at: float = 0.0
    # False once frames stopped for this reply; durable edits then keep their ordinary cadence.
    running: bool = True
    # How many of the oldest tool entries frames show without previews, so snapshots fit the size limit. Only a
    # snapshot raises it, so the deltas between snapshots stay small.
    bare: int = 0
    # Whether frames leave the widgets out, the first thing a snapshot too big sheds. Only a snapshot decides it, so
    # the deltas between two snapshots do not bring back what the next one drops.
    sheds_ui: bool = False

    def checkpoint(self) -> dict[str, object] | None:
        """Return the watermark a durable write carries: every frame up to it is no newer than that write."""
        if self.seq < 0:
            return None
        return {"sid": self.publisher_id, "seq": self.seq}

    async def _send_next(self, reply_event_id: str, docs: LiveDocs) -> LiveSendOutcome | None:
        """Send one frame when the documents changed or a snapshot is due; return the outcome, None when none was due."""
        now = time.monotonic()
        snapshot_due = self.last_sent is None or now - self.last_snapshot_at >= _SNAPSHOT_INTERVAL_SECONDS
        shown = self._shown(docs, self.bare)
        if not snapshot_due and shown == self.last_sent:
            return None
        self.seq += 1
        frame = None
        if not snapshot_due and self.last_sent is not None:
            frame = live_frame(
                reply_event_id=reply_event_id,
                publisher_id=self.publisher_id,
                seq=self.seq,
                delta=live_delta(self.last_sent, shown),
            )
        if frame is None or not self._fits(frame):
            bare, sheds_ui = self.bare, self.sheds_ui
            snapshot, fitted = self._fitted_snapshot(reply_event_id, docs)
            if frame is None or self._fits(snapshot):
                snapshot_due, frame, shown = True, snapshot, fitted
            else:
                # The size check is an estimate; a delta it rejects may still go out, where no snapshot fits it.
                self.bare, self.sheds_ui = bare, sheds_ui
        outcome = await send_live_frame(
            self.client,
            self.room_id,
            frame,
            transaction_id=f"{self.publisher_id}.{self.seq}",
        )
        if outcome is LiveSendOutcome.SENT:
            self.last_sent = shown
            if snapshot_due:
                self.last_snapshot_at = now
        elif outcome is LiveSendOutcome.RETRY:
            self.last_sent = None
        else:
            self.running = False
        return outcome

    def _fits(self, frame: dict[str, object]) -> bool:
        return frame_fits(self.client, self.room_id, frame, event_type=LIVE_EVENT_TYPE)

    def _shown(self, docs: LiveDocs, bare: int) -> LiveDocs:
        """Return the documents a frame shows: no previews for the ``bare`` oldest tool entries, no widgets once shed."""
        return replace(docs, tools=fitted_tools(docs.tools, bare=bare), ui=None if self.sheds_ui else docs.ui)

    def _fitted_snapshot(self, reply_event_id: str, docs: LiveDocs) -> tuple[dict[str, object], LiveDocs]:
        """Return a snapshot frame within the size limit.

        It first leaves out the widgets, then the previews of the fewest more oldest tool entries.

        When even no previews do not fit, as when the Markdown alone is too big, the transport refuses the frame and
        frames stop.
        """

        def built(bare: int) -> tuple[dict[str, object], LiveDocs]:
            shown = self._shown(docs, bare)
            frame = live_frame(
                reply_event_id=reply_event_id,
                publisher_id=self.publisher_id,
                seq=self.seq,
                snapshot=shown,
            )
            return frame, shown

        count = len(docs.tools)
        self.bare = min(self.bare, count)
        # Tried with the widgets first, so a snapshot that has room for them again brings them back.
        self.sheds_ui = False
        self.sheds_ui = docs.ui is not None and not self._fits(built(self.bare)[0])
        if not self._fits(built(self.bare)[0]):
            fitting = bisect_left(range(count + 1), True, lo=self.bare + 1, key=lambda bare: self._fits(built(bare)[0]))
            self.bare = min(fitting, count)
        return built(self.bare)

    async def run(
        self,
        *,
        current: Callable[[], tuple[str, LiveDocs] | None],
        on_tick: Callable[[], None],
        on_stopped: Callable[[], None],
    ) -> None:
        """Send a frame every tick while the reply's live documents change, until frames stop or the task is cancelled.

        ``current`` returns the reply's event id and documents, or None while frames must wait; ``on_tick`` runs each
        tick so the caller can schedule durable checkpoints; ``on_stopped`` runs once if frames stop while the stream
        still runs, so its durable reply catches up at once.
        """
        while self.running:
            await asyncio.sleep(_FRAME_INTERVAL_SECONDS)
            try:
                on_tick()
                state = current()
                outcome = None if state is None else await self._send_next(*state)
            except Exception:
                # Frames are best effort: ending them restores the ordinary edit cadence instead of leaving
                # checkpoint-only edits with nothing scheduling the checkpoints.
                logger.warning("live_frames_stopped_after_error", room_id=self.room_id, exc_info=True)
                self.running = False
            if not self.running:
                on_stopped()
                return
            if outcome is LiveSendOutcome.RETRY:
                # A send that fails at once, as through a proxy while the homeserver restarts, is not repeated every
                # tick; the next frame is a snapshot.
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
