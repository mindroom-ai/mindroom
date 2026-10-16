"""Run the knowledge graph store and ingestion worker while some agent enables the graph."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.knowledge_graph.ingestion import KnowledgeGraphIngestionWorker
from mindroom.knowledge_graph.store import GraphitiStore, record_knowledge_graph_failure, set_knowledge_graph_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.ingestion import RedactedEventIds


@dataclass
class KnowledgeGraphRuntime:
    """Own the runtime's one graph store and its ingestion worker."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    redacted_event_ids: RedactedEventIds
    _store: GraphitiStore | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    async def sync(self) -> None:
        """Start the store and worker once an agent enables the graph, and stop them once none does."""
        config = self.config_provider()
        if config is None or not config.knowledge_graph_agents():
            await self.stop()
            return
        if self._task is not None and not self._task.done():
            return
        await self.stop()
        store = GraphitiStore(lambda: self.config_provider() or config, self.runtime_paths)
        worker = KnowledgeGraphIngestionWorker(
            runtime_paths=self.runtime_paths,
            config_provider=self.config_provider,
            store=store,
            redacted_event_ids=self.redacted_event_ids,
        )
        self._store = store
        set_knowledge_graph_store(store)
        self._task = asyncio.create_task(worker.run(), name="knowledge_graph_ingestion")

    async def stop(self) -> None:
        """Stop the worker, then close the store and its embedded database."""
        store, task = self._store, self._task
        self._store, self._task = None, None
        if task is not None:
            # A pass can wait minutes on extraction calls; its marker records progress, so cancelling loses none.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if store is not None:
            set_knowledge_graph_store(None)
            await store.close()
            record_knowledge_graph_failure(None)
