"""Unreadable-message diagnostics never own or settle application messages."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import nio
import pytest
from nio.durable import SyncBatch

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.matrix import decrypt_failure
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from tests.access_schema_support import with_responder_access
from tests.journal_membership_helpers import admit_room_membership
from tests.test_durable_ingestion_admission import Session
from tests.test_durable_ingestion_decryption import ROOM, SENDER, _ciphertext, _decrypted
from tests.test_room_member_hooks import _router_bot

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


async def _bot(tmp_path: Path) -> AgentBot:
    bot = _router_bot(tmp_path)
    with_responder_access(bot.config, bot.agent_name, users=[SENDER])
    assert bot.client is not None
    bot.client.outgoing_key_requests = {}
    await admit_room_membership(bot.journal_principal(), ROOM, "join")
    return bot


@pytest.mark.asyncio
async def test_waiting_diagnostic_cannot_settle_a_decrypted_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delayed notice completion must leave the separately admitted message pending."""
    bot = await _bot(tmp_path)
    requests: list[tuple[str, str]] = []
    notices: list[str] = []

    async def request(event: nio.MegolmEvent) -> None:
        requests.append((event.room_id, event.event_id))

    async def notify(_client: nio.AsyncClient, room_id: str) -> bool:
        notices.append(room_id)
        return True

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(decrypt_failure, "_send_decrypt_failure_notice", notify)
    bot.admission_gate.close()
    record = _ciphertext(nio.TimelineEventProvenance.LIVE)
    try:
        principal = bot.journal_principal()
        consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
        assert consumer.stream_id is not None
        cursor = await principal._backend.read(
            lambda transaction: transaction.fetchone("SELECT next_sequence FROM matrix_sync_consumers"),
        )
        assert cursor is not None
        batch = SyncBatch(consumer.stream_id, int(cursor["next_sequence"]), (record,))
        async with asyncio.timeout(1):
            for _ in range(2):
                await consume_one_ingestion_batch(
                    Session(batch),
                    principal,
                    account_id=bot.agent_user.user_id,
                    on_decryption_failure=bot._decryption_diagnostics.schedule,
                )
        await asyncio.sleep(0)
        assert requests == []
        clear = _decrypted(record)
        assert clear.clear is not None
        await bot.journal_principal().admit(
            InboundEvent("$cipher", ROOM, "$thread", EventKind.MESSAGE, EventClass.ACTIONABLE, SENDER, 10, clear.clear),
        )
        bot.admission_gate.reopen()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        assert requests == [(ROOM, "$cipher")]
        assert notices == [ROOM]
        assert await bot.journal_principal().is_pending("$cipher")
    finally:
        bot.admission_gate.reopen()
        await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
        await bot._journal_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["history", "authorization", "rejoin", "join_fence"])
async def test_diagnostic_uses_current_access_and_original_tenure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Queued diagnostics cannot bypass history silence, access revocation, or room tenure."""
    bot = await _bot(tmp_path)
    requests: list[str] = []
    notices: list[str] = []

    async def request(event: nio.MegolmEvent) -> None:
        requests.append(event.event_id)

    async def notify(_client: nio.AsyncClient, room_id: str) -> bool:
        notices.append(room_id)
        return True

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(decrypt_failure, "_send_decrypt_failure_notice", notify)
    bot.admission_gate.close()
    provenance = nio.TimelineEventProvenance.HISTORY if case == "history" else nio.TimelineEventProvenance.LIVE
    try:
        bot._decryption_diagnostics.schedule(_ciphertext(provenance))
        if case == "authorization":
            with_responder_access(bot.config, bot.agent_name, users=[])
        elif case == "rejoin":
            await admit_room_membership(bot.journal_principal(), ROOM, "leave")
            await admit_room_membership(bot.journal_principal(), ROOM, "join")
        elif case == "join_fence":
            bot._room_lifecycle._decrypt_notice_fenced_room_ids.add(ROOM)
        bot.admission_gate.reopen()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        assert requests == (["$cipher"] if case == "join_fence" else [])
        assert notices == []
    finally:
        bot.admission_gate.reopen()
        await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
        await bot._journal_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["rejoin", "authorization"])
async def test_diagnostic_rechecks_access_after_requesting_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Revoking access during a key request suppresses its later notice."""
    bot = await _bot(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    notices: list[str] = []

    async def request(_event: nio.MegolmEvent) -> None:
        started.set()
        await release.wait()

    async def notify(_client: nio.AsyncClient, room_id: str) -> bool:
        notices.append(room_id)
        return True

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(decrypt_failure, "_send_decrypt_failure_notice", notify)
    try:
        bot._decryption_diagnostics.schedule(_ciphertext(nio.TimelineEventProvenance.LIVE))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not bot.admission_gate.close_if_idle()
        if case == "rejoin":
            await admit_room_membership(bot.journal_principal(), ROOM, "leave")
            await admit_room_membership(bot.journal_principal(), ROOM, "join")
        else:
            with_responder_access(bot.config, bot.agent_name, users=[])
        release.set()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        assert notices == []
        assert bot.admission_gate.close_if_idle()
    finally:
        release.set()
        bot.admission_gate.reopen()
        await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
        await bot._journal_store.close()


@pytest.mark.asyncio
async def test_runtime_shutdown_drains_its_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime owner can cancel the outstanding reader before closing its client."""
    bot = await _bot(tmp_path)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def request(_event: nio.MegolmEvent) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(bot.client, "request_room_key", request)
    try:
        bot._decryption_diagnostics.schedule(_ciphertext(nio.TimelineEventProvenance.LIVE))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
        assert cleaned.is_set()
        assert await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
    finally:
        await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
        await bot._journal_store.close()
