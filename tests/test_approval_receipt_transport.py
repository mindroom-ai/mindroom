"""The asking account sends and edits its approval cards, and large argument evidence stays usable."""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.approval.manager import ApprovalManager
from mindroom.approval.transport import ApprovalMatrixTransport, _offload_oversized_full_arguments
from mindroom.event_journal import (
    ApprovalCardReservation,
    ApprovalDeliveryView,
    CallKey,
    DeliveryStage,
    EventJournalStore,
    MatrixDelivery,
)
from mindroom.matrix.large_messages import content_fits_normal_event
from tests.conftest import approval_manager_for_test, test_runtime_paths

_ROOM = "!room:example.org"
_CARD_TYPE = "io.mindroom.tool_approval"


def _bot(name: str) -> MagicMock:
    """Return a running bot of one entity that is in the room."""
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = f"@mindroom_{name}:example.org"
    client.device_id = f"{name}-device"
    return MagicMock(
        agent_name=name,
        running=True,
        client=client,
        approval_room_ids=frozenset({_ROOM}),
        latest_thread_event_id_if_needed=AsyncMock(return_value=None),
    )


def _transport(
    bots: dict[str, MagicMock],
    senders: dict[str, str | None] | None = None,
    cards: ApprovalDeliveryView | None = None,
) -> ApprovalMatrixTransport:
    """Return the transport over these bots, reading each card's sender from ``senders`` or from ``cards``."""
    if cards is None:
        cards = MagicMock()
        cards.approvals.approval_card_sender = AsyncMock(
            side_effect=lambda *, delivery_id: (senders or {})[delivery_id]
        )
    return ApprovalMatrixTransport(
        bot_provider=bots.get,
        room_poster=lambda _room_id: next(iter(bots.values()), None),
        cards=lambda: cards,
    )


def _delivery(*, edits_event_id: str | None = None) -> MatrixDelivery:
    return MatrixDelivery(
        delivery_id="card-1",
        stage=DeliveryStage.INITIAL if edits_event_id is None else DeliveryStage.FINAL,
        room_id=_ROOM,
        membership_epoch=0,
        thread_id=None,
        transaction_id="txn-1",
        payload={"approval_id": "card-1", "body": "Approval required"},
        edits_event_id=edits_event_id,
        acknowledged_event_id=None,
        created_at_ns=1,
        event_type=_CARD_TYPE,
    )


@pytest.fixture
def sending_clients(monkeypatch: pytest.MonkeyPatch) -> list[nio.AsyncClient]:
    """Accept every send and return the client each one went through."""
    used: list[nio.AsyncClient] = []

    async def send(client: nio.AsyncClient, room_id: str, *_event: object, **_options: object) -> nio.RoomSendResponse:
        used.append(client)
        return nio.RoomSendResponse(f"$event-{len(used)}", room_id)

    monkeypatch.setattr("mindroom.approval.transport.send_room_event_result", send)
    return used


@pytest.mark.asyncio
async def test_card_is_sent_by_asking_agent(sending_clients: list[nio.AsyncClient]) -> None:
    """The bot of the entity a card records sends it, with another bot in the room."""
    bots = {"other": _bot("other"), "code": _bot("code")}

    await _transport(bots, {"card-1": "code"}).send_approval_delivery(_delivery())

    assert sending_clients == [bots["code"].client]


@pytest.mark.asyncio
async def test_terminal_edit_goes_through_card_sender(sending_clients: list[nio.AsyncClient]) -> None:
    """Only the account that sent a card edits it."""
    bots = {"other": _bot("other"), "code": _bot("code")}

    await _transport(bots, {"card-1": "code"}).send_approval_delivery(_delivery(edits_event_id="$card"))

    assert sending_clients == [bots["code"].client]
    bots["other"].client.room_send.assert_not_called()


@pytest.mark.asyncio
async def test_team_turn_card_is_sent_by_team_account(tmp_path: Path) -> None:
    """A team turn's card names the team account as its sender, not the member whose tool asks."""
    prepared_for: list[str] = []

    async def prepare(_room_id: str, _thread_id: str | None, content: dict, sender_entity: str) -> dict:
        prepared_for.append(sender_entity)
        return content

    manager = approval_manager_for_test(test_runtime_paths(tmp_path), prepare_approval_event=prepare)
    card = await manager.prepare_reply_card(
        approval_id="card-1",
        continuation_id="card-1",
        continuation_generation=0,
        entity_name="research_team",
        response_event_id="$reply",
        tool_call_id="call-1",
        tool_name="shell",
        arguments={"command": "ls"},
        room_id=_ROOM,
        requester_id="@alice:example.org",
        approver_user_id="@alice:example.org",
        expires_at_ns=time.time_ns() + 60_000_000_000,
        agent_name="member",
        thread_id=None,
        grant_operation=None,
    )

    assert card is not None
    assert card.sender_entity == "research_team"
    assert prepared_for == ["research_team"]


def _reservation(sender_entity: str | None) -> ApprovalCardReservation:
    return ApprovalCardReservation(
        delivery_id="card-1",
        tool_call_id="call-1",
        event_type=_CARD_TYPE,
        payload={"approval_id": "card-1", "body": "Approval required"},
        approver_user_id="@alice:example.org",
        approvable=True,
        sender_entity=sender_entity,
    )


async def _reserve_card(cards: ApprovalDeliveryView, sender_entity: str | None) -> CallKey:
    call = CallKey.detached("run-1", "call-1")
    assert await cards.approvals.reserve_detached_card(
        call=call,
        owner_kind="script",
        tool_name="shell",
        toolkit_name=None,
        invoking_agent="code",
        room_id=_ROOM,
        thread_id=None,
        expires_at_ns=time.time_ns() + 3_600_000_000_000,
        card=_reservation(sender_entity),
    )
    return call


@pytest.mark.asyncio
async def test_card_row_records_sender_entity(tmp_path: Path) -> None:
    """The card row keeps the entity that sends the card."""
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    try:
        cards = journal.principal("router@shared")
        await _reserve_card(cards, "code")

        assert await cards.approvals.approval_card_sender(delivery_id="card-1") == "code"
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_timed_card_offloads_complete_arguments_before_receipt_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A near-limit original must leave space for durable timed decision evidence."""
    arguments = {"command": "x" * 52_000}
    content = {
        "arguments": {"command": "preview"},
        "arguments_truncated": True,
        "full_arguments": arguments,
        "auto_approve_options": [300, 600, 1800],
        "approvable": True,
    }
    assert content_fits_normal_event(content)
    encrypted_file = {
        "url": "mxc://example.org/arguments",
        "key": {"k": "key"},
        "iv": "iv",
        "hashes": {"sha256": "hash"},
        "v": "v2",
        "size": 53_000,
        "mimetype": "application/json",
    }
    upload = AsyncMock(return_value=("mxc://example.org/arguments", encrypted_file))
    monkeypatch.setattr("mindroom.approval.transport.upload_json_sidecar", upload)
    client = MagicMock(spec=nio.AsyncClient)

    prepared = await _offload_oversized_full_arguments(client, "!room:example.org", content)

    # Encrypted in every room: the card is sent later, perhaps after the room turned encrypted.
    upload.assert_awaited_once_with(client, "!room:example.org", arguments, room_encrypted=True, durable=True)
    assert "full_arguments" not in prepared
    assert prepared["full_arguments_file"] == encrypted_file
    assert "full_arguments_url" not in prepared
    assert prepared["approvable"] is True
    assert content_fits_normal_event({**prepared, "approval_provenance": {"evidence": "x" * 4_000}})


def _card_event(sender: str) -> MagicMock:
    """Return the room's answer for a card event one account sent."""
    response = MagicMock(spec=nio.RoomGetEventResponse)
    response.event = SimpleNamespace(
        event_id="$card",
        sender=sender,
        source={"type": _CARD_TYPE, "room_id": _ROOM, "content": {"approval_id": "card-1"}},
    )
    return response


@pytest.mark.asyncio
async def test_click_on_card_from_other_account_is_rejected() -> None:
    """A card event counts only when the account of the card's recorded sender sent it."""
    bots = {"other": _bot("other"), "code": _bot("code")}
    transport = _transport(bots, {"card-1": "code"})
    reader = bots["other"].client

    reader.room_get_event = AsyncMock(return_value=_card_event("@mindroom_other:example.org"))
    assert await transport.resolve_approval_action_delivery(_ROOM, "$card") is None

    reader.room_get_event = AsyncMock(return_value=_card_event("@mindroom_code:example.org"))
    assert await transport.resolve_approval_action_delivery(_ROOM, "$card") == "card-1"


@pytest.mark.asyncio
async def test_approval_works_in_room_with_one_agent_and_no_owner(
    sending_clients: list[nio.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One agent alone in a room prepares and sends its own card."""
    monkeypatch.setattr("mindroom.approval.transport.can_send_to_encrypted_room", lambda *_args, **_options: True)
    bots = {"code": _bot("code")}
    transport = _transport(bots, {"card-1": "code"})
    content = {"approval_id": "card-1", "body": "Approval required"}

    assert await transport.prepare_approval_event(_ROOM, None, content, "code") == content
    assert await transport.send_approval_delivery(_delivery()) == "$event-1"
    assert sending_clients == [bots["code"].client]


@pytest.mark.asyncio
async def test_restart_edits_card_through_its_recorded_sender(
    tmp_path: Path,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """After a restart the terminal edit of a decided card goes out through the account that sent the card."""
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    cards = journal.principal("router@shared")
    before = {"other": _bot("other"), "code": _bot("code")}
    after = {"other": _bot("other"), "code": _bot("code")}
    managers = [
        ApprovalManager(test_runtime_paths(tmp_path), transport=_transport(bots, cards=cards), cards=cards)
        for bots in (before, after)
    ]
    try:
        call = await _reserve_card(cards, "code")
        assert (await managers[0].recover_cards_on_startup()).complete
        await managers[0].shutdown()
        await cards.approvals.resolve_approval_call(call, requested_status="denied", reason="No.")

        assert (await managers[1].recover_cards_on_startup()).complete

        assert sending_clients == [before["code"].client, after["code"].client]
    finally:
        for manager in managers:
            await manager.shutdown()
        await journal.close()
