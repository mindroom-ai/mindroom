"""Sub-agent runs as ``io.mindroom.subagents`` frames, sent by one publisher per sending account and room.

A run's frames have the live reply frame's structure, so one delta format serves both: the run's transcript is
projected into a reply's live documents, and ``meta`` says whose run it is and where it stands.
"""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES
from mindroom.live_frames.frames import FRAME_VERSION, LiveDocs, live_delta, new_publisher_id
from mindroom.live_frames.publisher import RETRY_BACKOFF_SECONDS, SEND_TIMEOUT_SECONDS
from mindroom.live_frames.transport import LiveSendOutcome, homeserver_relays_live_frames, send_live_frame
from mindroom.llm.events import ReportedCall
from mindroom.logging_config import get_logger
from mindroom.tool_system.events import complete_pending_tool_block, format_tool_started_event, serialize_tool_trace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.delegation.activity import SubagentSnapshot
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

# The frame format and the publisher are this module's interface for the sub-agent activity feed's sink.
__all__ = ["SUBAGENT_EVENT_TYPE", "SubagentFramePublisher", "publisher_for", "run_docs", "run_meta", "subagent_frame"]

SUBAGENT_EVENT_TYPE = "io.mindroom.subagents"

# At most 8 frames a second per account and room, which leaves at least 12 of the homeserver's 30 events a second per
# sender and room for the account's own reply frames at 10 a second.
_SEND_INTERVAL_SECONDS = 0.125
# Less often than a reply's snapshots, because a run's snapshots are bigger and share the room's frame buffer with
# reply frames. A snapshot this often still lets a client that missed frames catch up, and tells clients a quiet run,
# such as one waiting for an approval, is still live.
_SNAPSHOT_INTERVAL_SECONDS = 10.0
_WORKING_STATUSES = frozenset({"running", "awaiting_approval"})

type _Send = Callable[[dict[str, object], str], Awaitable[LiveSendOutcome]]


def run_docs(snapshot: SubagentSnapshot, *, previews: bool) -> LiveDocs:
    """Return a run's transcript as a reply's live documents, with tool previews only when ``previews``.

    Text items are the Markdown; each tool item adds the marker line a reply shows for it, numbered by its entry in
    ``tools``. The documents stream while the run works or waits for an approval.
    """
    parts: list[str] = []
    entries: list[ToolTraceEntry] = []
    for index, item in enumerate(snapshot.items):
        if item.kind == "text":
            # Text items meet only where the tool row cap dropped the call between them.
            if index and snapshot.items[index - 1].kind == "text":
                parts.append("\n\n")
            parts.append(item.text)
            continue
        assert item.tool is not None
        entries.append(item.tool)
        call = ReportedCall(tool_call_id=item.tool_call_id or "", tool_name=item.tool.tool_name, tool_args=None)
        marker, _ = format_tool_started_event(call, len(entries))
        if item.state != "running":
            marker = complete_pending_tool_block(marker, item.tool.tool_name, len(entries))
        parts.append(marker)
    tools = serialize_tool_trace(entries)
    if not previews:
        tools = tuple({"type": entry["type"], "tool_name": entry["tool_name"]} for entry in tools)
    return LiveDocs(
        md="".join(parts),
        reasoning="",
        tools=tools,
        status="streaming" if snapshot.status in _WORKING_STATUSES else "pending",
        activity="",
    )


def run_meta(snapshot: SubagentSnapshot, *, full: bool) -> dict[str, object]:
    """Return what every frame says about its run; ``full``, for a snapshot, adds the task and an ended run's result."""
    meta: dict[str, object] = {
        "agent": snapshot.agent_name,
        "caller": snapshot.caller_agent_name,
        "root": snapshot.root_caller_agent_name,
        "parent": snapshot.parent_delegation_id,
        "thread_id": snapshot.thread_id,
        "source": snapshot.source,
        "status": snapshot.status,
        "started_at": snapshot.started_at,
        "ended_at": snapshot.ended_at,
    }
    if full:
        meta["task"] = snapshot.task
        if snapshot.status in TERMINAL_CHILD_STATUSES:
            meta["result"] = snapshot.result_preview
    return meta


def subagent_frame(
    *,
    run: str,
    sid: str,
    seq: int,
    meta: dict[str, object],
    snapshot: LiveDocs | None = None,
    delta: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return one frame of a run's stream, carrying a whole snapshot or a delta, which is empty when only meta changed."""
    frame: dict[str, object] = {"v": FRAME_VERSION, "run": run, "sid": sid, "seq": seq, "meta": meta}
    if snapshot is not None:
        frame["snapshot"] = snapshot.to_json()
    else:
        assert delta is not None, "a frame carries a snapshot or a delta"
        frame["delta"] = delta
    return frame


def _front_cut(before: SubagentSnapshot, after: SubagentSnapshot) -> bool:
    """Return whether the transcript caps removed items or the start of the oldest text between two snapshots."""
    if after.items_dropped != before.items_dropped:
        return True
    if not before.items or not after.items:
        return False
    oldest, now_oldest = before.items[0], after.items[0]
    return oldest.kind == "text" and (now_oldest.kind != "text" or not now_oldest.text.startswith(oldest.text))


@dataclass(frozen=True, slots=True)
class _Landed:
    """What the last frame of a run's stream that landed showed."""

    snapshot: SubagentSnapshot
    docs: LiveDocs
    meta: dict[str, object]


@dataclass
class _Stream:
    """One run's frames: its stream id, the highest sequence number used, its latest snapshot, and what landed."""

    sid: str
    snapshot: SubagentSnapshot
    previews: bool
    seq: int = -1
    # Whether the latest snapshot has not gone out yet.
    dirty: bool = True
    # None makes the next frame a snapshot.
    landed: _Landed | None = None
    snapshot_at: float = 0.0

    def due(self, now: float) -> bool:
        """Whether the stream has a newer snapshot to send or owes clients a snapshot."""
        return self.dirty or now - self.snapshot_at >= _SNAPSHOT_INTERVAL_SECONDS

    @property
    def ended(self) -> bool:
        """Whether the frame of the run's end landed."""
        return self.landed is not None and self.landed.snapshot.status in TERMINAL_CHILD_STATUSES

    async def send_next(self, send: _Send, now: float) -> LiveSendOutcome | None:
        """Send the latest snapshot as the next frame; return the outcome, None when there was nothing new to send.

        The frame is a snapshot first, after a failed send, after the caps cut the transcript's start, every 10 s, and
        at the run's end; otherwise a delta from what landed last.
        """
        self.dirty = False
        snapshot = self.snapshot
        docs = run_docs(snapshot, previews=self.previews)
        landed = self.landed
        whole = (
            landed is None
            or snapshot.status in TERMINAL_CHILD_STATUSES
            or now - self.snapshot_at >= _SNAPSHOT_INTERVAL_SECONDS
            or _front_cut(landed.snapshot, snapshot)
        )
        meta = run_meta(snapshot, full=whole)
        delta = None if whole or landed is None else live_delta(landed.docs, docs)
        if landed is not None and delta == {} and meta == landed.meta:
            return None
        self.seq += 1
        frame = subagent_frame(
            run=snapshot.delegation_id,
            sid=self.sid,
            seq=self.seq,
            meta=meta,
            snapshot=docs if whole else None,
            delta=delta,
        )
        outcome = await send(frame, f"subagents.{self.sid}.{self.seq}")
        if outcome is LiveSendOutcome.SENT:
            self.landed = _Landed(snapshot, docs, meta)
            if whole:
                self.snapshot_at = now
        elif outcome is LiveSendOutcome.RETRY:
            self.landed = None
            self.dirty = True
        return outcome


class SubagentFramePublisher:
    """Send the frames of every sub-agent run one account shows in one room, within one budget.

    One send is in flight at a time and sends are at least 125 ms apart, serving the runs with something to send in
    turn. A run's stream ends after the frame of its end; the publisher stops when no stream remains.
    """

    def __init__(
        self,
        account: str,
        room_id: str,
        *,
        client: Callable[[], nio.AsyncClient | None],
        homeserver_url: str,
    ) -> None:
        self._key = (account, room_id)
        self._room_id = room_id
        self._client = client
        self._homeserver_url = homeserver_url
        self._streams: dict[str, _Stream] = {}
        self._task: asyncio.Task[None] | None = None

    def update(self, snapshot: SubagentSnapshot, *, previews: bool) -> None:
        """Queue a run's latest snapshot for its next frame, starting the publisher when it is idle."""
        stream = self._streams.get(snapshot.delegation_id)
        if stream is None:
            self._streams[snapshot.delegation_id] = _Stream(
                sid=new_publisher_id(),
                snapshot=snapshot,
                previews=previews,
            )
        else:
            stream.snapshot = snapshot
            stream.previews = previews
            stream.dirty = True
        if self._task is None or self._task.done():
            self._task = create_background_task(
                self._run(),
                name=f"subagent_frames_{self._room_id}",
                # Outside any turn's context: the frames of every run in the room go out from here.
                context=Context(),
            )

    async def _run(self) -> None:
        try:
            if not await homeserver_relays_live_frames(self._homeserver_url):
                return
            while self._streams:
                outcome = await self._send_next()
                # A failed send waits longer, so a homeserver failing every send at once, as through a proxy while it
                # restarts, is not asked again every 125 ms.
                await asyncio.sleep(
                    RETRY_BACKOFF_SECONDS if outcome is LiveSendOutcome.RETRY else _SEND_INTERVAL_SECONDS,
                )
        except Exception:
            # Frames are best effort: a bug building or sending one ends the room's frames, never a run.
            logger.warning("subagent_frames_stopped_after_error", room_id=self._room_id, exc_info=True)
        finally:
            self._streams.clear()
            if _PUBLISHERS.get(self._key) is self:
                del _PUBLISHERS[self._key]

    async def _send_next(self) -> LiveSendOutcome | None:
        """Send one frame of the first due stream with something new, which then waits behind the others."""
        now = monotonic()
        for run, stream in list(self._streams.items()):
            if not stream.due(now):
                continue
            outcome = await stream.send_next(self._send, now)
            if outcome is None:
                continue
            del self._streams[run]
            # A refused frame ends its run's stream; a later update starts a new one with a snapshot.
            if outcome is not LiveSendOutcome.STOP and not stream.ended:
                self._streams[run] = stream
            return outcome
        return None

    async def _send(self, frame: dict[str, object], transaction_id: str) -> LiveSendOutcome:
        client = self._client()
        if client is None:
            # The account is between clients, as while it signs in again; its next frame is a snapshot.
            return LiveSendOutcome.RETRY
        try:
            return await asyncio.wait_for(
                send_live_frame(
                    client,
                    self._room_id,
                    frame,
                    transaction_id=transaction_id,
                    event_type=SUBAGENT_EVENT_TYPE,
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return LiveSendOutcome.RETRY
        except Exception:
            logger.warning("subagent_frame_send_failed", room_id=self._room_id, run=frame["run"], exc_info=True)
            return LiveSendOutcome.STOP


# The publisher of each sending account and room with runs to send.
_PUBLISHERS: dict[tuple[str, str], SubagentFramePublisher] = {}


def publisher_for(
    account: str,
    room_id: str,
    *,
    client: Callable[[], nio.AsyncClient | None],
    homeserver_url: str,
) -> SubagentFramePublisher:
    """Return the publisher of ``account``'s frames in ``room_id``; ``client`` returns the account's live client.

    A publisher whose streams all ended is forgotten, so look it up for each update instead of keeping it.
    """
    publisher = _PUBLISHERS.get((account, room_id))
    if publisher is None:
        publisher = SubagentFramePublisher(account, room_id, client=client, homeserver_url=homeserver_url)
        _PUBLISHERS[(account, room_id)] = publisher
    return publisher
