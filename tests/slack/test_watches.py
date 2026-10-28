"""Tests for personal Slack watches: the durable store and the check that a room may receive them."""

# ruff: noqa: D103

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom import constants
from mindroom.config.main import Config
from mindroom.slack.watches import SlackWatch, SlackWatchStore, watch_id_for, watch_room_problem

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:example.org"
ALICE_BRIDGE = "@alice-bridge:example.org"
BOB = "@bob:example.org"
ROOM = "!room:example.org"
AGENT = "@mindroom_assistant:example.org"
ROUTER = "@mindroom_router:example.org"


WATCH = SlackWatch(
    watch_id=watch_id_for(ALICE, "assistant", ROOM),
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
BOB_WATCH = replace(WATCH, watch_id=watch_id_for(BOB, "assistant", ROOM), owner_id=BOB)


def test_watch_id_hashes_owner_agent_and_room() -> None:
    expected = hashlib.sha256(f"{ALICE}|assistant|{ROOM}".encode()).hexdigest()[:16]

    assert watch_id_for(ALICE, "assistant", ROOM) == expected
    assert len(expected) == 16
    assert len({watch_id_for(owner, agent, room) for owner in (ALICE, BOB) for agent in "ab" for room in "xy"}) == 8


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
        },
    )


def _client(*members: str) -> AsyncMock:
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, None, None) for user_id in members],
        ROOM,
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [(ALICE,), (ALICE, AGENT), (ALICE, AGENT, ROUTER), (ALICE_BRIDGE, ALICE, AGENT)])
async def test_a_room_with_only_the_owner_and_agents_passes(
    config: Config,
    paths: RuntimePaths,
    members: tuple[str, ...],
) -> None:
    assert await watch_room_problem(_client(*members), ROOM, ALICE, config, paths) is None


@pytest.mark.asyncio
async def test_the_owner_may_be_present_under_a_bridge_alias(config: Config, paths: RuntimePaths) -> None:
    assert await watch_room_problem(_client(ALICE_BRIDGE, AGENT), ROOM, ALICE, config, paths) is None


@pytest.mark.asyncio
async def test_an_owner_who_left_pauses_the_watch(config: Config, paths: RuntimePaths) -> None:
    problem = await watch_room_problem(_client(AGENT), ROOM, ALICE, config, paths)

    assert problem == "paused: you are not in this room"


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [BOB, "@mindroom_unknown:example.org"])
async def test_another_person_pauses_the_watch(config: Config, paths: RuntimePaths, other: str) -> None:
    problem = await watch_room_problem(_client(ALICE, AGENT, other), ROOM, ALICE, config, paths)

    assert problem == "paused: another person is in this room"


@pytest.mark.asyncio
async def test_a_member_list_that_cannot_be_read_pauses_the_watch(config: Config, paths: RuntimePaths) -> None:
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersError("boom")

    assert await watch_room_problem(client, ROOM, ALICE, config, paths) == "paused: room members could not be checked"
