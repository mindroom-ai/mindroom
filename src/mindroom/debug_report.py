"""Collect everything the backend stored about one reported conversation.

Every reader is read-only. The runtime's own openers create or migrate schema,
so they are never used here: inspecting an install must not change it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

AI_RUN_KEY = "io.mindroom.ai_run"


@dataclass(frozen=True)
class DebugReportIds:
    """Identifiers of one reported conversation."""

    room_id: str | None = None
    thread_id: str | None = None
    event_ids: frozenset[str] = frozenset()
    run_ids: frozenset[str] = frozenset()
    session_ids: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        """Return whether there is nothing to look up."""
        return not (self.room_id or self.thread_id or self.event_ids or self.run_ids)

    def to_json(self) -> dict[str, object]:
        """Return a JSON-ready view with sorted lists."""
        return {
            "roomId": self.room_id,
            "threadId": self.thread_id,
            "eventIds": sorted(self.event_ids),
            "runIds": sorted(self.run_ids),
            "sessionIds": sorted(self.session_ids),
        }


def _ai_runs(value: object) -> Iterator[Mapping[str, Any]]:
    """Yield every AI run block nested anywhere in an event; streaming edits carry it in m.new_content."""
    if isinstance(value, Mapping):
        for key, child in cast("Mapping[str, object]", value).items():
            if key == AI_RUN_KEY and isinstance(child, Mapping):
                yield cast("Mapping[str, Any]", child)
            yield from _ai_runs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _ai_runs(child)


def _scan_event(raw: object, events: set[str], run_ids: set[str], session_ids: set[str]) -> None:
    """Add the event id and every nested AI run identifier of one raw event."""
    if not isinstance(raw, Mapping):
        return
    event = cast("Mapping[str, Any]", raw)
    if isinstance(event.get("event_id"), str):
        events.add(event["event_id"])
    for ai_run in _ai_runs(raw):
        if isinstance(ai_run.get("run_id"), str):
            run_ids.add(ai_run["run_id"])
        if isinstance(ai_run.get("session_id"), str):
            session_ids.add(ai_run["session_id"])


def collect_ids(
    report: Mapping[str, Any] | None,
    *,
    event_ids: Iterable[str] = (),
    room_id: str | None = None,
    thread_id: str | None = None,
) -> DebugReportIds:
    """Merge identifiers from a MindRoom Chat bug report and explicit flags."""
    events = set(event_ids)
    run_ids: set[str] = set()
    session_ids: set[str] = set()
    if report is not None:
        target = report.get("target") or {}
        room_id = room_id or target.get("roomId")
        thread_id = thread_id or target.get("threadId")
        if isinstance(target.get("eventId"), str):
            events.add(target["eventId"])
        for entry in report.get("events") or []:
            _scan_event(entry.get("event"), events, run_ids, session_ids)
            _scan_event(entry.get("latestEdit"), events, run_ids, session_ids)
    if thread_id:
        events.add(thread_id)
    if room_id:
        session_ids.add(f"{room_id}:{thread_id}" if thread_id else room_id)
    return DebugReportIds(
        room_id=room_id,
        thread_id=thread_id,
        # Local echoes ("~…") never reached the homeserver, so the backend cannot know them.
        event_ids=frozenset(event for event in events if event and not event.startswith("~")),
        run_ids=frozenset(run_ids),
        session_ids=frozenset(session_ids),
    )
