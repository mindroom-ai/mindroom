"""The ``io.mindroom.live`` frame format: snapshots and deltas of one reply's live presentation."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache

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
    ``compactions`` holds the reply's automatic compaction entries, in start order.
    """

    md: str
    reasoning: str
    tools: tuple[dict[str, object], ...]
    status: str
    activity: str
    segments: tuple[dict[str, int], ...] = ()
    compactions: tuple[dict[str, object], ...] = ()

    def to_json(self) -> dict[str, object]:
        """Return the snapshot form of these documents."""
        return {
            "md": self.md,
            "reasoning": self.reasoning,
            "tools": list(self.tools),
            "status": self.status,
            "activity": self.activity,
            "segments": [dict(segment) for segment in self.segments],
            "compactions": [dict(entry) for entry in self.compactions],
        }


def new_publisher_id() -> str:
    """Return a publisher id whose string order is its start order: zero-padded start milliseconds, then random hex."""
    return f"{time.time_ns() // 1_000_000:013d}-{secrets.token_hex(4)}"


@dataclass(slots=True)
class _FittedSlot:
    source: dict[str, object]
    cut: dict[str, object]
    bare: dict[str, object] | None = None
    bare_savings: int = 0


def _bare_form(slot: _FittedSlot) -> tuple[dict[str, object], int]:
    if slot.bare is None:
        stripped = _fitted_entry(slot.source, bare=True)
        savings = len(json.dumps(slot.cut, separators=(",", ":"))) - len(json.dumps(stripped, separators=(",", ":")))
        slot.bare, slot.bare_savings = (stripped, savings) if savings > 0 else (slot.cut, 0)
    return slot.bare, slot.bare_savings


class ToolFitCache:
    """Per-publisher cache of cut and bare tool entries synced by entry identity."""

    def __init__(self) -> None:
        self._slots: list[_FittedSlot] = []
        self._fitted: list[dict[str, object]] = []
        self._last_raw: tuple[dict[str, object], ...] | None = None
        self._last_bare = 0
        self._last_result: tuple[dict[str, object], ...] = ()

    def fit(self, tools: tuple[dict[str, object], ...], *, bare: int) -> tuple[dict[str, object], ...]:
        """Return fitted tool entries for ``tools`` with the oldest ``bare`` entries stripped."""
        if tools is self._last_raw and bare == self._last_bare:
            return self._last_result
        old_bare, slots, fitted = self._last_bare, self._slots, self._fitted
        surviving = min(len(slots), len(tools))
        if tools is not self._last_raw:
            del slots[len(tools) :], fitted[len(tools) :]
            for index, entry in enumerate(tools):
                if index < surviving and slots[index].source is entry:
                    continue
                slot = _FittedSlot(source=entry, cut=_fitted_entry(entry, bare=False))
                shown = _bare_form(slot)[0] if index < bare else slot.cut
                if index < len(slots):
                    slots[index], fitted[index] = slot, shown
                else:
                    slots.append(slot)
                    fitted.append(shown)
        for index in range(min(old_bare, bare), min(max(old_bare, bare), surviving)):
            fitted[index] = _bare_form(slots[index])[0] if index < bare else slots[index].cut
        self._last_raw, self._last_bare, self._last_result = tools, bare, tuple(fitted)
        return self._last_result

    def bare_savings(self, index: int) -> int:
        """Return compact-JSON bytes saved by switching synced entry ``index`` from cut to bare."""
        return _bare_form(self._slots[index])[1]


def fitted_tools(
    tools: tuple[dict[str, object], ...],
    *,
    bare: int,
    cache: ToolFitCache | None = None,
) -> tuple[dict[str, object], ...]:
    """Return the tool entries a frame shows: previews cut to the frame cap, and none on the oldest ``bare`` entries.

    An entry that lost any of its previews says ``clipped``. A result that is a background job handle keeps the
    handle, which clients read for the call's status. A bare entry is never bigger than its cut form, so a frame only
    shrinks as ``bare`` grows.
    """
    return (cache or ToolFitCache()).fit(tools, bare=bare)


def _fitted_entry(entry: dict[str, object], *, bare: bool) -> dict[str, object]:
    previews = {key: text for key in _PREVIEW_KEYS if isinstance(text := entry.get(key), str)}
    kept = {} if bare else {key: _clipped(text) for key, text in previews.items()}
    handle = _job_handle(previews.get("result_preview"), whole=not bare)
    if handle is not None:
        kept["result_preview"] = handle
    if kept == previews:
        return entry
    others = {key: value for key, value in entry.items() if key not in previews}
    return {**others, **kept, "clipped": True}


@lru_cache(maxsize=2048)
def _parse_job_handle(text: str) -> str | None:
    if not text.lstrip().startswith("{"):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ("job_id", "tool", "status")):
        return None
    return json.dumps({key: value[key] for key in ("job_id", "tool", "status")})


def _job_handle(text: str | None, *, whole: bool) -> str | None:
    """Return the ``{job_id, tool, status}`` handle a result is, None for any other result.

    With ``whole``, a result within the preview cap stays as it is; otherwise only those three fields are kept.
    """
    if text is None or (minimal := _parse_job_handle(text)) is None:
        return None
    return text if whole and len(text) <= _FRAME_PREVIEW_CHARACTERS else minimal


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
    if before.compactions != after.compactions:
        delta["compactions"] = [dict(entry) for entry in after.compactions]
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
