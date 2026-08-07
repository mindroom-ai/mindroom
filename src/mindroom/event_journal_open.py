"""Where the process's event-journal database lives.

Two callers open the same store: the bot runtime, which writes the projection
from sync, and one thread-export pass, which reads it. They have to agree on
which file or DSN that is. A disagreement does not fail — it produces an empty
store, and an export that reports every room as having no history at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal import EventJournalStore
from mindroom.event_journal_change import record_opened_event_journal

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.matrix import EventJournalConfig
    from mindroom.constants import RuntimePaths


def open_event_journal_store(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
    storage_path: Path,
) -> EventJournalStore:
    """Open the durable store this runtime's journal, projection, and outbox share.

    One database can hold every principal in the deployment; each caller
    receives only its own principal-bound view from it.

    Opening is also what makes the journal unmovable for the rest of the
    process, so this records the identity every config authority compares
    against. Recorded after the open succeeds: a store that failed to open is
    not one anybody is writing to.
    """
    store = (
        EventJournalStore.open_postgres(journal_config.resolve_postgres_database_url(runtime_paths))
        if journal_config.backend == "postgres"
        else EventJournalStore.open_sqlite(storage_path / "tracking" / "event_journal.db")
    )
    record_opened_event_journal(journal_config, runtime_paths=runtime_paths)
    return store


__all__ = ["open_event_journal_store"]
