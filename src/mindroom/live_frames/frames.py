"""The ``io.mindroom.live`` frame format: snapshots and deltas of one reply's live presentation."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

LIVE_EVENT_TYPE = "io.mindroom.live"
LIVE_CHECKPOINT_KEY = "io.mindroom.live_checkpoint"
FRAME_VERSION = 1
# Each tool preview a frame shows at most, so a long tool-heavy turn's trace fits; durable edits keep whole previews.
_FRAME_PREVIEW_CHARACTERS = 200
_PREVIEW_KEYS = ("args_preview", "result_preview")


@dataclass(frozen=True, slots=True)
class LiveDocs:
    """What one reply shows live: Markdown, reasoning, public tool-trace events, stream status, and worker activity.

    ``segments`` says where each reasoning segment begins in ``reasoning`` and how many tool calls preceded it.
    """

    md: str
    reasoning: str
    tools: tuple[dict[str, object], ...]
    status: str
    activity: str
    segments: tuple[dict[str, int], ...] = ()

    def to_json(self) -> dict[str, object]:
        """Return the snapshot form of these documents."""
        return {
            "md": self.md,
            "reasoning": self.reasoning,
            "tools": list(self.tools),
            "status": self.status,
            "activity": self.activity,
            "segments": [dict(segment) for segment in self.segments],
        }


def new_publisher_id() -> str:
    """Return a publisher id whose string order is its start order: zero-padded start milliseconds, then random hex."""
    return f"{time.time_ns() // 1_000_000:013d}-{secrets.token_hex(4)}"


def fitted_tools(tools: tuple[dict[str, object], ...], *, bare: int) -> tuple[dict[str, object], ...]:
    """Return the tool entries a frame shows: previews cut to the frame cap, and none on the oldest ``bare`` entries.

    An entry that lost any of its previews says ``clipped``, so a client can take them from the durable trace.
    """
    fitted: list[dict[str, object]] = []
    for index, entry in enumerate(tools):
        previews = {key: text for key in _PREVIEW_KEYS if isinstance(text := entry.get(key), str)}
        kept = {} if index < bare else {key: _clipped(text) for key, text in previews.items()}
        if kept == previews:
            fitted.append(entry)
            continue
        others = {key: value for key, value in entry.items() if key not in previews}
        fitted.append({**others, **kept, "clipped": True})
    return tuple(fitted)


def _clipped(text: str) -> str:
    if len(text) <= _FRAME_PREVIEW_CHARACTERS:
        return text
    return f"{text[: _FRAME_PREVIEW_CHARACTERS - 1]}…"


def utf16_length(text: str) -> int:
    """Return the length of ``text`` in UTF-16 code units, as clients count it."""
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
        "at": utf16_length(before[:prefix]),
        "del": utf16_length(before[prefix : len(before) - suffix]),
        "ins": after[prefix : len(after) - suffix],
        "len": utf16_length(after),
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
    if before.segments != after.segments:
        delta["segments"] = [dict(segment) for segment in after.segments]
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
