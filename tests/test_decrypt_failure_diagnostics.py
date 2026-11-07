"""Unreadable-message diagnostics never own or settle application messages."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import nio
import pytest
from nio.durable import SyncBatch

from mindroom import conversation_resolver
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.matrix import client_delivery, decrypt_failure
from mindroom.matrix.decrypt_failure import handle_decrypt_failure
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from tests.access_schema_support import with_responder_access
from tests.journal_membership_helpers import admit_room_membership
from tests.test_decrypt_failure import _megolm_event, _mock_client, _mock_room, _runtime_paths
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

    async def notify(_client: nio.AsyncClient, room_id: str, _event: nio.MegolmEvent, _resolve: object) -> bool:
        notices.append(room_id)
        return True

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(decrypt_failure, "_send_decrypt_failure_notice", notify)
    bot.orchestrator.response_admission_gate.close()
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
        bot.orchestrator.response_admission_gate.reopen()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        assert requests == [(ROOM, "$cipher")]
        assert notices == [ROOM]
        assert await bot.journal_principal().is_pending("$cipher")
    finally:
        bot.orchestrator.response_admission_gate.reopen()
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

    async def notify(_client: nio.AsyncClient, room_id: str, _event: nio.MegolmEvent, _resolve: object) -> bool:
        notices.append(room_id)
        return True

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(decrypt_failure, "_send_decrypt_failure_notice", notify)
    bot.orchestrator.response_admission_gate.close()
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
        bot.orchestrator.response_admission_gate.reopen()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        assert requests == (["$cipher"] if case == "join_fence" else [])
        assert notices == []
    finally:
        bot.orchestrator.response_admission_gate.reopen()
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

    async def notify(_client: nio.AsyncClient, room_id: str, _event: nio.MegolmEvent, _resolve: object) -> bool:
        notices.append(room_id)
        return True

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(decrypt_failure, "_send_decrypt_failure_notice", notify)
    try:
        bot._decryption_diagnostics.schedule(_ciphertext(nio.TimelineEventProvenance.LIVE))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not bot.orchestrator.response_admission_gate.close_if_idle()
        if case == "rejoin":
            await admit_room_membership(bot.journal_principal(), ROOM, "leave")
            await admit_room_membership(bot.journal_principal(), ROOM, "join")
        else:
            with_responder_access(bot.config, bot.agent_name, users=[])
        release.set()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        assert notices == []
        assert bot.orchestrator.response_admission_gate.close_if_idle()
    finally:
        release.set()
        bot.orchestrator.response_admission_gate.reopen()
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


@pytest.mark.asyncio
@pytest.mark.parametrize("original", ["known", "reply", "unknown", "unavailable", "server-error", "stalled"])
async def test_edit_notice_joins_the_edited_message_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original: str,
) -> None:
    """An undecryptable edit's or reply's notice joins its target's thread, else falls back to a plain reply."""
    bot = await _bot(tmp_path)
    sent: list[dict[str, object]] = []
    original_source = {
        "type": "m.room.message",
        "event_id": "$original",
        "room_id": ROOM,
        "sender": SENDER,
        "origin_server_ts": 5,
        "content": {"msgtype": "m.text", "body": "hi", "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"}},
    }

    async def request(_event: nio.MegolmEvent) -> None:
        return None

    async def send(_client: nio.AsyncClient, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        sent.append(content)
        return object()

    async def get_event(_room_id: str, event_id: str) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        if original == "stalled":
            await asyncio.Event().wait()
        if original == "server-error":
            return nio.RoomGetEventError("boom", "M_UNKNOWN")
        if original in {"known", "reply"} and event_id == "$original":
            return nio.RoomGetEventResponse.from_dict(original_source)
        return nio.RoomGetEventError("not found", "M_NOT_FOUND")

    monkeypatch.setattr(bot.client, "request_room_key", request)
    monkeypatch.setattr(bot.client, "room_get_event", get_event)
    monkeypatch.setattr(client_delivery, "send_message_result", send)
    monkeypatch.setattr(conversation_resolver, "_RELATED_THREAD_LOOKUP_TIMEOUT_SECONDS", 0.05)
    if original != "unknown":
        # "unavailable": the journal names the thread, but the homeserver cannot confirm it, so the resolver raises.
        await bot.journal_principal().admit(
            InboundEvent(
                "$original",
                ROOM,
                "$thread",
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                SENDER,
                5,
                original_source,
            ),
        )
    record = _ciphertext(nio.TimelineEventProvenance.LIVE)
    relation = (
        {"m.in_reply_to": {"event_id": "$original"}}
        if original == "reply"
        else {"rel_type": "m.replace", "event_id": "$original"}
    )
    content = {**record.source["content"], "m.relates_to": relation}
    try:
        bot._decryption_diagnostics.schedule(replace(record, source={**record.source, "content": content}))
        assert await wait_for_background_tasks(timeout=5, owner=bot._runtime_view)
        expected = (
            {
                "rel_type": "m.thread",
                "event_id": "$thread",
                "is_falling_back": False,
                "m.in_reply_to": {"event_id": "$cipher"},
            }
            if original in {"known", "reply"}
            else {"io.mindroom.in_reply_to": {"event_id": "$cipher"}}
        )
        assert [message["m.relates_to"] for message in sent] == [expected]
    finally:
        await wait_for_background_tasks(timeout=0, owner=bot._runtime_view)
        await bot._journal_store.close()


@pytest.mark.asyncio
async def test_concurrent_bots_with_slow_resolver_post_one_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger claim lands before the thread lookup, so a second bot never waits on it or posts again."""
    release = asyncio.Event()
    lookups: list[str] = []
    sent: list[dict[str, object]] = []

    async def resolve(_room_id: str, related_event_id: str) -> str | None:
        lookups.append(related_event_id)
        await release.wait()
        return "$thread"

    async def send(_client: nio.AsyncClient, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        sent.append(content)
        return object()

    event = _megolm_event(event_id="$failed:localhost", relates_to={"rel_type": "m.replace", "event_id": "$original"})
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr(client_delivery, "send_message_result", send)
    tasks = [
        asyncio.create_task(
            handle_decrypt_failure(
                _mock_client(user_id=f"@mindroom_{name}:localhost"),
                _mock_room(),
                event,
                agent_name=name,
                runtime_paths=runtime_paths,
                resolve_related_thread=resolve,
            ),
        )
        for name in ("first", "second")
    ]
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)
    assert lookups == ["$original"]
    assert [message["m.relates_to"]["event_id"] for message in sent] == ["$thread"]
