"""A room's routing lane: its owner selects once, saves the handoff, and moves it to the chosen bot's journal."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.coalescing.batch import build_prepared_turn, requester_coalescing_key
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import ROUTING_HANDOFF_CLAIM, EventKind, turn_rows
from mindroom.event_journal.identity import journal_principal_id
from mindroom.turn_record import TurnRecord
from mindroom.turns.routing_lane import route_turn, routed_events, routed_turn
from tests.conftest import bind_runtime_paths, make_pending_event, runtime_paths_for, test_runtime_paths
from tests.turn_dispatch_helpers import nothing_else_queued

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.coalescing.batch import PreparedTurn
    from mindroom.event_journal import EventJournalStore

_ROOM_ID = "!room:localhost"
_SENDER = "@user:localhost"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Two agents a lane owner selects between."""
    agents = {name: AgentConfig(display_name=name.title()) for name in ("general", "research")}
    return bind_runtime_paths(Config(agents=agents), test_runtime_paths(tmp_path))


def _no_timestamp(_timestamp_ms: float | None) -> str | None:
    return None


def _principal(config: Config, entity_name: str) -> str:
    registry = entity_identity_registry(config, runtime_paths_for(config))
    return journal_principal_id(entity_name, registry.current_id(entity_name).full_id)


def _turn(*messages: tuple[str, str]) -> PreparedTurn:
    """Return the turn a lane owner's gate made of these ``(event_id, body)`` messages."""
    room = nio.MatrixRoom(_ROOM_ID, "@mindroom_router:localhost")
    events = [
        nio.RoomMessageText.from_dict(
            {
                "content": {"body": body, "msgtype": "m.text"},
                "event_id": event_id,
                "sender": _SENDER,
                "origin_server_ts": 1_000_000 + index,
                "room_id": _ROOM_ID,
                "type": "m.room.message",
            },
        )
        for index, (event_id, body) in enumerate(messages)
    ]
    return build_prepared_turn(
        requester_coalescing_key(_ROOM_ID, None, _SENDER),
        [
            make_pending_event(event, room, source_kind=MESSAGE_SOURCE_KIND, requester_user_id=_SENDER)
            for event in events
        ],
        queues_other_run=nothing_else_queued,
        timestamp_formatter=_no_timestamp,
    )


class _CrashesBeforeAcceptance:
    """A journal that stops, as a crash does, when a saved handoff is about to move."""

    def __init__(self, journal: EventJournalStore) -> None:
        self._journal = journal

    def __getattr__(self, name: str) -> object:
        return getattr(self._journal, name)

    async def accept_routing_handoffs(self, _room_id: str | None = None) -> tuple[str, ...]:
        msg = "crash"
        raise RuntimeError(msg)


async def _route(
    journal: EventJournalStore,
    config: Config,
    turn: PreparedTurn,
    choose: Callable[[], object],
    *,
    owner: str = ROUTER_AGENT_NAME,
) -> tuple[str, ...] | None:
    return await route_turn(journal, config, runtime_paths_for(config), owner, turn, choose)


async def _routed_turns(journal: EventJournalStore, config: Config, entity_name: str) -> list[PreparedTurn]:
    """Return the turns waiting in one bot's journal, rebuilt as that bot's dispatcher rebuilds them."""
    principal = journal.principal(_principal(config, entity_name))
    room = nio.MatrixRoom(_ROOM_ID, "@mindroom_research:localhost")
    turns = []
    for event_id in sorted(await principal.unsettled_event_ids()):
        event = await principal.load_event(event_id)
        assert event is not None
        assert event.kind is EventKind.ROUTED_TURN
        turns.append(
            routed_turn(event.event_id, None, routed_events(event.source, room), timestamp_formatter=_no_timestamp),
        )
    return turns


@pytest.mark.asyncio
async def test_routed_turn_reaches_the_chosen_bot_as_the_lane_owner_prepared_it(
    journal_store: EventJournalStore,
    config: Config,
) -> None:
    """The chosen bot's journal holds the lane owner's whole batch, owned through the handoff, answering the person."""
    turn = _turn(("$first", "what is on today"), ("$second", "and tomorrow"))
    choose = AsyncMock(return_value="research")

    assert await _route(journal_store, config, turn, choose) == ("research",)

    choose.assert_awaited_once()
    assert await _routed_turns(journal_store, config, "general") == []
    (routed,) = await _routed_turns(journal_store, config, "research")
    assert routed.handled_turn.source_event_ids == ("routed:$first",)
    assert set(routed.handled_turn.indexed_event_ids) >= {"$first", "$second"}
    assert routed.answers_event_id == turn.event.event_id
    assert routed.event.body == turn.event.body
    assert routed.facts.run_requester_id == _SENDER


@pytest.mark.asyncio
async def test_routed_upload_reaches_the_chosen_bot_as_media(journal_store: EventJournalStore, config: Config) -> None:
    """An upload in the lane owner's batch is rebuilt as the media event it was, with its caption as the turn's text."""
    room = nio.MatrixRoom(_ROOM_ID, "@mindroom_router:localhost")
    image = nio.RoomMessageImage.from_dict(
        {
            "content": {"body": "what is in this picture", "msgtype": "m.image", "url": "mxc://localhost/picture"},
            "event_id": "$image",
            "sender": _SENDER,
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )
    turn = build_prepared_turn(
        requester_coalescing_key(_ROOM_ID, None, _SENDER),
        [make_pending_event(image, room, source_kind=MESSAGE_SOURCE_KIND, requester_user_id=_SENDER)],
        queues_other_run=nothing_else_queued,
        timestamp_formatter=_no_timestamp,
    )

    await _route(journal_store, config, turn, AsyncMock(return_value="research"))

    (routed,) = await _routed_turns(journal_store, config, "research")
    assert [(type(event), event.url) for event in routed.media_events] == [
        (nio.RoomMessageImage, "mxc://localhost/picture"),
    ]
    assert routed.event.body == turn.event.body


@pytest.mark.asyncio
async def test_crash_before_acceptance_retries_the_same_handoff_without_a_new_selection(
    journal_database: Callable[[], EventJournalStore],
    config: Config,
) -> None:
    """A handoff saved before a crash is the one the replayed turn moves on."""
    journal = journal_database()
    turn = _turn(("$first", "what is on today"))
    choose = AsyncMock(return_value="research")
    with pytest.raises(RuntimeError, match="crash"):
        await _route(_CrashesBeforeAcceptance(journal), config, turn, choose)
    assert await _routed_turns(journal, config, "research") == []
    await journal.close()

    restarted = journal_database()
    # The replayed turn would choose another agent now; the saved choice stands.
    assert await _route(restarted, config, turn, AsyncMock(return_value="general")) == ("research",)
    choose.assert_awaited_once()
    assert len(await _routed_turns(restarted, config, "research")) == 1
    assert await _routed_turns(restarted, config, "general") == []


@pytest.mark.asyncio
async def test_restart_moves_a_saved_handoff_before_any_turn_replays(
    journal_database: Callable[[], EventJournalStore],
    config: Config,
) -> None:
    """The row alone carries the turn, so startup hands it over without the lane owner's events."""
    journal = journal_database()
    with pytest.raises(RuntimeError, match="crash"):
        await _route(
            _CrashesBeforeAcceptance(journal),
            config,
            _turn(("$first", "what is on today")),
            AsyncMock(return_value="research"),
        )
    await journal.close()

    restarted = journal_database()
    await restarted.accept_routing_handoffs()

    assert len(await _routed_turns(restarted, config, "research")) == 1


@pytest.mark.asyncio
async def test_replay_after_acceptance_hands_nothing_over_again(
    journal_store: EventJournalStore,
    config: Config,
) -> None:
    """Once the chosen bot holds the turn, a replay of the lane owner's turn selects nothing and moves nothing."""
    turn = _turn(("$first", "what is on today"))
    await _route(journal_store, config, turn, AsyncMock(return_value="research"))
    recipient = journal_store.principal(_principal(config, "research"))
    await recipient.settle_many(("routed:$first",))
    choose = AsyncMock(return_value="general")

    assert await _route(journal_store, config, turn, choose) == ()

    choose.assert_not_awaited()
    assert await _routed_turns(journal_store, config, "research") == []
    assert await _routed_turns(journal_store, config, "general") == []


@pytest.mark.asyncio
async def test_successor_completes_a_pending_handoff_and_the_stale_owner_saves_nothing(
    journal_store: EventJournalStore,
    config: Config,
) -> None:
    """A new lane owner moves the handoff its predecessor saved; a bot that lost the lane meanwhile saves no choice."""
    with pytest.raises(RuntimeError, match="crash"):
        await _route(
            _CrashesBeforeAcceptance(journal_store),
            config,
            _turn(("$first", "what is on today")),
            AsyncMock(return_value="research"),
        )

    # The successor never saw the first message: its own next turn moves the row its predecessor left.
    successor_turn = _turn(("$second", "and tomorrow"))
    assert await _route(
        journal_store,
        config,
        successor_turn,
        AsyncMock(return_value="general"),
        owner="general",
    ) == ("research", "general")
    assert [turn.event.body for turn in await _routed_turns(journal_store, config, "research")] == ["what is on today"]

    async def chooses_while_the_lane_moves() -> str:
        await journal_store.hold_routing_lane(_ROOM_ID, _principal(config, "general"))
        return "research"

    stale_turn = _turn(("$third", "one more"))
    assert await _route(journal_store, config, stale_turn, chooses_while_the_lane_moves) is None
    assert await journal_store.room_event_claims(_ROOM_ID, ["$third"], ROUTING_HANDOFF_CLAIM) == {}


@pytest.mark.asyncio
async def test_no_fit_is_saved_and_nobody_gets_the_turn(journal_store: EventJournalStore, config: Config) -> None:
    """A turn nobody fits is decided once too: its replay asks no model and hands nothing over."""
    turn = _turn(("$first", "what is on today"))

    assert await _route(journal_store, config, turn, AsyncMock(return_value=None)) == ()

    assert await journal_store.room_event_claims(_ROOM_ID, ["$first"], ROUTING_HANDOFF_CLAIM) == {
        "$first": "routed:$first",
    }
    choose = AsyncMock(return_value="research")
    assert await _route(journal_store, config, turn, choose) == ()
    choose.assert_not_awaited()
    assert await _routed_turns(journal_store, config, "research") == []


@pytest.mark.asyncio
async def test_message_the_old_router_relayed_is_left_to_its_relay(
    journal_store: EventJournalStore,
    config: Config,
) -> None:
    """At upgrade, a message the relaying router already answered gets no handoff; one batched with it keeps its own."""
    relayed = TurnRecord.create(["$relayed"], response_event_id="$relay")
    await journal_store.turn_records(ROUTER_AGENT_NAME).write(
        turn_rows.record_responded_turn,
        relayed,
        room_id=_ROOM_ID,
    )
    choose = AsyncMock(return_value="research")

    assert await _route(journal_store, config, _turn(("$relayed", "old message")), choose, owner="general") == ()
    choose.assert_not_awaited()

    batch = _turn(("$relayed", "old message"), ("$fresh", "new message"))
    assert await _route(journal_store, config, batch, choose, owner="general") == ("research",)
    (routed,) = await _routed_turns(journal_store, config, "research")
    assert routed.event.body == "new message"
