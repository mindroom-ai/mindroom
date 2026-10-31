"""One streaming reply's live frames: what it last sent, and the loop that sends the next one."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.live_frames.frames import LiveDocs, live_delta, live_frame, new_publisher_id
from mindroom.live_frames.transport import LiveSendOutcome, send_live_frame
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

logger = get_logger(__name__)

_FRAME_INTERVAL_SECONDS = 0.1
# A snapshot this often lets a client that missed frames, or opened the room mid-reply, catch up, and tells clients
# the reply is still live while nothing changes.
_SNAPSHOT_INTERVAL_SECONDS = 5.0
_SEND_TIMEOUT_SECONDS = 5.0
_RETRY_BACKOFF_SECONDS = 1.0


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

    def checkpoint(self) -> dict[str, object] | None:
        """Return the watermark a durable write carries: every frame up to it is no newer than that write."""
        if self.seq < 0:
            return None
        return {"sid": self.publisher_id, "seq": self.seq}

    async def _send_next(self, reply_event_id: str, docs: LiveDocs) -> LiveSendOutcome | None:
        """Send one frame when the documents changed or a snapshot is due; return the outcome, None when none was due."""
        now = time.monotonic()
        snapshot_due = self.last_sent is None or now - self.last_snapshot_at >= _SNAPSHOT_INTERVAL_SECONDS
        if not snapshot_due and docs == self.last_sent:
            return None
        self.seq += 1
        frame = live_frame(
            reply_event_id=reply_event_id,
            publisher_id=self.publisher_id,
            seq=self.seq,
            snapshot=docs if snapshot_due else None,
            delta=None if snapshot_due or self.last_sent is None else live_delta(self.last_sent, docs),
        )
        try:
            outcome = await asyncio.wait_for(
                send_live_frame(self.client, self.room_id, frame, transaction_id=f"{self.publisher_id}.{self.seq}"),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            outcome = LiveSendOutcome.RETRY
        except Exception:
            # Frames are best effort: a send that raises ends them for this reply instead of failing the reply.
            logger.warning("live_frame_send_failed", room_id=self.room_id, event_id=reply_event_id, exc_info=True)
            outcome = LiveSendOutcome.STOP
        if outcome is LiveSendOutcome.SENT:
            self.last_sent = docs
            if snapshot_due:
                self.last_snapshot_at = now
        elif outcome is LiveSendOutcome.RETRY:
            self.last_sent = None
        else:
            self.running = False
        return outcome

    async def run(
        self,
        *,
        current: Callable[[], tuple[str, LiveDocs] | None],
        on_tick: Callable[[], None],
        on_too_large: Callable[[], None],
    ) -> None:
        """Send a frame every tick while the reply's live documents change, until frames stop or the task is cancelled.

        ``current`` returns the reply's event id and documents, or None while frames must wait; ``on_tick`` runs each
        tick so the caller can schedule durable checkpoints; ``on_too_large`` runs once if frames stop because one is
        too big.
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
                return
            if outcome is LiveSendOutcome.TOO_LARGE:
                on_too_large()
            elif outcome is LiveSendOutcome.RETRY:
                # A send that fails at once, as through a proxy while the homeserver restarts, is not repeated every
                # tick; the next frame is a snapshot.
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
