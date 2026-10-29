"""Tests for personal Slack watches: the durable store and the check that a room may receive them."""

# ruff: noqa: D103

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest

from mindroom import constants
from mindroom.config.main import Config
from mindroom.slack.watches import SlackWatch, SlackWatchStore, _watch_id_for, _watch_room_problem, watch_problem
from tests.slack.room_state import room_state_client

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:example.org"
ALICE_BRIDGE = "@alice-bridge:example.org"
BOB = "@bob:example.org"
ROOM = "!room:example.org"
AGENT = "@mindroom_assistant:example.org"
ROUTER = "@mindroom_router:example.org"
INTERNAL_USER = "@mindroom_user:example.org"
BRIDGE_BOT = "@bridge:example.org"
LATER_JOINERS = "paused: people who join this room later could read earlier messages"


WATCH = SlackWatch(
    watch_id=_watch_id_for(ALICE, "assistant", ROOM),
    owner_id=ALICE,
    agent_name="assistant",
    room_id=ROOM,
    team_id="T0123ABCD",
    slack_user_id="U0ALICE00",
    mentions=True,
    direct_messages=True,
    channels=(),
    created_at=1_800_000_000,
)
BOB_WATCH = replace(WATCH, watch_id=_watch_id_for(BOB, "assistant", ROOM), owner_id=BOB)


def test_watch_id_hashes_owner_agent_and_room() -> None:
    expected = hashlib.sha256(f"{ALICE}|assistant|{ROOM}".encode()).hexdigest()[:16]

    assert _watch_id_for(ALICE, "assistant", ROOM) == expected
    assert len(expected) == 16
    assert len({_watch_id_for(owner, agent, room) for owner in (ALICE, BOB) for agent in "ab" for room in "xy"}) == 8


def test_store_round_trips_a_watch_through_the_control_state_file(tmp_path: Path) -> None:
    store = SlackWatchStore(tmp_path)
    watch = replace(WATCH, channels=("C0123ABCD", "C0456EFGH"), direct_messages=False)

    store.put(watch)

    assert (tmp_path / "slack" / "watches.json").is_file()
    assert SlackWatchStore(tmp_path).get(watch.watch_id) == watch
    assert SlackWatchStore(tmp_path).all() == (watch,)


def test_empty_store_has_no_watches(tmp_path: Path) -> None:
    store = SlackWatchStore(tmp_path)

    assert store.all() == ()
    assert store.get("0123456789abcdef") is None
    assert store.remove("0123456789abcdef") is False
    assert not (tmp_path / "slack" / "watches.json").exists()


def test_put_replaces_the_watch_with_the_same_id(tmp_path: Path) -> None:
    store = SlackWatchStore(tmp_path)
    store.put(WATCH)
    store.put(BOB_WATCH)
    changed = replace(WATCH, mentions=False, channels=("C0123ABCD",))

    store.put(changed)

    assert store.all() == (changed, BOB_WATCH)


def test_remove_reports_whether_the_watch_existed(tmp_path: Path) -> None:
    store = SlackWatchStore(tmp_path)
    store.put(WATCH)
    store.put(BOB_WATCH)

    assert store.remove(WATCH.watch_id) is True
    assert store.remove(WATCH.watch_id) is False
    assert store.all() == (BOB_WATCH,)


def test_store_file_holds_only_the_watch_fields(tmp_path: Path) -> None:
    watch = replace(WATCH, channels=("C0123ABCD",))
    SlackWatchStore(tmp_path).put(watch)

    stored = json.loads((tmp_path / "slack" / "watches.json").read_text(encoding="utf-8"))

    assert stored == {"watches": {watch.watch_id: {**asdict(watch), "channels": ["C0123ABCD"]}}}


@pytest.mark.parametrize("operation", ["all", "get", "put", "remove"])
def test_a_corrupt_store_raises_and_is_left_as_it_was(tmp_path: Path, operation: str) -> None:
    store = SlackWatchStore(tmp_path)
    path = tmp_path / "slack" / "watches.json"
    path.parent.mkdir()
    path.write_bytes(b'{"watches": {"0123456789abcdef": {"watch_id"')
    operations = {
        "all": store.all,
        "get": lambda: store.get(WATCH.watch_id),
        "put": lambda: store.put(WATCH),
        "remove": lambda: store.remove(WATCH.watch_id),
    }

    with pytest.raises(json.JSONDecodeError):
        operations[operation]()

    assert path.read_bytes() == b'{"watches": {"0123456789abcdef": {"watch_id"'


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://example.org"},
    )


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {"assistant": {"display_name": "Assistant"}},
            "authorization": {"aliases": {ALICE: [ALICE_BRIDGE]}},
            "mindroom_user": {"username": "mindroom_user", "display_name": "MindRoomUser"},
            "bot_accounts": [BRIDGE_BOT],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "joined",
    [(ALICE,), (ALICE, AGENT), (ALICE, AGENT, ROUTER), (ALICE_BRIDGE, ALICE, AGENT), (ALICE, AGENT, INTERNAL_USER)],
)
@pytest.mark.parametrize("history_visibility", ["invited", "joined"])
async def test_a_room_with_only_the_owner_and_mindroom_accounts_passes(
    config: Config,
    paths: RuntimePaths,
    joined: tuple[str, ...],
    history_visibility: str,
) -> None:
    client = room_state_client(joined, history_visibility=history_visibility)

    assert await _watch_room_problem(client, ROOM, ALICE, config, paths) is None
    client.room_get_state.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_the_owner_may_be_present_under_a_bridge_alias(config: Config, paths: RuntimePaths) -> None:
    client = room_state_client((ALICE_BRIDGE, AGENT))

    assert await _watch_room_problem(client, ROOM, ALICE, config, paths) is None


@pytest.mark.asyncio
async def test_people_who_left_and_agents_invited_do_not_count(config: Config, paths: RuntimePaths) -> None:
    client = room_state_client((ALICE, AGENT), invited=(ROUTER,), departed=(BOB,))

    assert await _watch_room_problem(client, ROOM, ALICE, config, paths) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "room",
    [{"joined": (AGENT,)}, {"joined": (AGENT,), "invited": (ALICE,)}, {"joined": (AGENT,), "departed": (ALICE,)}],
)
async def test_an_owner_who_is_not_joined_pauses_the_watch(
    config: Config,
    paths: RuntimePaths,
    room: dict[str, tuple[str, ...]],
) -> None:
    problem = await _watch_room_problem(room_state_client(**room), ROOM, ALICE, config, paths)

    assert problem == "paused: you are not in this room"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "room",
    [
        {"joined": (ALICE, AGENT, BOB)},
        {"joined": (ALICE, AGENT, "@mindroom_unknown:example.org")},
        {"joined": (ALICE, AGENT), "invited": (BOB,)},
        {"joined": (ALICE, AGENT), "invited": (ALICE_BRIDGE, BOB)},
        {"joined": (ALICE, AGENT, BRIDGE_BOT)},
        {"joined": (ALICE, AGENT), "invited": (BRIDGE_BOT,)},
    ],
)
async def test_another_person_or_bot_account_pauses_the_watch(
    config: Config,
    paths: RuntimePaths,
    room: dict[str, tuple[str, ...]],
) -> None:
    problem = await _watch_room_problem(room_state_client(**room), ROOM, ALICE, config, paths)

    assert problem == "paused: another person is in this room"


@pytest.mark.asyncio
@pytest.mark.parametrize("history_visibility", ["shared", "world_readable", None])
async def test_a_room_that_shows_history_to_later_joiners_pauses_the_watch(
    config: Config,
    paths: RuntimePaths,
    history_visibility: str | None,
) -> None:
    client = room_state_client((ALICE, AGENT), history_visibility=history_visibility)

    assert await _watch_room_problem(client, ROOM, ALICE, config, paths) == LATER_JOINERS


@pytest.mark.asyncio
async def test_a_room_that_cannot_be_read_pauses_the_watch(config: Config, paths: RuntimePaths) -> None:
    unreadable = AsyncMock()
    unreadable.room_get_state.return_value = nio.RoomGetStateError("boom")

    assert (
        await _watch_room_problem(unreadable, ROOM, ALICE, config, paths) == "paused: room members could not be checked"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [aiohttp.ClientError("down"), TimeoutError(), nio.LocalProtocolError("stale")])
async def test_a_room_whose_read_fails_pauses_the_watch(config: Config, paths: RuntimePaths, error: Exception) -> None:
    failing = AsyncMock()
    failing.room_get_state.side_effect = error

    assert await _watch_room_problem(failing, ROOM, ALICE, config, paths) == "paused: room members could not be checked"


def _credentials(**claims: str) -> dict[str, object]:
    return {
        "token": "xoxp-alice-token",
        "_oauth_claims": {"sub": "U0ALICE00", "team_id": "T0123ABCD", **claims},
        "_oauth_claims_verified": True,
    }


@pytest.mark.asyncio
async def test_a_watch_with_a_connected_account_and_a_private_room_has_no_problem(
    config: Config,
    paths: RuntimePaths,
) -> None:
    client = room_state_client((ALICE, AGENT))

    assert await watch_problem(WATCH, _credentials(), client, config, paths) is None


@pytest.mark.asyncio
async def test_a_watch_without_credentials_is_paused_before_the_room_is_read(
    config: Config,
    paths: RuntimePaths,
) -> None:
    client = room_state_client((ALICE, AGENT, BOB))

    assert await watch_problem(WATCH, None, client, config, paths) == "paused: Slack is not connected"
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{"sub": "U0OTHER00"}, {"team_id": "T0OTHER00"}])
async def test_a_watch_follows_only_the_slack_account_it_started_with(
    config: Config,
    paths: RuntimePaths,
    claims: dict[str, str],
) -> None:
    client = room_state_client((ALICE, AGENT, BOB))

    problem = await watch_problem(WATCH, _credentials(**claims), client, config, paths)

    assert problem == "paused: Slack account changed; start the watch again"
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_watch_without_verified_claims_is_paused_as_a_changed_account(
    config: Config,
    paths: RuntimePaths,
) -> None:
    unverified = {**_credentials(), "_oauth_claims_verified": False}

    problem = await watch_problem(WATCH, unverified, room_state_client((ALICE, AGENT)), config, paths)

    assert problem == "paused: Slack account changed; start the watch again"


@pytest.mark.asyncio
async def test_a_watch_reports_the_rooms_problem_last(config: Config, paths: RuntimePaths) -> None:
    client = room_state_client((ALICE, AGENT, BOB))

    assert await watch_problem(WATCH, _credentials(), client, config, paths) == "paused: another person is in this room"
    assert (
        await watch_problem(
            WATCH,
            _credentials(),
            room_state_client((ALICE, AGENT), history_visibility="shared"),
            config,
            paths,
        )
        == LATER_JOINERS
    )
