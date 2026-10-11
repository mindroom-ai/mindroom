"""Durable desktop batch acknowledgement and transport supervision."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import aiohttp
import pytest
from nio import LocalProtocolError
from nio.durable import RecordKind, SyncBatch, SyncRecord
from nio.durable.transport import HttpError, Transport

from mindroom.desktop.command_journal import DesktopCommandJournal, DesktopCommandJournalError
from mindroom.desktop.protocol import (
    DESKTOP_COMMAND_EVENT_TYPE,
    DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
    DesktopCommand,
    DesktopResponse,
)
from mindroom.desktop.session import DesktopSessionError
from mindroom.desktop.transport import DesktopTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

COMMAND = DesktopCommand("one", "session", 1, 1000, 31000, "status", "@user:example.org", "helper")


class FakeSource:
    """Expose only the durable consumer boundary while persistence stays real."""

    def __init__(self, callback: Callable[[SyncRecord], Awaitable[None]]) -> None:
        self.callback = callback
        self.acknowledged: list[SyncBatch] = []

    async def dispatch(self, record: SyncRecord) -> None:
        """Invoke the consumer boundary without acknowledging it."""
        await self.callback(record)

    async def ack(self, batch: SyncBatch) -> None:
        """Record completed consumer admission."""
        self.acknowledged.append(batch)


def _batch(command: DesktopCommand = COMMAND) -> SyncBatch:
    return SyncBatch(uuid4(), 1, (SyncRecord(RecordKind.TO_DEVICE, None, command.to_content()),))


@pytest.mark.asyncio
async def test_desktop_batch_ack_follows_durable_admission(tmp_path: Path) -> None:
    """Acknowledgement follows a committed command that survives reopen."""
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path)

    async def admit(record: SyncRecord) -> None:
        journal.admit(DesktopCommand.from_content(record.source), "a" * 64)
        assert source.acknowledged == []

    source = FakeSource(admit)
    batch = _batch()
    await DesktopTransport(source).consume_batch(batch)
    journal.close()
    restored = DesktopCommandJournal.load(path)
    assert restored.queued()[0].command == COMMAND
    assert source.acknowledged == [batch]
    restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [DesktopCommandJournalError("disk failed"), DesktopSessionError("auth failed")])
async def test_desktop_batch_failure_is_never_acknowledged(error: Exception) -> None:
    """Authentication and persistence errors must leave the source batch pending."""

    async def reject(_record: SyncRecord) -> None:
        raise error

    source = FakeSource(reject)
    with pytest.raises(type(error), match=str(error)):
        await DesktopTransport(source).consume_batch(_batch())
    assert source.acknowledged == []


@pytest.mark.asyncio
async def test_desktop_batch_waits_for_inbox_capacity(tmp_path: Path) -> None:
    """Capacity pressure pauses admission without losing or acknowledging the batch."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3", max_entries=1)
    journal.admit(COMMAND, "a" * 64)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def admit(record: SyncRecord) -> None:
        journal.admit(DesktopCommand.from_content(record.source), "b" * 64)

    async def wait_for_capacity() -> None:
        waiting.set()
        await release.wait()

    source = FakeSource(admit)
    second = replace(COMMAND, request_id="two", sequence=2)
    batch = _batch(second)
    task = asyncio.create_task(DesktopTransport(source, wait_for_capacity=wait_for_capacity).consume_batch(batch))
    try:
        await waiting.wait()
        assert source.acknowledged == []
        journal.remember_response(COMMAND, "a" * 64, DesktopResponse("one", "session", True))
        journal.mark_delivered(journal.pending_responses()[0][0])
        release.set()
        await task
        assert journal.queued()[0].command == second
        assert source.acknowledged == [batch]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        journal.close()


@pytest.mark.asyncio
async def test_desktop_runner_retries_network_and_surfaces_revoked_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient server errors retry; permanent authentication failures stop the owner."""
    calls = []
    consumer_stopped = asyncio.Event()

    class Source:
        async def run(self) -> None:
            calls.append("run")
            raise HttpError(503 if len(calls) == 1 else 401, "M_UNKNOWN_TOKEN" if len(calls) > 1 else None)

        async def wait_for_work(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                consumer_stopped.set()

    monkeypatch.setattr("mindroom.desktop.transport._RETRY_DELAY_SECONDS", 0)
    with pytest.raises(DesktopSessionError, match="permanent authentication"):
        await DesktopTransport(Source()).run()
    assert calls == ["run", "run"]
    assert consumer_stopped.is_set()


@pytest.mark.asyncio
async def test_pairing_batch_cannot_consume_commands_or_accept_early() -> None:
    """A mixed batch remains wholly pending when no command admission owner is present."""
    dispatched = []

    async def accept(record: SyncRecord) -> None:
        dispatched.append(record)

    source = FakeSource(accept)
    batch = SyncBatch(
        uuid4(),
        1,
        (
            SyncRecord(RecordKind.TO_DEVICE, None, {"type": DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE}),
            SyncRecord(
                RecordKind.TO_DEVICE,
                None,
                {"type": "m.room.encrypted"},
                clear={"type": DESKTOP_COMMAND_EVENT_TYPE},
            ),
        ),
    )
    with pytest.raises(DesktopSessionError, match=r"drain.*before pairing"):
        await DesktopTransport(source, reject_commands=True).consume_batch(batch)
    assert dispatched == []
    assert source.acknowledged == []


@pytest.mark.asyncio
async def test_desktop_runner_retries_real_nio_connection_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long outage survives NIO's inner retry budget and still detects revocation."""
    attempts = []

    class OfflineClient:
        access_token = "test-token"  # noqa: S105

        async def send(self, *_args: object, **_kwargs: object) -> None:
            attempts.append("send")
            msg = "offline"
            raise aiohttp.ClientConnectionError(msg)

    class Source:
        runs = 0

        async def run(self) -> None:
            self.runs += 1
            if self.runs == 1:
                await Transport(OfflineClient(), 1024).request("GET", "/sync")
            raise HttpError(401, "M_UNKNOWN_TOKEN")

    async def no_delay(_delay: float) -> None:
        pass

    monkeypatch.setattr("nio.durable.transport.asyncio.sleep", no_delay)
    source = Source()
    with pytest.raises(DesktopSessionError, match="permanent authentication"):
        await DesktopTransport(source)._run_source()
    assert source.runs == 2
    assert len(attempts) == 5


@pytest.mark.asyncio
async def test_desktop_runner_preserves_nontransient_protocol_failure() -> None:
    """Invalid durable state must not enter an endless reconnect loop."""
    failure = LocalProtocolError("invalid durable state")

    class Source:
        async def run(self) -> None:
            raise failure

    with pytest.raises(LocalProtocolError) as caught:
        await DesktopTransport(Source())._run_source()
    assert caught.value is failure
