"""The asking account sends and edits its approval cards, and large argument evidence stays usable."""

import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
import pytest_asyncio

from mindroom.approval.manager import ApprovalManager, ToolApprovalTransportError, UnverifiableApprovalCardError
from mindroom.approval.recovery import ApprovalRecovery
from mindroom.approval.transport import ApprovalMatrixTransport, _offload_oversized_full_arguments
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalCardReservation,
    ApprovalDeliveryView,
    CallKey,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    MatrixDelivery,
)
from mindroom.matrix.large_messages import content_fits_normal_event
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import approval_manager_for_test, test_runtime_paths
from tests.journal_membership_helpers import admit_room_membership
from tests.reply_span_helpers import paused_for_approval

_ROOM = "!room:example.org"
_CARD_TYPE = "io.mindroom.tool_approval"
_CODE = "code@@mindroom_code:example.org"
_BOOKS = "router@@mindroom_router:example.org"


def _bot(name: str, *, device: str | None = None) -> MagicMock:
    """Return a running bot of one entity whose own client is joined to the room, which it was never invited to."""
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = f"@mindroom_{name}:example.org"
    client.device_id = device or f"{name}-device"
    client.rooms = {_ROOM: MagicMock()}
    return MagicMock(
        agent_name=name,
        running=True,
        client=client,
        latest_thread_event_id_if_needed=AsyncMock(return_value=None),
    )


def _transport(bots: dict[str, MagicMock], cards: ApprovalDeliveryView | None = None) -> ApprovalMatrixTransport:
    """Return the transport over these bots, reading card senders from ``cards`` or else knowing one card of code's."""
    if cards is None:
        cards = MagicMock(principal_id=_BOOKS)
        cards.approvals.approval_card_sender = AsyncMock(
            side_effect=lambda *, delivery_id: (_CODE, _ROOM) if delivery_id == "card-1" else None,
        )
    return ApprovalMatrixTransport(
        bots=lambda: bots,
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
    """Accept every send to a room the client may send to, and return the client each one went through."""
    used: list[nio.AsyncClient] = []

    async def send(client: nio.AsyncClient, room_id: str, *_event: object, **_options: object) -> nio.RoomSendResponse:
        used.append(client)
        return nio.RoomSendResponse(f"$event-{len(used)}", room_id)

    monkeypatch.setattr("mindroom.approval.transport.send_room_event_result", send)
    monkeypatch.setattr("mindroom.approval.transport.can_send_to_encrypted_room", lambda *_args, **_options: True)
    return used


@pytest.mark.asyncio
@pytest.mark.parametrize("edits_event_id", [None, "$card"])
async def test_card_is_sent_by_asking_agent(sending_clients: list[nio.AsyncClient], edits_event_id: str | None) -> None:
    """The bot of the account a card records sends the card and its terminal edit, with another bot in the room."""
    bots = {"other": _bot("other"), "code": _bot("code")}

    send, _resolve, device_id = await _transport(bots).pinned_delivery("card-1")
    await send(_delivery(edits_event_id=edits_event_id))

    assert sending_clients == [bots["code"].client]
    assert device_id == "code-device"


@pytest.mark.asyncio
async def test_delivery_goes_out_through_the_client_pinned_with_its_device(
    sending_clients: list[nio.AsyncClient],
) -> None:
    """A client that replaces the sender's after the device was named for the claim does not send the delivery."""
    bots = {"code": _bot("code", device="device-a")}
    pinned = bots["code"].client

    send, _resolve, device_id = await _transport(bots).pinned_delivery("card-1")
    bots["code"] = _bot("code", device="device-b")
    await send(_delivery())

    assert (device_id, sending_clients) == ("device-a", [pinned])


@pytest.mark.asyncio
async def test_sender_outside_the_room_or_without_a_bot_pins_nothing() -> None:
    """A sender whose client is not joined, or whose bot is being replaced, defers the delivery unclaimed."""
    bots = {"code": _bot("code")}
    bots["code"].client.rooms = {}
    transport = _transport(bots)

    assert transport.sender_bot(_CODE, _ROOM) is None
    with pytest.raises(ToolApprovalTransportError, match="not ready"):
        await transport.pinned_delivery("card-1")

    del bots["code"]
    with pytest.raises(ToolApprovalTransportError, match="not ready"):
        await transport.pinned_delivery("card-1")


@pytest.mark.asyncio
async def test_team_turn_card_is_sent_by_team_account(tmp_path: Path) -> None:
    """A team turn's card names the team account as its sender, not the member whose tool asks."""
    prepared_for: list[str] = []

    async def prepare(_room_id: str, _thread_id: str | None, content: dict, sender_entity: str) -> dict:
        prepared_for.append(sender_entity)
        return content

    manager = approval_manager_for_test(
        test_runtime_paths(tmp_path),
        prepare_approval_event=prepare,
        responder_principal_id=lambda entity: f"{entity}@@mindroom_{entity}:example.org",
    )
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
    assert card.sender_principal_id == "research_team@@mindroom_research_team:example.org"
    assert prepared_for == ["research_team"]


def _reservation(delivery_id: str = "card-1", tool_call_id: str = "call-1") -> ApprovalCardReservation:
    return ApprovalCardReservation(
        delivery_id=delivery_id,
        tool_call_id=tool_call_id,
        event_type=_CARD_TYPE,
        payload={"approval_id": delivery_id, "body": "Approval required"},
        approver_user_id="@alice:example.org",
        approvable=True,
        sender_principal_id=_CODE,
    )


async def _reserve_card(cards: ApprovalDeliveryView, card: ApprovalCardReservation | None = None) -> CallKey | None:
    """Reserve one detached card the code agent sends; None when the room refuses it."""
    card = card or _reservation()
    call = CallKey.detached("run-1", card.tool_call_id)
    reserved = await cards.approvals.reserve_detached_card(
        call=call,
        owner_kind="script",
        tool_name="shell",
        toolkit_name=None,
        invoking_agent="code",
        room_id=_ROOM,
        thread_id=None,
        expires_at_ns=time.time_ns() + 3_600_000_000_000,
        card=card,
    )
    return call if reserved else None


@pytest_asyncio.fixture
async def journal(tmp_path: Path) -> AsyncIterator[EventJournalStore]:
    """Open a fresh journal and close it after the test."""
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_card_row_records_sender_entity(journal: EventJournalStore) -> None:
    """The card row keeps the principal of the account that sends the card."""
    cards = journal.principal(_BOOKS)
    await _reserve_card(cards)

    assert await cards.approvals.approval_card_sender(delivery_id="card-1") == (_CODE, _ROOM)
    assert await cards.approvals.approval_card_sender(delivery_id="unknown") is None


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
    transport = _transport(bots)
    reader = bots["other"].client

    reader.room_get_event = AsyncMock(return_value=_card_event("@mindroom_other:example.org"))
    assert await transport.resolve_approval_action_delivery(_ROOM, "$card") is None

    reader.room_get_event = AsyncMock(return_value=_card_event("@mindroom_code:example.org"))
    assert await transport.resolve_approval_action_delivery(_ROOM, "$card") == "card-1"


@pytest.mark.asyncio
async def test_click_is_verified_by_the_sender_when_another_account_cannot_read_the_card() -> None:
    """An account that joined after the card cannot read it; the click still counts when the sender can."""
    forbidden = nio.RoomGetEventError("forbidden", status_code="M_FORBIDDEN")
    bots = {"other": _bot("other"), "code": _bot("code")}
    bots["other"].client.room_get_event = AsyncMock(return_value=forbidden)
    bots["code"].client.room_get_event = AsyncMock(return_value=_card_event("@mindroom_code:example.org"))
    transport = _transport(bots)

    assert await transport.resolve_approval_action_delivery(_ROOM, "$card") == "card-1"

    bots["code"].client.room_get_event = AsyncMock(return_value=forbidden)
    with pytest.raises(UnverifiableApprovalCardError):
        await transport.resolve_approval_action_delivery(_ROOM, "$card")


@pytest.mark.asyncio
async def test_click_is_verified_while_the_fleet_changes_under_the_readers() -> None:
    """A bot replaced while one account reads the event does not stop the card's sender from verifying it."""
    bots = {"other": _bot("other"), "code": _bot("code")}

    async def forbidden_after_a_reload(*_event: object) -> nio.RoomGetEventError:
        bots["late"] = _bot("late")
        return nio.RoomGetEventError("forbidden", status_code="M_FORBIDDEN")

    bots["other"].client.room_get_event = forbidden_after_a_reload
    bots["code"].client.room_get_event = AsyncMock(return_value=_card_event("@mindroom_code:example.org"))

    assert await _transport(bots).resolve_approval_action_delivery(_ROOM, "$card") == "card-1"


@pytest.mark.asyncio
async def test_approval_works_in_room_with_one_agent_and_no_owner(sending_clients: list[nio.AsyncClient]) -> None:
    """One agent alone in a room it was never invited to, as its personal room, prepares and sends its own card."""
    bots = {"code": _bot("code")}
    transport = _transport(bots)
    content = {"approval_id": "card-1", "body": "Approval required"}

    assert await transport.prepare_approval_event(_ROOM, None, content, "code") == content
    send, _resolve, _device_id = await transport.pinned_delivery("card-1")
    assert await send(_delivery()) == "$event-1"
    assert sending_clients == [bots["code"].client]


def _manager(tmp_path: Path, bots: dict[str, MagicMock], cards: ApprovalDeliveryView) -> ApprovalManager:
    return ApprovalManager(test_runtime_paths(tmp_path), transport=_transport(bots, cards=cards), cards=cards)


@pytest.mark.asyncio
async def test_restart_edits_card_through_its_recorded_sender(
    tmp_path: Path,
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """After a restart the terminal edit of a decided card goes out through the account that sent the card."""
    cards = journal.principal(_BOOKS)
    before = {"other": _bot("other"), "code": _bot("code")}
    after = {"other": _bot("other"), "code": _bot("code", device="code-device-2")}
    managers = [_manager(tmp_path, bots, cards) for bots in (before, after)]
    try:
        call = await _reserve_card(cards)
        assert call is not None
        assert (await managers[0].recover_cards_on_startup()).complete
        await managers[0].shutdown()
        await cards.approvals.resolve_approval_call(call, requested_status="denied", reason="No.")

        assert (await managers[1].recover_cards_on_startup()).complete

        assert sending_clients == [before["code"].client, after["code"].client]
    finally:
        for manager in managers:
            await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["stopped", "absent"])
async def test_recovery_without_the_senders_client_leaves_a_fresh_card_unattempted(
    tmp_path: Path,
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
    sender: str,
) -> None:
    """A sweep finding the card's sender still starting, or with no bot during a reload gap, claims nothing.

    The card is sent whole once the sender runs, or once the replacement bot is there.
    """
    cards = journal.principal(_BOOKS)
    bot = _bot("code")
    bot.running = sender != "stopped"
    bots = {} if sender == "absent" else {"code": bot}
    manager = _manager(tmp_path, bots, cards)
    try:
        await _reserve_card(cards)

        assert not (await manager.recover_cards_on_startup()).complete

        initial = await cards.load_matrix_delivery(delivery_id="card-1", stage=DeliveryStage.INITIAL)
        assert initial is not None
        assert not initial.attempted
        bot.running = True
        bots["code"] = bot
        assert (await manager.recover_cards_on_startup()).complete
        assert sending_clients == [bot.client]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_removed_owner_ends_once_after_its_card_edit_failed_before_the_removal(
    tmp_path: Path,
    journal: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent removed from the config, whose card edit failed while it still ran, ends its paused turn once."""
    cards = journal.principal(_BOOKS)
    owner = journal.principal(_CODE)
    bots = {"code": _bot("code"), "other": _bot("other")}
    bots["other"].journal_principal.return_value = journal.principal("other@@mindroom_other:example.org")
    configured = {"code", "other"}
    sent: list[tuple[nio.AsyncClient, dict]] = []

    async def send(client: nio.AsyncClient, room_id: str, _type: str, content: dict, **_options: object) -> object:
        if "m.new_content" in content:
            return nio.RoomSendError(message="Matrix temporarily unavailable")
        sent.append((client, content))
        return nio.RoomSendResponse(f"$event-{len(sent)}", room_id)

    monkeypatch.setattr("mindroom.approval.transport.send_room_event_result", send)
    monkeypatch.setattr("mindroom.approval.transport.can_send_to_encrypted_room", lambda *_args, **_options: True)
    manager = _manager(tmp_path, bots, cards)
    recovery = ApprovalRecovery(
        deliver_unavailable_notice=manager.transport.deliver_unavailable_notice,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name in configured,
    )
    recovery.bind(manager)
    await owner.admit(
        InboundEvent(
            event_id="$source",
            room_id=_ROOM,
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@alice:example.org",
            origin_server_ts=1_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run it"}},
        ),
    )
    call = ApprovalCall(tool_call_id="call-1", tool_name="shell", invoking_agent="code", expires_at_ns=9 * 10**18)
    paused = approval_continuation(
        approval_id="approval-1",
        entity_name="code",
        room_id=_ROOM,
        thread_id=None,
        calls=(call,),
        state="waiting",
        runtime_generation="runtime-a",
    )
    await paused_for_approval(owner, paused)
    try:
        card = replace(
            _reservation(),
            payload={
                "approval_id": "card-1",
                "continuation_id": "approval-1",
                "continuation_generation": 0,
                "tool_call_id": "call-1",
            },
        )
        assert await manager.reserve_and_publish(
            continuation_principal_id=_CODE,
            continuation_id="approval-1",
            continuation_generation=0,
            cards=(card,),
        )
        # The agent still runs when its removal first settles it, and Matrix refuses the edit for now.
        assert await recovery.reconcile_removed_entities({"code"}) == {"code"}

        del bots["code"]
        # No bot while the config still lists the agent is a gap, not a removal: the edit stays owed.
        assert await recovery.reconcile_removed_entities({"code"}) == {"code"}
        configured.discard("code")
        for _ in range(2):
            await recovery.reconcile_unavailable_entities({"code"})

        assert await owner.approvals.approval_continuation("approval-1") is None
        assert [(client, content["msgtype"]) for client, content in sent[1:]] == [(bots["other"].client, "m.notice")]
    finally:
        await recovery.close()
        await manager.shutdown()


def _notice_transport(journal: EventJournalStore, bots: dict[str, MagicMock], *posters: str) -> ApprovalMatrixTransport:
    """Return the transport whose room poster is each of ``posters`` in turn."""
    for name, bot in bots.items():
        bot.journal_principal.return_value = journal.principal(f"{name}@@mindroom_{name}:example.org")
    elected = iter(posters)
    return ApprovalMatrixTransport(
        bots=lambda: bots,
        room_poster=lambda _room_id: bots.get(next(elected)),
        cards=lambda: journal.principal(_BOOKS),
    )


_PAUSED = SimpleNamespace(approval_id="approval-1", room_id=_ROOM, thread_id=None, response_event_id="$reply")
_GONE = "Requesting agent 'gone' is no longer available."


@pytest.mark.asyncio
async def test_unavailable_notice_keeps_its_first_poster(
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """A retry after the room's poster changed reuses the notice the first poster sent and posts no second one."""
    bots = {"code": _bot("code"), "other": _bot("other")}
    transport = _notice_transport(journal, bots, "code", "other")

    assert await transport.deliver_unavailable_notice(_PAUSED, _GONE)
    assert await transport.deliver_unavailable_notice(_PAUSED, _GONE)

    assert sending_clients == [bots["code"].client]


@pytest.mark.asyncio
async def test_unavailable_notice_is_reused_after_its_poster_leaves(
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """A notice its poster sent stays sent once that poster left the room and its bot is gone."""
    bots = {"code": _bot("code"), "other": _bot("other")}
    transport = _notice_transport(journal, bots, "code", "other")
    code = journal.principal(_CODE)
    await admit_room_membership(code, _ROOM, "join")
    assert await transport.deliver_unavailable_notice(_PAUSED, _GONE)

    await admit_room_membership(code, _ROOM, "leave")
    poster = bots.pop("code").client

    assert await transport.deliver_unavailable_notice(_PAUSED, _GONE)
    assert sending_clients == [poster]


@pytest.mark.asyncio
async def test_unavailable_notice_never_sent_falls_to_the_next_poster(
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """A notice its first poster left without sending is posted once by the room's next poster."""
    bots = {"code": _bot("code"), "other": _bot("other")}
    transport = _notice_transport(journal, bots, "other")
    code = journal.principal(_CODE)
    await admit_room_membership(code, _ROOM, "join")
    assert await code.approvals.enqueue_unavailable_approval_notice(
        approval_id="approval-1",
        room_id=_ROOM,
        thread_id=None,
        payload={"body": _GONE},
    )

    await admit_room_membership(code, _ROOM, "leave")

    assert await transport.deliver_unavailable_notice(_PAUSED, _GONE)
    assert sending_clients == [bots["other"].client]


@pytest.mark.asyncio
async def test_owner_departure_leaves_another_accounts_cards_alone(
    tmp_path: Path,
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """The owner account leaving a room decides no card an agent sent there, and the card is still sent and edited."""
    books = journal.principal(_BOOKS)
    bots = {"code": _bot("code")}
    manager = _manager(tmp_path, bots, books)
    await admit_room_membership(journal.principal(_CODE), _ROOM, "join")
    await admit_room_membership(books, _ROOM, "join")
    try:
        call = await _reserve_card(books)
        assert call is not None

        await admit_room_membership(books, _ROOM, "leave")

        assert await books.approvals.detached_decision(call) is None
        assert (await manager.recover_cards_on_startup()).complete
        pending = await books.approvals.pending_approval_cards(room_id=_ROOM, limit=10)
        assert [(card.delivery_id, card.current, card.card_event_id) for card in pending] == [
            ("card-1", True, "$event-1"),
        ]
        assert await _reserve_card(books, _reservation("card-2", "call-2")) is not None
        await books.approvals.resolve_approval_call(call, requested_status="denied", reason="No.")
        assert (await manager.recover_cards_on_startup()).complete
        assert sending_clients == [bots["code"].client] * 3
        # The ledger keeps its one epoch when the owner account comes back.
        assert await admit_room_membership(books, _ROOM, "join") == 0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_card_of_a_sender_that_rejoined_is_sent_and_acknowledged(
    tmp_path: Path,
    journal: EventJournalStore,
    sending_clients: list[nio.AsyncClient],
) -> None:
    """A sender in its second membership of a room the owner account never left still gets its card sent."""
    books = journal.principal(_BOOKS)
    code = journal.principal(_CODE)
    bots = {"code": _bot("code")}
    manager = _manager(tmp_path, bots, books)
    for membership in ("join", "leave", "join"):
        await admit_room_membership(code, _ROOM, membership)
    try:
        assert await _reserve_card(books) is not None

        assert (await manager.recover_cards_on_startup()).complete

        [card] = await books.approvals.pending_approval_cards(room_id=_ROOM, limit=10)
        assert (card.current, card.card_event_id) == (True, "$event-1")
        assert sending_clients == [bots["code"].client]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_sender_departure_denies_its_own_cards(journal: EventJournalStore) -> None:
    """The agent that sent a card leaving the room denies the card and refuses its next one."""
    books = journal.principal(_BOOKS)
    code = journal.principal(_CODE)
    await admit_room_membership(code, _ROOM, "join")
    call = await _reserve_card(books)
    assert call is not None

    await admit_room_membership(code, _ROOM, "leave")

    decision = await books.approvals.detached_decision(call)
    assert decision is not None
    assert decision.status == "denied"
    assert await _reserve_card(books, _reservation("card-2", "call-2")) is None


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
