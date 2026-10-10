"""Background tool jobs live in the event journal, written only by the runtime generation that owns them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import ToolJobExistsError, ToolJobOwnershipLostError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore


@pytest.mark.asyncio
async def test_snapshot_and_payload_round_trip(journal_store: EventJournalStore) -> None:
    """A later snapshot keeps the payload its outcome saved; deletion forgets both."""
    jobs = journal_store.tool_jobs("first")
    await jobs.take_ownership()
    await jobs.accept("job", '{"status": "running"}')
    assert await jobs.load_payload("job") is None
    await jobs.save("job", '{"status": "completed"}', '{"value": "full result"}')
    await jobs.save("job", '{"status": "completed", "consumed": true}')
    assert [(job.job_id, job.job_json) for job in await jobs.load_all()] == [
        ("job", '{"status": "completed", "consumed": true}'),
    ]
    assert await jobs.load_payload("job") == '{"value": "full result"}'
    assert [job.job_id for job in await journal_store.saved_tool_jobs()] == ["job"]
    await jobs.delete("job")
    assert await jobs.load_all() == ()
    assert await jobs.load_payload("job") is None


@pytest.mark.asyncio
async def test_an_accepted_id_is_never_accepted_again(journal_store: EventJournalStore) -> None:
    """Admission refuses an ID any runtime accepted, even after a later runtime took over."""
    first = journal_store.tool_jobs("first")
    await first.take_ownership()
    await first.accept("job", "{}")
    second = journal_store.tool_jobs("second")
    await second.take_ownership()
    with pytest.raises(ToolJobExistsError):
        await second.accept("job", "{}")


@pytest.mark.asyncio
async def test_a_newer_runtime_fences_the_older_one(journal_database: Callable[[], EventJournalStore]) -> None:
    """Once another process takes over, every write of the older runtime is refused and changes nothing."""
    older = journal_database().tool_jobs("older")
    await older.take_ownership()
    await older.accept("job", '{"status": "running"}')
    newer = journal_database().tool_jobs("newer")
    await newer.take_ownership()
    for write in (
        older.require_ownership(),
        older.accept("other", "{}"),
        older.save("job", '{"status": "completed"}', '{"value": "late"}'),
        older.delete("job"),
    ):
        with pytest.raises(ToolJobOwnershipLostError):
            await write
    assert [(job.job_id, job.job_json) for job in await newer.load_all()] == [("job", '{"status": "running"}')]
    assert await newer.load_payload("job") is None
    await newer.save("job", '{"status": "interrupted"}')
    assert [job.job_json for job in await newer.load_all()] == ['{"status": "interrupted"}']


@pytest.mark.asyncio
async def test_writes_need_ownership(journal_store: EventJournalStore) -> None:
    """A runtime that never took ownership cannot write."""
    with pytest.raises(ToolJobOwnershipLostError):
        await journal_store.tool_jobs("unowned").accept("job", "{}")
