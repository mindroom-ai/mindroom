"""The steward rule: which MindRoom account does a room's shared work, and which one posts where no agent answers."""

from __future__ import annotations

import inspect
import math
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import nio

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_steward import pick_steward, room_poster, room_steward
from tests.conftest import install_bots, orchestrator_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
PERSON = "@alice:example.org"
OWNER = "@mindroom_router:example.org"
CODE = "@mindroom_code:example.org"
HELPER = "@mindroom_helper:example.org"
CREW = "@mindroom_crew:example.org"


def _state_event(event_type: str, sender: str, content: dict[str, object]) -> nio.Event | nio.BadEventType:
    return nio.Event.parse_event(
        {
            "type": event_type,
            "state_key": "",
            "sender": sender,
            "event_id": f"${event_type}",
            "origin_server_ts": 1,
            "content": content,
        },
    )


def _bot(
    user_id: str,
    power: int = 0,
    *,
    room_version: str = "11",
    creator: str = PERSON,
    running: bool = True,
    room_id: str = ROOM,
) -> AgentBot:
    """A bot whose own client caches ``room_id`` with ``power`` for itself, as its sync of the room's state fills it."""
    room = nio.MatrixRoom(room_id, user_id)
    room.handle_event(_state_event("m.room.create", creator, {"room_version": room_version}))
    room.handle_event(_state_event("m.room.power_levels", creator, {"users": {user_id: power}}))
    client = SimpleNamespace(user_id=user_id, rooms={room_id: room})
    return cast("AgentBot", SimpleNamespace(running=running, client=client))


def test_highest_power_wins() -> None:
    """Power 100 beats 50 beats 0, whatever the Matrix IDs and the order the bots are listed in."""
    assert pick_steward([(CODE, 0), (HELPER, 100), (CREW, 50)]) == HELPER
    assert pick_steward([(CODE, 0), (CREW, 50)]) == CREW

    bots = {"code": _bot(CODE, 0), "helper": _bot(HELPER, 100), "crew": _bot(CREW, 50)}
    assert room_steward(bots, ROOM) is bots["helper"]
    del bots["helper"]
    assert room_steward(bots, ROOM) is bots["crew"]


def test_tie_goes_to_lowest_matrix_id() -> None:
    """Among equal powers the lowest Matrix ID wins, not the first listed and not the entity name."""
    assert pick_steward([(HELPER, 50), (CODE, 50), (CREW, 50)]) == CODE

    bots = {"a_first_by_name": _bot(HELPER, 50), "z_last_by_name": _bot(CODE, 50)}
    assert room_steward(bots, ROOM) is bots["z_last_by_name"]


def test_privileged_creator_outranks_power_100() -> None:
    """In a room whose version gives creators unlimited power, the creator beats an account at power 100."""
    assert pick_steward([(CODE, 100), (HELPER, math.inf)]) == HELPER

    bots = {
        "code": _bot(CODE, 100, room_version="12", creator=HELPER),
        "helper": _bot(HELPER, room_version="12", creator=HELPER),
    }
    assert room_steward(bots, ROOM) is bots["helper"]
    # Before room version 12 the creator has only the power the room's power levels give it.
    bots = {"code": _bot(CODE, 100, creator=HELPER), "helper": _bot(HELPER, creator=HELPER)}
    assert room_steward(bots, ROOM) is bots["code"]


def test_stopped_or_unjoined_bot_is_not_a_candidate() -> None:
    """A stopped bot, one without a client, and one whose own client does not list the room are passed over."""
    without_client = cast("AgentBot", SimpleNamespace(running=True, client=None))
    bots = {
        "stopped": _bot(HELPER, 100, running=False),
        "without_client": without_client,
        "elsewhere": _bot(CREW, 100, room_id=OTHER_ROOM),
        "code": _bot(CODE, 0),
    }
    assert room_steward(bots, ROOM) is bots["code"]
    assert room_poster(bots, ROOM) is bots["code"]


def test_no_candidate_returns_none() -> None:
    """With no bot running and joined there is no steward and no poster."""
    assert pick_steward([]) is None
    bots = {ROUTER_AGENT_NAME: _bot(OWNER, 100, running=False), "elsewhere": _bot(CODE, 100, room_id=OTHER_ROOM)}
    assert room_steward(bots, ROOM) is None
    assert room_poster(bots, ROOM) is None
    assert room_steward({}, ROOM) is None
    assert room_poster({}, ROOM) is None


def test_one_agent_room_makes_that_agent_steward() -> None:
    """In a person's room with one agent at power 0 and no owner account, that agent is steward and poster."""
    bots = {ROUTER_AGENT_NAME: _bot(OWNER, 100, room_id=OTHER_ROOM), "code": _bot(CODE, 0)}
    assert room_steward(bots, ROOM) is bots["code"]
    assert room_poster(bots, ROOM) is bots["code"]


def test_owner_is_steward_by_power_in_a_room_it_created() -> None:
    """The owner entity is a candidate like any other, so its power in a room it created makes it the steward."""
    bots = {"code": _bot(CODE, 0), ROUTER_AGENT_NAME: _bot(OWNER, 100)}
    assert room_steward(bots, ROOM) is bots[ROUTER_AGENT_NAME]
    # It holds no special place: an agent with more power is the steward instead.
    bots = {"code": _bot(CODE, 100), ROUTER_AGENT_NAME: _bot(OWNER, 50)}
    assert room_steward(bots, ROOM) is bots["code"]


def test_poster_is_the_owner_where_it_is_running_and_joined() -> None:
    """The owner posts where it is joined, even with less power and a higher Matrix ID than an agent there."""
    bots = {"code": _bot(CODE, 100), ROUTER_AGENT_NAME: _bot(OWNER, 0)}
    assert room_poster(bots, ROOM) is bots[ROUTER_AGENT_NAME]
    assert room_poster({ROUTER_AGENT_NAME: bots[ROUTER_AGENT_NAME]}, ROOM) is bots[ROUTER_AGENT_NAME]


def test_poster_is_lowest_id_agent_or_team_without_the_owner() -> None:
    """Where the owner is stopped, not joined or absent, the lowest Matrix ID among agents and teams posts."""
    agents = {"helper": _bot(HELPER, 100), "crew": _bot(CREW, 0), "code": _bot(CODE, 50)}
    assert room_poster(agents, ROOM) is agents["code"]
    assert room_poster({**agents, ROUTER_AGENT_NAME: _bot(OWNER, 100, running=False)}, ROOM) is agents["code"]
    assert room_poster({**agents, ROUTER_AGENT_NAME: _bot(OWNER, 100, room_id=OTHER_ROOM)}, ROOM) is agents["code"]
    del agents["code"]
    assert room_poster(agents, ROOM) is agents["crew"]


def test_every_bot_gets_the_same_answer(tmp_path: Path) -> None:
    """No argument names the asking bot, so every bot asking its runtime about a room gets the same answer."""
    assert list(inspect.signature(room_steward).parameters) == ["bots", "room_id"]
    assert list(inspect.signature(room_poster).parameters) == ["bots", "room_id"]

    runtime_paths = orchestrator_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"code": {"display_name": "Code"}, "helper": {"display_name": "Helper"}},
        },
        runtime_paths,
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=config)
    bots = {"code": _bot(CODE, 50), "helper": _bot(HELPER, 50), ROUTER_AGENT_NAME: _bot(OWNER, 100)}
    install_bots(orchestrator, bots)
    for bot in bots.values():
        cast("SimpleNamespace", bot).orchestrator = orchestrator

    for asking in bots.values():
        assert asking.orchestrator.room_steward(ROOM) is bots[ROUTER_AGENT_NAME]
        assert asking.orchestrator.room_poster(ROOM) is bots[ROUTER_AGENT_NAME]
        assert asking.orchestrator.room_steward(OTHER_ROOM) is None
    cast("SimpleNamespace", bots[ROUTER_AGENT_NAME]).running = False
    for asking in bots.values():
        assert asking.orchestrator.room_steward(ROOM) is bots["code"]
        assert asking.orchestrator.room_poster(ROOM) is bots["code"]
