"""The ``io.mindroom.live`` frame format: snapshots and deltas of one reply's live presentation."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

LIVE_EVENT_TYPE = "io.mindroom.live"
LIVE_CHECKPOINT_KEY = "io.mindroom.live_checkpoint"
FRAME_VERSION = 1


@dataclass(frozen=True, slots=True)
class LiveDocs:
    """What one reply shows live: Markdown, reasoning, public tool-trace events, stream status, and worker activity."""

    md: str
    reasoning: str
    tools: tuple[dict[str, object], ...]
    status: str
    activity: str

    def to_json(self) -> dict[str, object]:
        """Return the snapshot form of these documents."""
        return {
            "md": self.md,
            "reasoning": self.reasoning,
            "tools": list(self.tools),
            "status": self.status,
            "activity": self.activity,
        }


def new_publisher_id() -> str:
    """Return a publisher id whose string order is its start order: zero-padded start milliseconds, then random hex."""
    return f"{time.time_ns() // 1_000_000:013d}-{secrets.token_hex(4)}"


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _utf16_splice(before: str, after: str) -> dict[str, object] | None:
    """Return the one splice turning ``before`` into ``after``, in UTF-16 code units, or None when they are equal.

    The common prefix is taken first and the common suffix only from what remains, both over code points, so a splice
    never splits a surrogate pair.
    """
    if before == after:
        return None
    limit = min(len(before), len(after))
    prefix = 0
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    return {
        "at": _utf16_length(before[:prefix]),
        "del": _utf16_length(before[prefix : len(before) - suffix]),
        "ins": after[prefix : len(after) - suffix],
        "len": _utf16_length(after),
    }


def live_delta(before: LiveDocs, after: LiveDocs) -> dict[str, object]:
    """Return the delta fields that turn ``before`` into ``after``; empty when nothing changed."""
    delta: dict[str, object] = {}
    for name, old, new in (("md", before.md, after.md), ("reasoning", before.reasoning, after.reasoning)):
        splice = _utf16_splice(old, new)
        if splice is not None:
            delta[name] = splice
    changed = [
        [index, entry]
        for index, entry in enumerate(after.tools)
        if index >= len(before.tools) or before.tools[index] != entry
    ]
    if changed or len(before.tools) != len(after.tools):
        delta["tools"] = {"len": len(after.tools), "set": changed}
    if before.status != after.status:
        delta["status"] = after.status
    if before.activity != after.activity:
        delta["activity"] = after.activity
    return delta


def live_frame(
    *,
    reply_event_id: str,
    publisher_id: str,
    seq: int,
    snapshot: LiveDocs | None = None,
    delta: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return one frame carrying either a whole snapshot or a delta."""
    frame: dict[str, object] = {"v": FRAME_VERSION, "reply": reply_event_id, "sid": publisher_id, "seq": seq}
    if snapshot is not None:
        frame["snapshot"] = snapshot.to_json()
    else:
        assert delta, "a delta frame carries at least one change"
        frame["delta"] = delta
    return frame
