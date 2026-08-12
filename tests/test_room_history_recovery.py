"""Durable room-history recovery obligations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, Self

import nio
import pytest

from mindroom.event_journal import (
    ConversationCursor,
    DepartureSource,
    HistoryRecoveryOutcome,
    HistoryRecoveryState,
    ProjectedEvent,
    RoomHistoryRecovery,
)
from mindroom.matrix import conversation_hydration
from mindroom.matrix.conversation_hydration import (
    _MESSAGES_PAGE_LIMIT,
    ConversationHydrator,
    _HydrationError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOT = "@mindroom_general:example.org"


@pytest.fixture
def principal(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one principal bound to each supported journal backend."""
    return journal_store.principal("agent@alice")


def projected(event_id: str, body: str, *, ts: int, thread_id: str | None = None) -> ProjectedEvent:
    """Build one visible message without involving Matrix transport types."""
    return ProjectedEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        sender=ALICE,
        origin_server_ts=ts,
        content={"msgtype": "m.text", "body": body},
        replaces_event_id=None,
        redacts_event_id=None,
    )


def raw(
    event_id: str,
    body: str,
    *,
    ts: int,
    thread_id: str | None = None,
    replaces: str | None = None,
) -> dict[str, Any]:
    """Build one raw Matrix message event."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if thread_id is not None:
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_id,
        }
    if replaces is not None:
        content["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": replaces,
        }
        content["m.new_content"] = {
            "msgtype": "m.text",
            "body": body,
        }
    return {
        "event_id": event_id,
        "sender": ALICE,
        "origin_server_ts": ts,
        "type": "m.room.message",
        "content": content,
    }


def parse(source: dict[str, Any]) -> nio.Event:
    """Parse one raw source the way nio parses a history response."""
    event = nio.Event.parse_event(source)
    assert isinstance(event, nio.Event)
    return event


@dataclass
class PagedClient:
    """A homeserver serving explicit backwards-history pages."""

    pages: list[tuple[list[dict[str, Any]], str | None] | nio.RoomMessagesError]
    calls: int = 0
    requested_limits: list[int] = field(default_factory=list)
    olm: object | None = None

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        direction: object = None,
        limit: int = 10,
    ) -> nio.RoomMessagesResponse | nio.RoomMessagesError:
        """Return the next configured page."""
        del room_id, start, direction
        self.requested_limits.append(limit)
        page = self.pages[self.calls]
        self.calls += 1
        if isinstance(page, nio.RoomMessagesError):
            return page
        sources, end = page
        return nio.RoomMessagesResponse(ROOM, [parse(source) for source in sources], "start", end)


@dataclass
class LazyEditedHistoryClient:
    """Generate edit-heavy backwards history one bounded page at a time."""

    total_events: int
    before_page: Callable[[int], Awaitable[None]] | None = None
    calls: int = 0
    generated_events: int = 0
    olm: object | None = None

    @staticmethod
    def _source(index: int) -> dict[str, Any]:
        logical_index = index // 5
        position = index % 5
        original_event_id = f"$message-{logical_index}"
        base_ts = 1_000_000 - logical_index * 10
        if position == 4:
            return raw(
                original_event_id,
                f"message {logical_index}",
                ts=base_ts,
            )
        revision = 4 - position
        return raw(
            f"$edit-{logical_index}-{revision}",
            f"message {logical_index} edit {revision}",
            ts=base_ts + revision,
            replaces=original_event_id,
        )

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        direction: object = None,
        limit: int = 10,
    ) -> nio.RoomMessagesResponse:
        """Build only the page requested by the current recovery iteration."""
        del room_id, start, direction
        self.calls += 1
        if self.before_page is not None:
            await self.before_page(self.calls)
        page_size = min(limit, self.total_events - self.generated_events)
        first = self.generated_events
        sources = [self._source(index) for index in range(first, first + page_size)]
        self.generated_events += page_size
        end = None if self.generated_events == self.total_events else f"page-{self.calls}"
        return nio.RoomMessagesResponse(ROOM, [parse(source) for source in sources], "start", end)


@dataclass
class RecordingRecoveryStore:
    """Record recovery API boundaries while delegating durable work."""

    principal: PrincipalStore
    fail_before_chunk: int | None = None
    installed_batch_sizes: list[int] = field(default_factory=list)
    settlements: int = 0

    async def room_history_recovery(self, room_id: str) -> RoomHistoryRecovery | None:
        """Delegate recovery lookup."""
        return await self.principal.room_history_recovery(room_id)

    async def membership_epoch(self, room_id: str) -> int:
        """Delegate membership lookup."""
        return await self.principal.membership_epoch(room_id)

    async def install_room_history_recovery_chunk(
        self,
        recovery: RoomHistoryRecovery,
        *,
        events: tuple[ProjectedEvent, ...],
        expected_membership_epoch: int,
    ) -> bool:
        """Record one bounded batch and optionally crash before installing it."""
        next_chunk = len(self.installed_batch_sizes) + 1
        if self.fail_before_chunk == next_chunk:
            msg = "injected recovery installation crash"
            raise RuntimeError(msg)
        self.installed_batch_sizes.append(len(events))
        return await self.principal.install_room_history_recovery_chunk(
            recovery,
            events=events,
            expected_membership_epoch=expected_membership_epoch,
        )

    async def settle_room_history_recovery(
        self,
        recovery: RoomHistoryRecovery,
        *,
        exhausted_server: bool,
        attempted_policy_rank: int,
        expected_membership_epoch: int,
    ) -> HistoryRecoveryOutcome:
        """Record terminal settlement without accepting a recovered event tuple."""
        self.settlements += 1
        return await self.principal.settle_room_history_recovery(
            recovery,
            exhausted_server=exhausted_server,
            attempted_policy_rank=attempted_policy_rank,
            expected_membership_epoch=expected_membership_epoch,
        )


class CountingProjectedEvent(ProjectedEvent):
    """Track how many projected recovery events remain live simultaneously."""

    __slots__ = ()

    live: ClassVar[int] = 0
    peak: ClassVar[int] = 0

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """Count one projected event when the hydrator constructs it."""
        del args, kwargs
        instance = object.__new__(cls)
        cls.live += 1
        cls.peak = max(cls.peak, cls.live)
        return instance

    def __del__(self) -> None:
        """Release the count when no page or accumulator retains this event."""
        type(self).live -= 1


def hydrator(
    principal: PrincipalStore,
    client: PagedClient,
    **bounds: int,
) -> ConversationHydrator:
    """Build a hydrator over the real store and explicit fake server."""
    return ConversationHydrator(
        store=principal,
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=BOT,
        **bounds,
    )


async def stored_recovery_row(principal: PrincipalStore) -> dict[str, Any] | None:
    """Read the persistence representation whose determinism is contractual."""
    row = await principal._backend.read(
        lambda transaction: transaction.fetchone(
            """
            SELECT state, revision FROM room_history_recovery
            WHERE principal_id = ? AND room_id = ?
            """,
            (principal._principal_id, ROOM),
        ),
    )
    return None if row is None else dict(row)


async def mark_complete(principal: PrincipalStore, thread_id: str | None) -> None:
    """Install a complete hydration marker under the room's current membership."""
    assert await principal.install_hydrated_conversation(
        room_id=ROOM,
        thread_id=thread_id,
        events=(),
        complete=True,
        attempted_policy_rank=2,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )


async def bodies(principal: PrincipalStore, thread_id: str | None = None) -> list[str]:
    """Return the room's visible message bodies, oldest first."""
    page = await principal.read_conversation(room_id=ROOM, thread_id=thread_id, limit=50)
    return [str(message.content["body"]) for message in page.messages]


async def visible_message_count(principal: PrincipalStore) -> int:
    """Return the number of projected logical messages in the test room."""
    row = await principal._backend.read(
        lambda transaction: transaction.fetchone(
            """
            SELECT count(*) AS count FROM visible_messages
            WHERE principal_id = ? AND room_id = ?
            """,
            (principal._principal_id, ROOM),
        ),
    )
    assert row is not None
    return int(row["count"])


async def install_recovery_events(
    principal: PrincipalStore,
    recovery: RoomHistoryRecovery,
    events: tuple[ProjectedEvent, ...],
    *,
    expected_membership_epoch: int | None = None,
) -> bool:
    """Install one test-sized recovery batch through both durable fences."""
    epoch = (
        await principal.membership_epoch(recovery.room_id)
        if expected_membership_epoch is None
        else expected_membership_epoch
    )
    return await principal.install_room_history_recovery_chunk(
        recovery,
        events=events,
        expected_membership_epoch=epoch,
    )


async def test_repair_streams_twenty_thousand_edit_heavy_events_in_page_batches(
    principal: PrincipalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A maximum proof walk must never become one maximum-sized Python tuple."""
    CountingProjectedEvent.live = 0
    CountingProjectedEvent.peak = 0
    monkeypatch.setattr(conversation_hydration, "ProjectedEvent", CountingProjectedEvent)
    recovery = await principal.record_room_history_recovery(ROOM)
    assert recovery is not None
    client = LazyEditedHistoryClient(total_events=20_000)
    recording = RecordingRecoveryStore(principal)
    repair = ConversationHydrator(
        store=recording,  # type: ignore[arg-type]
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=BOT,
    )

    outcome = await repair._repair(recovery)

    assert outcome is HistoryRecoveryOutcome.REPAIRED
    assert client.generated_events == 20_000
    assert len(recording.installed_batch_sizes) == 200
    assert max(recording.installed_batch_sizes) <= _MESSAGES_PAGE_LIMIT
    assert CountingProjectedEvent.peak <= _MESSAGES_PAGE_LIMIT * 2
    assert recording.settlements == 1
    assert await visible_message_count(principal) == 4_000
    page = await principal.read_conversation(room_id=ROOM, thread_id=None, limit=10)
    assert all(str(message.content["body"]).endswith("edit 4") for message in page.messages)
    assert await principal.room_history_recovery(ROOM) is None


async def test_recovery_crash_keeps_obligation_open_and_retry_converges(
    principal: PrincipalStore,
) -> None:
    """Committed recovery pages are safe to re-fetch after an interrupted walk."""
    recovery = await principal.record_room_history_recovery(ROOM)
    assert recovery is not None
    crashing_store = RecordingRecoveryStore(principal, fail_before_chunk=4)
    crashing_client = LazyEditedHistoryClient(total_events=500)
    crashing_repair = ConversationHydrator(
        store=crashing_store,  # type: ignore[arg-type]
        runtime=SimpleNamespace(client=crashing_client),  # type: ignore[arg-type]
        self_sender=BOT,
    )

    with pytest.raises(RuntimeError, match="injected recovery installation crash"):
        await crashing_repair._repair(recovery)

    assert crashing_store.installed_batch_sizes == [100, 100, 100]
    assert crashing_store.settlements == 0
    assert await principal.room_history_recovery(ROOM) == recovery
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    assert await visible_message_count(principal) == 60

    retry_store = RecordingRecoveryStore(principal)
    retry_client = LazyEditedHistoryClient(total_events=500)
    retry = ConversationHydrator(
        store=retry_store,  # type: ignore[arg-type]
        runtime=SimpleNamespace(client=retry_client),  # type: ignore[arg-type]
        self_sender=BOT,
    )

    outcome = await retry._repair(recovery)

    assert outcome is HistoryRecoveryOutcome.REPAIRED
    assert retry_store.installed_batch_sizes == [100, 100, 100, 100, 100]
    assert retry_store.settlements == 1
    assert await visible_message_count(principal) == 100
    page = await principal.read_conversation(room_id=ROOM, thread_id=None, limit=100)
    assert len(page.messages) == 100
    assert all(str(message.content["body"]).endswith("edit 4") for message in page.messages)


async def test_membership_epoch_change_stops_recovery_before_final_settlement(
    principal: PrincipalStore,
) -> None:
    """A page fetched after departure cannot enter or publish the new membership."""

    async def fence_before_second_page(page_number: int) -> None:
        if page_number == 2:
            await principal.fence_departure(ROOM, source=DepartureSource.LOCAL)

    recovery = await principal.record_room_history_recovery(ROOM)
    assert recovery is not None
    client = LazyEditedHistoryClient(total_events=300, before_page=fence_before_second_page)
    recording = RecordingRecoveryStore(principal)
    repair = ConversationHydrator(
        store=recording,  # type: ignore[arg-type]
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=BOT,
    )

    outcome = await repair._repair(recovery)

    assert outcome is HistoryRecoveryOutcome.SUPERSEDED
    assert client.calls == 2
    assert recording.installed_batch_sizes == [100, 100]
    assert recording.settlements == 0
    assert await principal.room_history_recovery(ROOM) is None
    assert await visible_message_count(principal) == 0
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_new_recovery_revision_stops_stale_walk_before_final_settlement(
    principal: PrincipalStore,
) -> None:
    """A later gap prevents an older walk from installing another page."""
    newer: RoomHistoryRecovery | None = None

    async def supersede_before_second_page(page_number: int) -> None:
        nonlocal newer
        if page_number == 2:
            newer = await principal.record_room_history_recovery(ROOM)

    recovery = await principal.record_room_history_recovery(ROOM)
    assert recovery is not None
    client = LazyEditedHistoryClient(total_events=300, before_page=supersede_before_second_page)
    recording = RecordingRecoveryStore(principal)
    repair = ConversationHydrator(
        store=recording,  # type: ignore[arg-type]
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=BOT,
    )

    outcome = await repair._repair(recovery)

    assert outcome is HistoryRecoveryOutcome.SUPERSEDED
    assert client.calls == 2
    assert recording.installed_batch_sizes == [100, 100]
    assert recording.settlements == 0
    assert newer is not None
    assert newer.revision == recovery.revision + 1
    assert await principal.room_history_recovery(ROOM) == newer
    assert await visible_message_count(principal) == 20
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_repair_ignores_a_post_gap_first_page_and_walks_to_server_exhaustion(
    principal: PrincipalStore,
) -> None:
    """A full prompt window in the live tail cannot hide the missing middle behind it."""
    client = PagedClient(
        pages=[
            ([raw("$tail", "tail", ts=3_000)], "page-two"),
            ([raw("$missing", "missing", ts=2_000)], None),
        ],
    )
    await principal.record_room_history_recovery(ROOM)

    await hydrator(principal, client, prompt_window_messages=1).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert client.calls == 2
    assert await bodies(principal) == ["missing", "tail"]
    assert await principal.room_history_recovery(ROOM) is None
    assert await principal.conversation_is_complete(room_id=ROOM, thread_id=None)


async def test_concurrent_room_and_thread_readers_share_one_recovery_walk(
    principal: PrincipalStore,
) -> None:
    """Every conversation behind one room obligation joins the same proof walk."""
    thread_id = "$root"
    await mark_complete(principal, None)
    await mark_complete(principal, thread_id)
    await principal.record_room_history_recovery(ROOM)
    client = PagedClient(
        pages=[
            (
                [
                    raw("$reply", "reply", ts=2_000, thread_id=thread_id),
                    raw(thread_id, "root", ts=1_000),
                ],
                None,
            ),
        ],
    )
    repair = hydrator(principal, client)

    await asyncio.gather(
        repair.ensure_hydrated(room_id=ROOM, thread_id=None),
        repair.ensure_hydrated(room_id=ROOM, thread_id=thread_id),
        repair.ensure_hydrated(room_id=ROOM, thread_id=thread_id),
    )

    assert client.calls == 1
    assert await principal.room_history_recovery(ROOM) is None
    assert await bodies(principal, thread_id) == ["root", "reply"]


async def test_reader_holding_a_settled_recovery_does_not_walk_again(
    principal: PrincipalStore,
) -> None:
    """The exact durable obligation stops a late reader from paying twice."""
    recovery = await principal.record_room_history_recovery(ROOM)
    client = PagedClient(pages=[([raw("$one", "one", ts=1_000)], None)])
    repair = hydrator(principal, client)

    await repair.ensure_hydrated(room_id=ROOM, thread_id=None)
    assert client.calls == 1

    await repair._repair(recovery)

    assert client.calls == 1
    assert await bodies(principal) == ["one"]


async def test_unreadable_server_exhaustion_fails_without_installing(principal: PrincipalStore) -> None:
    """Exhaustion proves nothing when the walk could not read every fetched event."""
    encrypted = {
        "event_id": "$encrypted",
        "sender": ALICE,
        "origin_server_ts": 1_000,
        "type": "m.room.encrypted",
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "ciphertext",
            "sender_key": "sender-key",
            "session_id": "session",
            "device_id": "DEVICE",
        },
    }
    client = PagedClient(pages=[([encrypted], None)])
    recovery = await principal.record_room_history_recovery(ROOM)

    with pytest.raises(_HydrationError, match="unreadable"):
        await hydrator(principal, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await principal.room_history_recovery(ROOM) == recovery
    assert await bodies(principal) == []
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_bad_event_at_server_exhaustion_stays_repairable(principal: PrincipalStore) -> None:
    """A malformed event is unreadable evidence, not proof that the room is whole."""
    malformed = nio.Event.parse_event({"event_id": "$bad", "type": "m.room.message"})
    assert not isinstance(malformed, nio.Event)

    @dataclass
    class BadEventClient(PagedClient):
        async def room_messages(
            self,
            room_id: str,
            start: str | None = None,
            direction: object = None,
            limit: int = 10,
        ) -> nio.RoomMessagesResponse:
            del room_id, start, direction, limit
            self.calls += 1
            return nio.RoomMessagesResponse(ROOM, [malformed], "start", None)  # type: ignore[list-item]

    recovery = await principal.record_room_history_recovery(ROOM)

    with pytest.raises(_HydrationError, match="unreadable"):
        await hydrator(principal, BadEventClient(pages=[])).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await principal.room_history_recovery(ROOM) == recovery
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_repair_pagination_error_leaves_the_obligation_repairable(principal: PrincipalStore) -> None:
    """A failed request may leave retry-safe pages but cannot publish them as whole."""
    client = PagedClient(
        pages=[
            ([raw("$partial", "partial", ts=2_000)], "next"),
            nio.RoomMessagesError("M_FORBIDDEN"),
        ],
    )
    recovery = await principal.record_room_history_recovery(ROOM)

    with pytest.raises(_HydrationError, match="Could not fetch history"):
        await hydrator(principal, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert await principal.room_history_recovery(ROOM) == recovery
    assert await bodies(principal) == ["partial"]
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_repair_repeated_token_leaves_the_obligation_repairable(principal: PrincipalStore) -> None:
    """A stalled continuation token is not server exhaustion."""
    client = PagedClient(pages=[([], "same"), ([], "same")])
    recovery = await principal.record_room_history_recovery(ROOM)

    with pytest.raises(_HydrationError, match="repeated pagination token"):
        await hydrator(principal, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert client.calls == 2
    assert await principal.room_history_recovery(ROOM) == recovery
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_repair_ceiling_truncates_and_a_fresh_hydrator_does_not_walk_again(
    principal: PrincipalStore,
) -> None:
    """A cost ceiling preserves its readable prefix and prevents an unbounded read tax."""
    client = PagedClient(
        pages=[
            ([raw("$recent", "recent", ts=3_000)], "more"),
            ([raw("$must-not-fetch", "older", ts=2_000)], None),
        ],
    )
    await principal.record_room_history_recovery(ROOM)

    await hydrator(principal, client, max_requests=1).ensure_hydrated(room_id=ROOM, thread_id=None)

    recovery = await principal.room_history_recovery(ROOM)
    assert recovery is not None
    assert recovery.state is HistoryRecoveryState.TRUNCATED
    assert client.calls == 1
    assert await bodies(principal) == ["recent"]
    assert await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    assert not await principal.conversation_is_complete(room_id=ROOM, thread_id=None)

    await hydrator(principal, client, max_requests=1).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert client.calls == 1


async def test_repair_raw_event_ceiling_stops_inside_a_page(principal: PrincipalStore) -> None:
    """The raw-event ceiling limits a final page before projection installs it."""
    client = PagedClient(
        pages=[
            (
                [
                    raw("$newest", "newest", ts=3_000),
                    raw("$older", "older", ts=2_000),
                ],
                "more",
            ),
        ],
    )
    await principal.record_room_history_recovery(ROOM)

    await hydrator(principal, client, max_fetched_events=1).ensure_hydrated(room_id=ROOM, thread_id=None)

    recovery = await principal.room_history_recovery(ROOM)
    assert recovery is not None
    assert recovery.state is HistoryRecoveryState.TRUNCATED
    assert client.calls == 1
    assert client.requested_limits == [1]
    assert await bodies(principal) == ["newest"]
    assert await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    assert not await principal.conversation_is_complete(room_id=ROOM, thread_id=None)


async def test_membership_movement_during_repair_installs_nothing(principal: PrincipalStore) -> None:
    """A proof fetched under an ended membership cannot enter the new projection."""

    @dataclass
    class MovingMembershipClient(PagedClient):
        async def room_messages(
            self,
            room_id: str,
            start: str | None = None,
            direction: object = None,
            limit: int = 10,
        ) -> nio.RoomMessagesResponse | nio.RoomMessagesError:
            await principal.fence_departure(ROOM, source=DepartureSource.LOCAL)
            return await super().room_messages(room_id, start, direction, limit)

    client = MovingMembershipClient(pages=[([raw("$stale", "stale", ts=1_000)], None)])
    recovery = await principal.record_room_history_recovery(ROOM)

    await hydrator(principal, client)._repair(recovery)

    assert await principal.room_history_recovery(ROOM) is None
    assert await bodies(principal) == []
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_late_unknown_signal_after_departure_repairs_the_next_membership(
    principal: PrincipalStore,
) -> None:
    """A stale Classic signal may over-repair the next epoch but cannot certify a hole."""
    await mark_complete(principal, None)
    old_epoch = await principal.membership_epoch(ROOM)
    await principal.fence_departure(ROOM, source=DepartureSource.LOCAL)
    assert await principal.membership_epoch(ROOM) == old_epoch + 1
    await principal.note_membership_restarted(ROOM)

    recovery = await principal.record_room_history_recovery(ROOM)
    client = PagedClient(pages=[([raw("$current", "current", ts=2_000)], None)])

    await hydrator(principal, client).ensure_hydrated(room_id=ROOM, thread_id=None)

    assert recovery.revision == 0
    assert client.calls == 1
    assert await principal.room_history_recovery(ROOM) is None
    assert await bodies(principal) == ["current"]
    assert await principal.conversation_is_complete(room_id=ROOM, thread_id=None)


async def test_unknown_signal_while_departure_is_fenced_is_a_no_op(
    principal: PrincipalStore,
) -> None:
    """A gap from an ended membership cannot create work until a join is confirmed."""
    await principal.fence_departure(ROOM, source=DepartureSource.LOCAL)

    recovery = await principal.record_room_history_recovery(ROOM)

    assert recovery is None
    assert await principal.room_history_recovery(ROOM) is None


async def test_recording_creates_a_repairable_unknown_obligation(principal: PrincipalStore) -> None:
    """Classic sync can honestly persist only the fact that a room gap exists."""
    recovery = await principal.record_room_history_recovery(ROOM)

    assert recovery == RoomHistoryRecovery(
        room_id=ROOM,
        state=HistoryRecoveryState.REPAIRABLE,
        revision=0,
    )
    assert await principal.room_history_recovery(ROOM) == recovery
    assert await stored_recovery_row(principal) == {
        "state": "repairable",
        "revision": 0,
    }


async def test_an_empty_projection_still_creates_an_obligation(principal: PrincipalStore) -> None:
    """No visible rows cannot prove that a skipped interval contained no state."""
    recovery = await principal.record_room_history_recovery(ROOM)

    assert recovery.state is HistoryRecoveryState.REPAIRABLE
    assert await principal.room_history_recovery(ROOM) == recovery


async def test_recording_retracts_completeness_for_the_room_and_all_threads(
    principal: PrincipalStore,
) -> None:
    """No pre-gap room or thread marker may continue calling itself whole."""
    conversations = (None, "$thread-one", "$thread-two")
    for thread_id in conversations:
        await mark_complete(principal, thread_id)
        assert await principal.conversation_is_complete(room_id=ROOM, thread_id=thread_id)

    recovery = await principal.record_room_history_recovery(ROOM)

    for thread_id in conversations:
        assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=thread_id)

    outcome = await principal.settle_room_history_recovery(
        recovery,
        exhausted_server=False,
        attempted_policy_rank=3,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert outcome is HistoryRecoveryOutcome.TRUNCATED
    for thread_id in conversations:
        assert await principal.conversation_is_hydrated(room_id=ROOM, thread_id=thread_id)
        assert not await principal.conversation_is_complete(room_id=ROOM, thread_id=thread_id)
        coverage = await principal.conversation_hydration_coverage(room_id=ROOM, thread_id=thread_id)
        assert coverage is not None
        assert not coverage.reached_its_end
        assert await principal.conversation_hydration_was_truncated(room_id=ROOM, thread_id=thread_id)


async def test_truncated_obligation_leaves_bounded_context_readable_but_incomplete(
    principal: PrincipalStore,
) -> None:
    """A spent recovery ceiling exposes context without certifying completeness."""
    await mark_complete(principal, None)
    recovery = await principal.record_room_history_recovery(ROOM)
    assert await install_recovery_events(
        principal,
        recovery,
        (projected("$new", "new", ts=2_000),),
    )
    outcome = await principal.settle_room_history_recovery(
        recovery,
        exhausted_server=False,
        attempted_policy_rank=3,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert outcome is HistoryRecoveryOutcome.TRUNCATED
    assert (await principal.room_history_recovery(ROOM)).state is HistoryRecoveryState.TRUNCATED  # type: ignore[union-attr]
    assert await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)
    assert not await principal.conversation_is_complete(room_id=ROOM, thread_id=None)
    coverage = await principal.conversation_hydration_coverage(room_id=ROOM, thread_id=None)
    assert coverage is not None
    assert not coverage.reached_its_end
    assert await principal.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)


async def test_a_new_abandonment_resets_truncated_to_repairable(principal: PrincipalStore) -> None:
    """A new skipped interval needs a new walk even when its cause repeats."""
    recovery = await principal.record_room_history_recovery(ROOM)
    assert (
        await principal.settle_room_history_recovery(
            recovery,
            exhausted_server=False,
            attempted_policy_rank=1,
            expected_membership_epoch=await principal.membership_epoch(ROOM),
        )
        is HistoryRecoveryOutcome.TRUNCATED
    )

    repeated = await principal.record_room_history_recovery(ROOM)

    assert repeated == RoomHistoryRecovery(
        room_id=ROOM,
        state=HistoryRecoveryState.REPAIRABLE,
        revision=1,
    )
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_settlement_publishes_only_after_paginated_events_and_repairs_obligation(
    principal: PrincipalStore,
) -> None:
    """Only the final commit publishes coverage and clears the exact obligation."""
    recovery = await principal.record_room_history_recovery(ROOM)
    events = tuple(projected(f"${index}", str(index), ts=index) for index in range(1, 6))
    assert await install_recovery_events(principal, recovery, events)

    outcome = await principal.settle_room_history_recovery(
        recovery,
        exhausted_server=True,
        attempted_policy_rank=4,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert outcome is HistoryRecoveryOutcome.REPAIRED
    assert await principal.room_history_recovery(ROOM) is None
    assert await principal.conversation_is_complete(room_id=ROOM, thread_id=None)
    first = await principal.read_conversation(room_id=ROOM, thread_id=None, limit=2)
    assert [message.content["body"] for message in first.messages] == ["4", "5"]
    assert first.next_cursor == ConversationCursor(created_ts=4, logical_event_id="$4")
    second = await principal.read_conversation(
        room_id=ROOM,
        thread_id=None,
        limit=2,
        before=first.next_cursor,
    )
    assert [message.content["body"] for message in second.messages] == ["2", "3"]


async def test_successful_room_repair_restores_existing_thread_completeness(
    principal: PrincipalStore,
) -> None:
    """A room-wide proof makes pre-gap thread coverage trustworthy again."""
    thread_id = "$thread"
    await mark_complete(principal, thread_id)
    recovery = await principal.record_room_history_recovery(ROOM)
    assert recovery is not None
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=thread_id)
    assert await install_recovery_events(
        principal,
        recovery,
        (
            projected(thread_id, "root", ts=1),
            projected("$reply", "reply", ts=2, thread_id=thread_id),
        ),
    )

    outcome = await principal.settle_room_history_recovery(
        recovery,
        exhausted_server=True,
        attempted_policy_rank=4,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert outcome is HistoryRecoveryOutcome.REPAIRED
    assert await principal.room_history_recovery(ROOM) is None
    assert await principal.conversation_is_complete(room_id=ROOM, thread_id=thread_id)
    coverage = await principal.conversation_hydration_coverage(room_id=ROOM, thread_id=thread_id)
    assert coverage is not None
    assert coverage.reached_its_end
    assert coverage.attempted_policy_rank == 2


async def test_exact_object_mismatch_publishes_nothing(principal: PrincipalStore) -> None:
    """An older walk can neither add facts nor publish over a newer abandonment."""
    stale = await principal.record_room_history_recovery(ROOM)
    current = await principal.record_room_history_recovery(ROOM)
    assert not await install_recovery_events(
        principal,
        stale,
        (projected("$stale", "stale", ts=1),),
    )

    outcome = await principal.settle_room_history_recovery(
        stale,
        exhausted_server=True,
        attempted_policy_rank=4,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert outcome is HistoryRecoveryOutcome.SUPERSEDED
    assert await principal.room_history_recovery(ROOM) == current
    assert (await principal.read_conversation(room_id=ROOM, thread_id=None, limit=10)).messages == ()
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_repeated_recovery_supersedes_an_in_flight_settlement(
    principal: PrincipalStore,
) -> None:
    """A later identical abandonment cannot be discharged by an older walk."""
    old = await principal.record_room_history_recovery(ROOM)
    newer = await principal.record_room_history_recovery(ROOM)
    assert not await install_recovery_events(
        principal,
        old,
        (projected("$stale", "stale", ts=1),),
    )

    outcome = await principal.settle_room_history_recovery(
        old,
        exhausted_server=True,
        attempted_policy_rank=4,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert outcome is HistoryRecoveryOutcome.SUPERSEDED
    assert newer.revision == old.revision + 1
    assert await principal.room_history_recovery(ROOM) == newer
    assert (await principal.read_conversation(room_id=ROOM, thread_id=None, limit=10)).messages == ()
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_repaired_revision_is_not_reused_by_a_later_gap(principal: PrincipalStore) -> None:
    """Deleting a repaired row must not let a stale walk consume a new gap."""
    first = await principal.record_room_history_recovery(ROOM)
    assert first is not None
    assert await install_recovery_events(
        principal,
        first,
        (projected("$first", "first", ts=1),),
    )
    assert (
        await principal.settle_room_history_recovery(
            first,
            exhausted_server=True,
            attempted_policy_rank=4,
            expected_membership_epoch=await principal.membership_epoch(ROOM),
        )
        is HistoryRecoveryOutcome.REPAIRED
    )
    current = await principal.record_room_history_recovery(ROOM)
    assert current is not None
    assert not await install_recovery_events(
        principal,
        first,
        (projected("$stale", "stale", ts=2),),
    )

    stale_outcome = await principal.settle_room_history_recovery(
        first,
        exhausted_server=True,
        attempted_policy_rank=4,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    assert stale_outcome is HistoryRecoveryOutcome.SUPERSEDED
    assert current.revision > first.revision
    assert await principal.room_history_recovery(ROOM) == current
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)


async def test_expected_membership_mismatch_installs_neither_events_nor_settlement(
    principal: PrincipalStore,
) -> None:
    """A recovery walk from the wrong membership cannot leave either of its effects."""
    recovery = await principal.record_room_history_recovery(ROOM)
    assert not await install_recovery_events(
        principal,
        recovery,
        (projected("$wrong-epoch", "wrong", ts=1),),
        expected_membership_epoch=1,
    )

    outcome = await principal.settle_room_history_recovery(
        recovery,
        exhausted_server=True,
        attempted_policy_rank=4,
        expected_membership_epoch=1,
    )

    assert outcome is HistoryRecoveryOutcome.SUPERSEDED
    assert await principal.room_history_recovery(ROOM) == recovery
    assert (await principal.read_conversation(room_id=ROOM, thread_id=None, limit=10)).messages == ()
    assert not await principal.conversation_is_hydrated(room_id=ROOM, thread_id=None)
