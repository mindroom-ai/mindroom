"""Passive startup ownership for tool jobs parked until an enabled restart."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.tool_jobs.instances import tool_job_instance
from mindroom.tool_jobs.runtime import UnsupportedToolJobSnapshotError, read_job_snapshot, saved_job_paths
from mindroom.tool_jobs.settings import background_tool_jobs_enabled

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore, JournalEvent

logger = get_logger(__name__)


@dataclass
class ParkedWork:
    """Saved sources and approvals a disabled instance must leave untouched until an enabled restart."""

    sources: set[tuple[str, str]] = field(default_factory=set)
    approvals: set[str] = field(default_factory=set)


def _parked_work(runtime_paths: RuntimePaths) -> ParkedWork | None:
    instance = tool_job_instance(runtime_paths)
    return instance.parked if instance is not None else None


def event_is_parked(config: Config, runtime_paths: RuntimePaths, entity_name: str, event: JournalEvent) -> bool:
    """Fence saved sources before any handoff."""
    if background_tool_jobs_enabled(config, runtime_paths):
        return False
    parked = _parked_work(runtime_paths)
    return parked is not None and (entity_name, event.event_id) in parked.sources


def approval_is_parked(runtime_paths: RuntimePaths, approval_id: str) -> bool:
    """Keep parked approval owners out of startup expiry and failure cleanup."""
    parked = _parked_work(runtime_paths)
    return parked is not None and approval_id in parked.approvals


def _saved_sources(runtime_paths: RuntimePaths) -> ParkedWork:
    root = runtime_paths.storage_root / "tool_jobs"
    if root.is_symlink():
        msg = "Tool job storage must not use symlinks."
        raise ValueError(msg)
    parked = ParkedWork()
    for path in saved_job_paths(root):
        try:
            job = read_job_snapshot(path)
        except (UnsupportedToolJobSnapshotError, ValueError) as error:
            # A retired schema, invalid JSON, or rejected contents: this instance never opted in,
            # and enabled recovery still reports the file loudly.
            logger.warning(
                "Ignoring unreadable tool job snapshot while background tool jobs are disabled",
                path=str(path),
                error=str(error),
            )
            continue
        # A reply that consumed a parked outcome must not replay through ordinary execution either.
        for source in (job.source_event_id, job.consuming_source):
            if source is not None:
                parked.sources.add((job.owner.recipient, source))
    return parked


async def index_parked_work(
    runtime_paths: RuntimePaths,
    journal: EventJournalStore | None = None,
) -> ParkedWork:
    """Inspect saved ownership once; never recover, acknowledge, or execute it."""
    parked = await asyncio.to_thread(_saved_sources, runtime_paths)
    if journal is not None:
        for entity_name, event_id in tuple(parked.sources):
            record = await journal.turn_records(entity_name).load(event_id)
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
    return parked
