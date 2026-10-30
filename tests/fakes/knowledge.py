"""Knowledge fakes: a refresh scheduler that records refreshes instead of running them, and published-index lookups."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import mindroom.knowledge.refresh_scheduler as knowledge_refresh_scheduler
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.index_metadata import RefreshStatus, new_publication
from mindroom.knowledge.indexing_config import IndexingSettings
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
from mindroom.knowledge.registry import PublishedIndexKey, PublishedIndexResolution

if TYPE_CHECKING:
    import asyncio

    import pytest

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.index_reader import PublishedIndexReader
    from mindroom.knowledge.refresh_runner import KnowledgeRefreshResult
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ScheduledRefresh:
    """One refresh a recording scheduler would have started."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None


@dataclass
class RecordingRefreshScheduler(KnowledgeRefreshScheduler):
    """A refresh scheduler that keeps its cooldowns but records each refresh it would start instead of running it."""

    scheduled: list[ScheduledRefresh] = field(default_factory=list)

    def _schedule(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
        force_reindex: bool = False,
        waiter: asyncio.Future[KnowledgeRefreshResult] | None = None,
    ) -> None:
        del force_reindex
        if waiter is not None:
            waiter.cancel()
        self.scheduled.append(ScheduledRefresh(base_id, config, runtime_paths, execution_identity))

    @property
    def scheduled_base_ids(self) -> list[str]:
        """Return the base of each recorded refresh, in order."""
        return [refresh.base_id for refresh in self.scheduled]


def install_recording_refresh_scheduler(monkeypatch: pytest.MonkeyPatch) -> RecordingRefreshScheduler:
    """Make a recording scheduler the process's refresh scheduler, which file memory schedules through."""
    scheduler = RecordingRefreshScheduler()
    monkeypatch.setattr(knowledge_refresh_scheduler, "_runtime_scheduler", scheduler)
    return scheduler


def fake_indexing_settings(base_id: str, *, storage_root: str, knowledge_path: str) -> IndexingSettings:
    """Return semantic indexing settings for one base, as a publication records them."""
    return IndexingSettings(
        base_id=base_id,
        storage_root=storage_root,
        knowledge_path=knowledge_path,
        mode="semantic",
        embedder_provider="openai",
        embedder_model="text-embedding-3-small",
        embedder_host="",
        embedder_dimensions="",
        chunk_size="5000",
        chunk_overlap="0",
        repo_identity="",
        git_branch="",
        git_lfs="",
        git_skip_hidden="",
        git_include_patterns="",
        git_exclude_patterns="",
        include_patterns="",
        exclude_patterns="",
        include_extensions="",
        exclude_extensions="()",
    )


def published_index_lookup(
    base_id: str,
    reader: object | None,
    *,
    availability: KnowledgeAvailability | None = None,
    storage_root: str = "storage",
    knowledge_path: str | None = None,
) -> PublishedIndexResolution:
    """Return a lookup of ``base_id``: published and served by ``reader``, or unpublished when ``reader`` is None.

    ``availability`` defaults to READY for a published base and INITIALIZING for an unpublished one.
    """
    knowledge_path = knowledge_path or f"{storage_root}/{base_id}"
    settings = fake_indexing_settings(base_id, storage_root=storage_root, knowledge_path=knowledge_path)
    publication = (
        None
        if reader is None
        else new_publication(
            settings=settings,
            collection=f"{base_id}_collection",
            indexed_count=1,
            source_signature=hashlib.sha256().hexdigest(),
            published_revision=None,
        )
    )
    if availability is None:
        availability = KnowledgeAvailability.INITIALIZING if reader is None else KnowledgeAvailability.READY
    return PublishedIndexResolution(
        key=PublishedIndexKey(
            base_id=base_id,
            storage_root=storage_root,
            knowledge_path=knowledge_path,
            indexing_settings=settings,
        ),
        reader=cast("PublishedIndexReader | None", reader),
        publication=publication,
        status=RefreshStatus(),
        availability=availability,
    )
