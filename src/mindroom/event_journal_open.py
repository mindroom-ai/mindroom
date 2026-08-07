"""Where the process's event-journal database lives.

Two callers open the same store: the bot runtime, which writes the projection
from sync, and one thread-export pass, which reads it. They have to agree on
which file or DSN that is. A disagreement does not fail — it produces an empty
store, and an export that reports every room as having no history at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal import EventJournalStore

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.matrix import CacheConfig
    from mindroom.constants import RuntimePaths


def open_event_journal_store(
    cache_config: CacheConfig,
    *,
    runtime_paths: RuntimePaths,
    storage_path: Path,
) -> EventJournalStore:
    """Open the durable store this runtime's journal, projection, and outbox share.

    One database can hold every principal in the deployment; each caller
    receives only its own principal-bound view from it.
    """
    if cache_config.backend == "postgres":
        return EventJournalStore.open_postgres(cache_config.resolve_postgres_database_url(runtime_paths))
    return EventJournalStore.open_sqlite(storage_path / "tracking" / "event_journal.db")


__all__ = ["open_event_journal_store"]
