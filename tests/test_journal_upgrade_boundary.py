"""Upgrade boundaries must not turn pre-upgrade work into fresh requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.test_event_journal_store import admit

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore


@pytest.mark.asyncio
async def test_pre_durable_journal_is_refused_without_adopting_pending_work(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """Opening an old journal must fail before old pending requests can dispatch."""
    store = journal_database()
    principal = store.principal("@bot:example.org")
    await admit(principal, "$old-request")
    await store.backend.write(lambda tx: tx.execute("DROP TABLE matrix_sync_consumers"))

    with pytest.raises(RuntimeError, match="fresh event journal"):
        journal_database()

    assert [event.event_id for event in await principal.pending()] == ["$old-request"]
    # A failed open must not install the new producer-binding table. A second
    # attempt must still refuse, instead of now mistaking old work for current.
    with pytest.raises(RuntimeError, match="fresh event journal"):
        journal_database()
