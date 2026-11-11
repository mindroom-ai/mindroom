"""Sub-agent runs as ``io.mindroom.subagents`` frames, sent by one publisher per sending account and room.

A run's frames have the live reply frame's structure, so one delta format serves both: the run's transcript is
projected into a reply's live documents, and ``meta`` says whose run it is and where it stands.
"""

from __future__ import annotations

import asyncio
import math
from bisect import bisect_left
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES
from mindroom.live_frames.frames import FRAME_VERSION, LiveDocs, live_delta, new_publisher_id
from mindroom.live_frames.transport import (
    RETRY_BACKOFF_SECONDS,
    LiveSendOutcome,
    frame_fits,
    homeserver_relays_live_frames,
    send_live_frame,
)
from mindroom.llm.events import ReportedCall
from mindroom.logging_config import get_logger
from mindroom.tool_system.events import complete_pending_tool_block, format_tool_started_event, serialize_tool_trace

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.delegation.activity import SubagentSnapshot
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

# The frame format and `publish` are this module's interface for the sub-agent activity feed's sink.
__all__ = ["SUBAGENT_EVENT_TYPE", "publish", "run_docs", "run_meta", "subagent_frame"]

SUBAGENT_EVENT_TYPE = "io.mindroom.subagents"

# At most 8 frames a second per account and room, which leaves at least 12 of the homeserver's 30 events a second per
# sender and room for the account's own reply frames at 10 a second.
_SEND_INTERVAL_SECONDS = 0.125
# Less often than a reply's snapshots, because a run's snapshots are bigger and share the room's frame buffer with
# reply frames. A snapshot this often lets a client that missed frames catch up and tells clients a working run is
# still live; a paused run, which can wait for hours, goes quiet instead.
_SNAPSHOT_INTERVAL_SECONDS = 10.0
_WORKING_STATUSES = frozenset({"running", "awaiting_approval"})

type _Frame = dict[str, object]


def run_docs(snapshot: SubagentSnapshot, *, previews: bool, text_cut: int = 0) -> LiveDocs:
    """Return a run's transcript as a reply's live documents, without its oldest ``text_cut`` characters of text.

    Text items are the Markdown; each tool item adds the marker line a reply shows for it, numbered by its entry in
    ``tools``, which carries previews only when ``previews``. The documents stream while the run works.
    """
    parts: list[str] = []
    entries: list[ToolTraceEntry] = []
    after_text = False
    for item in snapshot.items:
        if item.kind == "text":
            text = item.text[text_cut:]
            text_cut = max(0, text_cut - len(item.text))
            if text:
                # Text items meet only where a cut or the tool row cap removed what was between them.
                parts.append(f"\n\n{text}" if after_text else text)
                after_text = True
            continue
        assert item.tool is not None
        entries.append(item.tool)
        call = ReportedCall(tool_call_id=item.tool_call_id or "", tool_name=item.tool.tool_name, tool_args=None)
        marker, _ = format_tool_started_event(call, len(entries))
        if item.state != "running":
            marker = complete_pending_tool_block(marker, item.tool.tool_name, len(entries))
        parts.append(marker)
        after_text = False
    tools = serialize_tool_trace(entries)
    if not previews:
        tools = tuple({"type": entry["type"], "tool_name": entry["tool_name"]} for entry in tools)
    status = "streaming" if snapshot.status in _WORKING_STATUSES else "pending"
    return LiveDocs(md="".join(parts), reasoning="", tools=tools, status=status, activity="")


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
) -> _Frame:
    """Return one frame of a run's stream, carrying either a whole snapshot or a delta."""
    frame: _Frame = {"v": FRAME_VERSION, "run": run, "sid": sid, "seq": seq, "meta": meta}
    if snapshot is not None:
        frame["snapshot"] = snapshot.to_json()
    else:
        assert delta, "a delta frame carries at least one change"
        frame["delta"] = delta
    return frame


def _trimmed(before: SubagentSnapshot, after: SubagentSnapshot) -> bool:
    """Return whether the transcript caps removed items, or the start of the oldest text, between two snapshots."""
    oldest = next((item.text for item in before.items if item.kind == "text"), None)
    now_oldest = next((item.text for item in after.items if item.kind == "text"), "")
    return after.items_dropped != before.items_dropped or (oldest is not None and not now_oldest.startswith(oldest))


@dataclass
class _Stream:
    """One run's frames: its stream id, the highest sequence number used, its latest snapshot, and what landed."""

    sid: str
    snapshot: SubagentSnapshot
    previews: bool
    seq: int = -1
    # Whether the latest snapshot has not gone out yet.
    dirty: bool = True
    # The snapshot and documents of the last frame that landed; None makes the next frame a snapshot.
    landed: tuple[SubagentSnapshot, LiveDocs] | None = None
    snapshot_at: float = 0.0
    # How the last snapshot fitted the size limit: whether it kept the previews and how much of the oldest text it
    # cut. Deltas keep that fit, so they stay appends.
    fit: tuple[bool, int] = (True, 0)

    def keepalive_at(self) -> float:
        """Return when a working run owes clients its next snapshot; never for a paused or ended one."""
        return self.snapshot_at + _SNAPSHOT_INTERVAL_SECONDS if self.snapshot.status in _WORKING_STATUSES else math.inf

    async def send_next(self, client: nio.AsyncClient, room_id: str, now: float) -> LiveSendOutcome | None:
        """Send the latest snapshot as the next frame; return the outcome, None when nothing changed.

        The frame is a snapshot first, after a failed send, after the caps cut the transcript, when only meta changed,
        every 10 s, at the run's end, and when a delta would not fit; otherwise a delta from what landed last.
        """
        snapshot, landed = self.snapshot, self.landed
        self.dirty = False
        fits = partial(frame_fits, client, room_id, event_type=SUBAGENT_EVENT_TYPE)
        if (
            landed is None
            or snapshot.status in TERMINAL_CHILD_STATUSES
            or now - self.snapshot_at >= _SNAPSHOT_INTERVAL_SECONDS
            or _trimmed(landed[0], snapshot)
            or run_meta(landed[0], full=False) != run_meta(snapshot, full=False)
        ):
            frame, docs = self._fitted_snapshot(snapshot, fits)
        else:
            docs = run_docs(snapshot, previews=self.previews and self.fit[0], text_cut=self.fit[1])
            delta = live_delta(landed[1], docs)
            if not delta:
                return None
            frame = self._frame(snapshot, delta=delta)
            if not fits(frame):
                frame, docs = self._fitted_snapshot(snapshot, fits)
        self.seq += 1
        outcome = await send_live_frame(
            client,
            room_id,
            frame,
            transaction_id=f"subagents.{self.sid}.{self.seq}",
            event_type=SUBAGENT_EVENT_TYPE,
        )
        if outcome is LiveSendOutcome.SENT:
            self.landed = (snapshot, docs)
            if "snapshot" in frame:
                self.snapshot_at = now
        elif outcome is LiveSendOutcome.RETRY:
            self.landed = None
            self.dirty = True
        return outcome

    def _frame(
        self,
        snapshot: SubagentSnapshot,
        *,
        docs: LiveDocs | None = None,
        delta: _Frame | None = None,
    ) -> _Frame:
        run, meta = snapshot.delegation_id, run_meta(snapshot, full=docs is not None)
        return subagent_frame(run=run, sid=self.sid, seq=self.seq + 1, meta=meta, snapshot=docs, delta=delta)

    def _fitted_snapshot(self, snapshot: SubagentSnapshot, fits: Callable[[_Frame], bool]) -> tuple[_Frame, LiveDocs]:
        """Return a snapshot frame within the size limit: without previews if need be, then without the oldest text."""

        def built(previews: bool, text_cut: int) -> tuple[_Frame, LiveDocs]:
            self.fit = (previews, text_cut)
            docs = run_docs(snapshot, previews=previews, text_cut=text_cut)
            return self._frame(snapshot, docs=docs), docs

        frame, docs = built(self.previews, 0)
        if self.previews and not fits(frame):
            frame, docs = built(False, 0)
        if fits(frame):
            return frame, docs
        # The least cut of the oldest text that fits; the frame without any text always fits, as the caps bound the rest.
        text_length = sum(len(item.text) for item in snapshot.items if item.kind == "text")
        text_cut = bisect_left(range(text_length + 1), True, lo=1, key=lambda cut: fits(built(False, cut)[0]))
        return built(False, text_cut)


class _Publisher:
    """Send the frames of every sub-agent run one account shows in one room, within one budget.

    One send is in flight at a time and sends are at least 125 ms apart, serving the runs with something to send in
    turn. A run's stream ends after the frame of its end, or when the room refuses one of its frames; such a run sends
    nothing more until it resumes from a pause or ends. The publisher stops when no stream remains.
    """

    def __init__(
        self,
        key: tuple[str, str],
        *,
        client: Callable[[], nio.AsyncClient | None],
        homeserver_url: str,
    ) -> None:
        self._key = key
        self._room_id = key[1]
        self._client = client
        self._homeserver_url = homeserver_url
        self._streams: dict[str, _Stream] = {}
        # The latest status of each unfinished run whose frame the room refused.
        self._refused: dict[str, str] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def update(self, snapshot: SubagentSnapshot, *, previews: bool) -> None:
        """Queue a run's latest snapshot for its next frame, starting the publisher when it is idle."""
        run = snapshot.delegation_id
        refused = self._refused.get(run)
        if refused is not None:
            resumed = refused == "paused" and snapshot.status != "paused"
            if snapshot.status not in TERMINAL_CHILD_STATUSES and not resumed:
                self._refused[run] = snapshot.status
                return
            del self._refused[run]
        stream = self._streams.get(run)
        if stream is None:
            self._streams[run] = _Stream(sid=new_publisher_id(), snapshot=snapshot, previews=previews)
        else:
            stream.snapshot, stream.previews, stream.dirty = snapshot, previews, True
        self._wake.set()
        if self._task is None or self._task.done():
            # Outside any turn's context: the frames of every run in the room go out from here.
            self._task = create_background_task(self._run(), name=f"subagent_frames_{self._room_id}", context=Context())

    async def _run(self) -> None:
        try:
            if await homeserver_relays_live_frames(self._homeserver_url):
                await self._send_frames()
            else:
                self._refused.clear()
        except Exception:
            # Frames are best effort: a bug building or sending one ends the room's frames, never a run.
            logger.warning("subagent_frames_stopped_after_error", room_id=self._room_id, exc_info=True)
            self._refused.clear()
        finally:
            self._streams.clear()
            # A publisher that still holds refused runs stays, so their next updates are still ignored.
            if not self._refused and _PUBLISHERS.get(self._key) is self:
                del _PUBLISHERS[self._key]

    async def _send_frames(self) -> None:
        client_missing = False
        while self._streams:
            self._wake.clear()
            client = self._client()
            if client is None:
                if client_missing:
                    # The account had no client for a whole retry, as when its bot stopped: its frames end here.
                    self._refused.clear()
                    return
                client_missing = True
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                continue
            client_missing = False
            outcome = await self._send_next(client)
            if outcome is None:
                await self._wait()
            else:
                # A failed send waits longer, so a homeserver failing every send at once, as through a proxy while it
                # restarts, is not asked again every 125 ms.
                retried = outcome is LiveSendOutcome.RETRY
                await asyncio.sleep(RETRY_BACKOFF_SECONDS if retried else _SEND_INTERVAL_SECONDS)

    async def _send_next(self, client: nio.AsyncClient) -> LiveSendOutcome | None:
        """Send one frame of the first due stream with something new, which then waits behind the others."""
        now = monotonic()
        for run, stream in list(self._streams.items()):
            due = stream.dirty or now >= stream.keepalive_at()
            outcome = await stream.send_next(client, self._room_id, now) if due else None
            if outcome is None:
                continue
            del self._streams[run]
            ended = stream.snapshot.status in TERMINAL_CHILD_STATUSES
            if outcome is LiveSendOutcome.STOP and not ended:
                self._refused[run] = stream.snapshot.status
            elif stream.dirty or not ended:
                # A run that ended while its refused frame was in flight still gets its end's one attempt.
                self._streams[run] = stream
            return outcome
        return None

    async def _wait(self) -> None:
        """Wait for an update, or until a working run owes clients its next snapshot."""
        keepalive_at = min(stream.keepalive_at() for stream in self._streams.values())
        with suppress(TimeoutError):
            async with asyncio.timeout(None if keepalive_at == math.inf else max(0.0, keepalive_at - monotonic())):
                await self._wake.wait()


# The publisher of each sending account and room with runs to send.
_PUBLISHERS: dict[tuple[str, str], _Publisher] = {}


def publish(
    account: str,
    room_id: str,
    snapshot: SubagentSnapshot,
    *,
    previews: bool,
    client: Callable[[], nio.AsyncClient | None],
    homeserver_url: str,
) -> None:
    """Queue a run's latest snapshot as frames from ``account`` in ``room_id``, with tool previews only when ``previews``.

    ``client`` returns the account's live client at each send.
    """
    publisher = _PUBLISHERS.get((account, room_id))
    if publisher is None:
        publisher = _Publisher((account, room_id), client=client, homeserver_url=homeserver_url)
        _PUBLISHERS[(account, room_id)] = publisher
    publisher.update(snapshot, previews=previews)
