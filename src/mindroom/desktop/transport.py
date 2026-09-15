"""Owned desktop polling and acknowledgement after durable application admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from nio.durable import RecordKind
from nio.durable.transport import ConnectionRetriesExhausted, HttpError

from mindroom.desktop.command_journal import DesktopCommandJournalFullError
from mindroom.desktop.protocol import DESKTOP_COMMAND_EVENT_TYPE
from mindroom.desktop.session import DesktopSessionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nio.durable import DurableSync, SyncBatch

_RETRY_DELAY_SECONDS = 1.0


@dataclass
class DesktopTransport:
    """Supervise one durable source and one admission consumer."""

    source: DurableSync
    wait_for_capacity: Callable[[], Awaitable[None]] | None = None
    reject_commands: bool = False
    on_batch_acknowledged: Callable[[], None] | None = None

    async def consume_batch(self, batch: SyncBatch) -> None:
        """Acknowledge only after every relevant callback has persisted admission."""
        if self.reject_commands and any(
            record.kind is RecordKind.TO_DEVICE
            and (
                record.source.get("type") == DESKTOP_COMMAND_EVENT_TYPE
                or (record.clear is not None and record.clear.get("type") == DESKTOP_COMMAND_EVENT_TYPE)
            )
            for record in batch.records
        ):
            msg = "Start the existing desktop bridge to drain pending commands before pairing again."
            raise DesktopSessionError(msg)
        for record in batch.records:
            if record.kind is not RecordKind.TO_DEVICE:
                continue
            while True:
                try:
                    await self.source.dispatch(record)
                except DesktopCommandJournalFullError:
                    if self.wait_for_capacity is None:
                        raise
                    await self.wait_for_capacity()
                else:
                    break
        await self.source.ack(batch)
        if self.on_batch_acknowledged is not None:
            self.on_batch_acknowledged()

    async def _consume(self) -> None:
        while True:
            await self.source.wait_for_work()
            batch = await self.source.next_batch()
            if batch is not None:
                await self.consume_batch(batch)

    async def _run_source(self) -> None:
        failures = 0
        while True:
            try:
                await self.source.run()
            except HttpError as exc:
                if exc.status in {401, 403} or exc.errcode in {"M_FORBIDDEN", "M_UNKNOWN_TOKEN", "M_USER_DEACTIVATED"}:
                    msg = "Desktop Matrix transport stopped after permanent authentication failure."
                    raise DesktopSessionError(msg) from exc
                if exc.status not in {408, 429} and exc.status < 500:
                    raise
            except (
                ConnectionRetriesExhausted,
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                TimeoutError,
            ):
                pass
            else:
                return
            await asyncio.sleep(min(_RETRY_DELAY_SECONDS * 2 ** min(failures, 5), 30.0))
            failures += 1

    async def run(self) -> None:
        """Stop both tasks on failure or cancellation, preserving the original error."""
        tasks = {
            asyncio.create_task(self._run_source(), name="desktop_matrix_source"),
            asyncio.create_task(self._consume(), name="desktop_matrix_admission"),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["DesktopTransport"]
