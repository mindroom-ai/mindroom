"""Passive startup ownership for tool jobs parked until an enabled restart."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.event_journal import EventKind
from mindroom.handled_turns import TurnRecordCodec
from mindroom.tool_jobs.runtime import read_job_snapshot
from mindroom.tool_jobs.settings import background_tool_jobs_enabled

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore, JournalEvent


@dataclass
class _ParkedWork:
    sources: set[tuple[str, str]] = field(default_factory=set)
    approvals: set[str] = field(default_factory=set)


_PARKED: dict[Path, _ParkedWork] = {}


def clear_parked_work(runtime_paths: RuntimePaths) -> None:
    """Release only the stopped process's passive index."""
    _PARKED.pop(runtime_paths.storage_root.resolve(), None)


def event_is_parked(config: Config, runtime_paths: RuntimePaths, entity_name: str, event: JournalEvent) -> bool:
    """Fence saved sources and every internal completion before any handoff."""
    if background_tool_jobs_enabled(config, runtime_paths):
        return False
    parked = _PARKED.get(runtime_paths.storage_root.resolve())
    return event.kind is EventKind.TOOL_JOB_COMPLETION or (
        parked is not None and (entity_name, event.event_id) in parked.sources
    )


def approval_is_parked(runtime_paths: RuntimePaths, approval_id: str) -> bool:
    """Keep parked approval owners out of startup expiry and failure cleanup."""
    parked = _PARKED.get(runtime_paths.storage_root.resolve())
    return parked is not None and approval_id in parked.approvals


def _saved_sources(runtime_paths: RuntimePaths) -> _ParkedWork:
    root = runtime_paths.storage_root / "tool_jobs"
    if root.is_symlink():
        msg = "Tool job storage must not use symlinks."
        raise ValueError(msg)
    parked = _ParkedWork()
    for path in sorted(root.glob("*.json")):
        job = read_job_snapshot(path)
        source = job.adapter.get("source_event_id")
        if isinstance(source, str):
            parked.sources.add((job.owner.transport_agent_name or job.owner.agent_name, source))
    return parked


async def index_parked_work(
    runtime_paths: RuntimePaths,
    journal: EventJournalStore | None = None,
) -> None:
    """Inspect saved ownership once; never recover, acknowledge, or execute it."""
    parked = await asyncio.to_thread(_saved_sources, runtime_paths)
    if journal is not None:
        for entity_name in {entity for entity, _source in parked.sources}:
            for event_id, _anchor, payload in await journal.turn_records(entity_name).load_all():
                if (entity_name, event_id) in parked.sources:
                    record = TurnRecordCodec._from_ledger_record(event_id, json.loads(payload))
                    if record is not None:
                        parked.sources.update((entity_name, source) for source in record.source_event_ids)
        cursor: tuple[str, str] | None = None
        while owners := await journal.approval_continuations(limit=100, after=cursor):
            for _principal_id, continuation in owners:
                owns_source = any(
                    (continuation.entity_name, event_id) in parked.sources for event_id in continuation.source_event_ids
                )
                if owns_source or continuation.requires_background_tool_jobs:
                    parked.approvals.add(continuation.approval_id)
                    parked.sources.update(
                        (continuation.entity_name, event_id) for event_id in continuation.source_event_ids
                    )
            cursor = (owners[-1][1].entity_name, owners[-1][1].approval_id)
    _PARKED[runtime_paths.storage_root.resolve()] = parked
