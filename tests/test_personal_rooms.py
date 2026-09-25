"""Native personal-room configuration and recoverable lifecycle tests."""

# Stateful Matrix transport accepts the same flexible payloads as the network API.
# ruff: noqa: ANN401

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import nio
import pytest
from pydantic import ValidationError

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import EventClass, EventKind
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.handled_turns import TurnRecord
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, send_message_result
from mindroom.matrix.personal_room_store import (
    PersonalRoomAdoption,
    PersonalRoomRecord,
    _personal_room_records,
    personal_room_record_path,
    read_personal_room,
    retained_personal_rooms,
    write_personal_room,
)
from mindroom.matrix.personal_rooms import PersonalRoomService
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.personal_room_lifecycle import PersonalRoomLifecycle, PersonalRoomTarget
from mindroom.runtime_resolution import resolve_agent_runtime
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import TEST_PASSWORD, install_runtime_journal_support, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.journal_helpers import admit_dispatch_event
from tests.test_room_member_hooks import _dispatch_member, _room_member_event

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def personal_config(**overrides: object) -> Config:
    """Build a shared lobby with a separately configured personal agent."""
    return Config.model_validate(
        {
            "agents": {
                "helper": {
                    "display_name": "Helper",
                    "rooms": [],
                    "access": {"users": ["@alice:localhost", "@bob:localhost"]},
                },
            },
            "rooms": {"lobby": {}},
            "personal_rooms": {"agent": "helper", "onboarding_rooms": ["lobby"], **overrides},
        },
    )


def test_personal_rooms_disabled_by_default() -> None:
    """Ordinary configurations must not activate automatic room creation."""
    assert Config().personal_rooms is None


def test_personal_target_does_not_need_to_join_lobby() -> None:
    """Router observes onboarding without exposing a private agent to the lobby."""
    config = personal_config()
    assert config.personal_rooms.agent == "helper"
    assert config.agents["helper"].rooms == []
    assert not config.personal_rooms.backfill
    assert not config.personal_rooms.welcome_dispatch
    assert not config.personal_rooms.requester_admin
    assert not config.personal_rooms.auto_join_requester


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent": "missing"},
        {"onboarding_rooms": ["unknown"]},
        {"onboarding_rooms": []},
        {"alias_prefix": "../bad"},
        {"name": "{user.__class__}"},
        {"welcome": "{unknown}"},
        {"commands": ["hello"]},
    ],
)
def test_invalid_personal_room_configuration(overrides: dict[str, object]) -> None:
    """Reject configurations that cannot safely identify targets or render text."""
    with pytest.raises(ValidationError):
        personal_config(**overrides)


class MatrixServer:
    """Stateful network boundary; tests assert server state rather than mocks."""

    def __init__(self) -> None:
        self.user_id = "@mindroom_helper:localhost"
        self.device_id = "DEVICE"
        self.rooms: dict[str, Any] = {}
        self.state: dict[str, list[dict[str, Any]]] = {}
        self.aliases: dict[str, str] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.fail_invite = False
        self.fail_kick = False
        self.fail_send = False
        self.fail_receipt = False
        self.create_count = 0
        self.lobby_members = {"@alice:localhost", "@bob:localhost"}

    async def room_resolve_alias(self, alias: str) -> object:
        """Resolve server-owned aliases."""
        if alias in self.aliases:
            return nio.RoomResolveAliasResponse(alias, self.aliases[alias], [])
        return nio.RoomResolveAliasError("missing", "M_NOT_FOUND")

    async def room_create(self, **kwargs: Any) -> object:
        """Create one private server room or return an alias collision."""
        await asyncio.sleep(0)
        alias = f"#{kwargs['alias']}:localhost"
        if alias in self.aliases:
            return nio.RoomCreateError("exists", "M_ROOM_IN_USE")
        self.create_count += 1
        room_id = f"!personal{self.create_count}:localhost"
        self.aliases[alias] = room_id
        self.state[room_id] = [
            {"type": "m.room.create", "state_key": "", "sender": self.user_id, "content": {"creator": self.user_id}},
            {
                "type": "m.room.member",
                "state_key": self.user_id,
                "sender": self.user_id,
                "content": {"membership": "join"},
            },
            *[{"state_key": "", "sender": self.user_id, **item} for item in kwargs["initial_state"]],
        ]
        self.rooms[room_id] = nio.MatrixRoom(room_id, self.user_id)
        return nio.RoomCreateResponse(room_id)

    async def room_get_state(self, room_id: str) -> object:
        """Return authoritative state including invited users."""
        return nio.RoomGetStateResponse(self.state[room_id], room_id)

    async def room_get_visibility(self, room_id: str) -> object:
        """Return the private directory state."""
        return nio.RoomGetVisibilityResponse.from_dict({"visibility": "private"}, room_id)

    async def room_get_state_event(self, room_id: str, event_type: str, state_key: str = "") -> object:
        """Read one authoritative state event."""
        event = next(
            (event for event in self.state[room_id] if (event["type"], event["state_key"]) == (event_type, state_key)),
            None,
        )
        if event is None:
            return nio.RoomGetStateEventError("missing", "M_NOT_FOUND")
        return nio.RoomGetStateEventResponse(event["content"], event_type, state_key, room_id)

    async def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> object:
        """Apply a room state mutation."""
        self.state[room_id] = [
            event for event in self.state[room_id] if (event["type"], event["state_key"]) != (event_type, state_key)
        ]
        self.state[room_id].append(
            {"type": event_type, "state_key": state_key, "sender": self.user_id, "content": content},
        )
        return nio.RoomPutStateResponse("$state", room_id)

    async def joined_members(self, room_id: str) -> object:
        """Return current joined users."""
        users = (
            self.lobby_members
            if room_id == "!lobby:localhost"
            else {
                event["state_key"]
                for event in self.state[room_id]
                if event["type"] == "m.room.member" and event["content"]["membership"] == "join"
            }
        )
        return nio.JoinedMembersResponse([nio.RoomMember(user_id, None, None) for user_id in users], room_id)

    async def room_invite(self, room_id: str, user_id: str) -> object:
        """Record an invitation or simulate an unavailable server."""
        if self.fail_invite:
            return nio.RoomInviteError("retry", "M_UNKNOWN")
        self.set_member(room_id, user_id, "invite")
        return nio.RoomInviteResponse()

    async def room_kick(self, room_id: str, user_id: str, reason: str | None = None) -> object:
        """Remove one member or revoke one invite."""
        del reason
        if self.fail_kick:
            return nio.RoomKickError("retry", "M_UNKNOWN")
        self.set_member(room_id, user_id, "leave")
        return nio.RoomKickResponse()

    def membership(self, room_id: str, user_id: str) -> str | None:
        """Return one user's current membership."""
        return next(
            (
                event["content"]["membership"]
                for event in self.state[room_id]
                if (event["type"], event["state_key"]) == ("m.room.member", user_id)
            ),
            None,
        )

    def set_member(self, room_id: str, user_id: str, membership: str) -> None:
        """Replace one membership state event."""
        self.state[room_id] = [
            event for event in self.state[room_id] if (event["type"], event["state_key"]) != ("m.room.member", user_id)
        ]
        self.state[room_id].append(
            {"type": "m.room.member", "state_key": user_id, "sender": user_id, "content": {"membership": membership}},
        )

    async def deliver(
        self,
        _client: object,
        room_id: str,
        content: dict[str, Any],
        **kwargs: Any,
    ) -> DeliveredMatrixEvent | None:
        """Deduplicate delivery by the Matrix transaction identifier."""
        if self.fail_send:
            return None
        transaction = kwargs["transaction_id"]
        self.messages.setdefault(transaction, {"room_id": room_id, "content": content})
        return DeliveredMatrixEvent(f"${transaction}", content)


def service(tmp_path: Path, server: MatrixServer, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Any:
    """Bind production service to a stateful Matrix transport."""
    config = personal_config(**overrides)
    runtime = SimpleNamespace(config=config, client=server, agent_reply_memberships=AgentReplyMembershipIndex())
    paths = test_runtime_paths(tmp_path)
    state = MatrixState.load(paths)
    state.add_room("lobby", "!lobby:localhost", "#lobby:localhost", "Lobby")
    state.save(paths)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.send_message_result", server.deliver)
    return PersonalRoomService("helper", runtime, paths, AsyncMock(return_value=True))


def move_onboarding_to_new_room(owner: PersonalRoomService, server: MatrixServer) -> None:
    """Keep the requester eligible after replacing the original onboarding room."""
    owner.runtime.config.rooms["new"] = owner.runtime.config.rooms["lobby"].model_copy()
    owner.runtime.config.personal_rooms.onboarding_rooms = ["new"]
    state = MatrixState.load(owner.runtime_paths)
    state.add_room("new", "!new:localhost", "#new:localhost", "New")
    state.save(owner.runtime_paths)
    server.state["!new:localhost"] = [
        {"type": "m.room.member", "state_key": "@alice:localhost", "content": {"membership": "join"}},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_event_processed", [True, False])
async def test_validated_replacement_source_completes_deferred_welcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_event_processed: bool,
) -> None:
    """A validated replacement survives callback delivery and default restart recovery."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome_dispatch=True, welcome="Original {user}")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    move_onboarding_to_new_room(owner, server)
    assert await owner.ensure("@alice:localhost", "!new:localhost", server) == room_id
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).source_room_id == "!lobby:localhost"
    assert not server.messages

    server.set_member(room_id, "@alice:localhost", "join")
    if owner_event_processed:
        lifecycle = PersonalRoomLifecycle(
            "helper",
            owner.runtime,
            owner.runtime_paths,
            owner,
            lambda _: None,
            lambda event: event.sender,
        )
        await lifecycle.member_event(
            nio.MatrixRoom(room_id, server.user_id),
            _room_member_event(prev_membership="invite"),
        )
        assert len(server.messages) == 1

    restarted = service(tmp_path, server, monkeypatch, welcome_dispatch=True, welcome="Changed {user}")
    restarted.runtime.config = owner.runtime.config
    router = PersonalRoomLifecycle(
        "router",
        restarted.runtime,
        restarted.runtime_paths,
        restarted,
        lambda _: PersonalRoomTarget(restarted, True),
        lambda event: event.sender,
    )
    await router._reconcile()
    record = read_personal_room(path)
    assert record.source_room_id == "!lobby:localhost"
    assert record.welcome_completed
    assert len(server.messages) == 1
    assert next(iter(server.messages.values()))["content"]["body"] == "Original @alice:localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_by", ["membership", "access", "locked_membership"])
async def test_unvalidated_replacement_source_does_not_authorize_deferred_welcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejected_by: str,
) -> None:
    """An owner callback cannot infer replacement source authority from configuration."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome_dispatch=True)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    move_onboarding_to_new_room(owner, server)
    if rejected_by == "membership":
        server.set_member("!new:localhost", "@alice:localhost", "leave")
    elif rejected_by == "access":
        owner.runtime.config.agents["helper"].access.users = []
    else:
        original_joined_members = server.joined_members
        lookups = 0

        async def depart_before_locked_lookup(room_id: str) -> object:
            nonlocal lookups
            if room_id == "!new:localhost":
                lookups += 1
                if lookups == 2:
                    server.set_member(room_id, "@alice:localhost", "leave")
            return await original_joined_members(room_id)

        monkeypatch.setattr(server, "joined_members", depart_before_locked_lookup)
    assert await owner.ensure("@alice:localhost", "!new:localhost", server) is None
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    server.set_member(room_id, "@alice:localhost", "join")
    await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    record = read_personal_room(path)
    assert record.source_room_id == "!lobby:localhost"
    assert record.resume_source_room_id is None
    assert not record.welcome_completed
    assert not server.messages


@pytest.mark.asyncio
async def test_concurrent_restart_reuses_room_and_welcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent triggers and a fresh service must reuse room and successful welcome."""
    server = MatrixServer()
    first = service(tmp_path, server, monkeypatch)
    ids = await asyncio.gather(*[first.ensure("@alice:localhost", "!lobby:localhost", server) for _ in range(8)])
    second = service(tmp_path, server, monkeypatch)
    assert await second.ensure("@alice:localhost", "!lobby:localhost", server) == ids[0]
    assert len(set(ids)) == server.create_count == len(server.messages) == 1
    assert server.aliases == {"#personal_481a6d9da9e7b569dd3c:localhost": ids[0]}


@pytest.mark.asyncio
async def test_membership_failure_retries_without_duplicate_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Room survives invitation failure and restarts finish its membership."""
    server = MatrixServer()
    first = service(tmp_path, server, monkeypatch)
    server.fail_invite = True
    with pytest.raises(RuntimeError, match="invite"):
        await first.ensure("@alice:localhost", "!lobby:localhost", server)
    server.fail_invite = False
    second = service(tmp_path, server, monkeypatch)
    room_id = await second.ensure("@alice:localhost", "!lobby:localhost", server)
    assert server.create_count == 1
    assert any(
        event["state_key"] == "@alice:localhost" and event["content"].get("membership") == "invite"
        for event in server.state[room_id]
    )


@pytest.mark.asyncio
async def test_welcome_retry_uses_frozen_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing configuration cannot change a pending transaction's message."""
    server = MatrixServer()
    server.fail_send = True
    first = service(tmp_path, server, monkeypatch, welcome="First {user}")
    with pytest.raises(RuntimeError, match="welcome"):
        await first.ensure("@alice:localhost", "!lobby:localhost", server)
    server.fail_send = False
    second = service(tmp_path, server, monkeypatch, welcome="Changed {user}")
    await second.ensure("@alice:localhost", "!lobby:localhost", server)
    assert next(iter(server.messages.values()))["content"]["body"] == "First @alice:localhost"


@pytest.mark.asyncio
async def test_dispatch_waits_for_human_join_and_keeps_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trusted self-authored onboarding prompt retains its human execution identity."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome_dispatch=True)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert not server.messages
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    legacy_record = json.loads(path.read_text())
    legacy_record.pop("resume_source_room_id")
    path.write_text(json.dumps(legacy_record))
    server.set_member(room_id, "@alice:localhost", "join")
    await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    content = next(iter(server.messages.values()))["content"]
    assert content[ORIGINAL_SENDER_KEY] == "@alice:localhost"
    assert content[SOURCE_KIND_KEY] == "hook_dispatch"


@pytest.mark.asyncio
async def test_service_accounts_and_denied_humans_are_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Membership alone grants neither bot onboarding nor access to a restricted agent."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    for user in (server.user_id, "@eve:localhost"):
        server.lobby_members.add(user)
        assert await owner.ensure(user, "!lobby:localhost", server) is None
    assert not server.state


@pytest.mark.asyncio
async def test_owner_invited_agents_keep_the_room_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner may bring other MindRoom agents into a personal room."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, "@mindroom_router:localhost", "join")
    server.set_member(room_id, "@mindroom_general:localhost", "invite")
    owner.runtime.config.agents["general"] = owner.runtime.config.agents["helper"].model_copy()

    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) == room_id

    assert server.membership(room_id, "@mindroom_router:localhost") == "join"
    assert server.membership(room_id, "@mindroom_general:localhost") == "invite"
    assert not server.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["invite", "join", "knock"])
@pytest.mark.parametrize("guest", ["@bob:localhost", "@bridge:localhost"])
async def test_other_people_are_removed_from_a_personal_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    membership: str,
    guest: str,
) -> None:
    """A personal room stays private: another person, or a bridge relaying people, is removed once."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    owner.runtime.config.bot_accounts = ["@bridge:localhost"]
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, guest, membership)

    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) == room_id

    assert server.membership(room_id, guest) == "leave"
    assert server.membership(room_id, "@alice:localhost") in {"invite", "join"}
    notices = [message["content"]["body"] for message in server.messages.values() if message["room_id"] == room_id]
    assert notices == [
        f"This room is private to @alice:localhost, so I removed {guest}. "
        "To work with other people, use a shared room.",
    ]


@pytest.mark.asyncio
async def test_a_guest_is_removed_when_the_invite_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removal does not wait for the next reconciliation, and leaves agents alone."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    room = nio.MatrixRoom(room_id, server.user_id)
    room.add_member("@alice:localhost", None, None)
    for user_id in ("@mindroom_router:localhost", "@bob:localhost"):
        server.set_member(room_id, user_id, "invite")
        room.add_member(user_id, None, None, invited=True)
        await owner.guest_membership_event(room, user_id, "invite")

    assert server.membership(room_id, "@mindroom_router:localhost") == "invite"
    assert server.membership(room_id, "@bob:localhost") == "leave"


@pytest.mark.asyncio
async def test_a_failed_guest_removal_is_retried_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member event never raises, so a failure cannot hold the room's event lane."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    room = nio.MatrixRoom(room_id, server.user_id)
    room.add_member("@alice:localhost", None, None)
    room.add_member("@bob:localhost", None, None, invited=True)
    server.set_member(room_id, "@bob:localhost", "invite")
    server.fail_kick = True

    await owner.guest_membership_event(room, "@bob:localhost", "invite")
    assert server.membership(room_id, "@bob:localhost") == "invite"
    with pytest.raises(RuntimeError, match="removal"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)

    server.fail_kick = False
    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert server.membership(room_id, "@bob:localhost") == "leave"


@pytest.mark.asyncio
async def test_existing_alias_requires_creator_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias collision never authorizes mutation of somebody else's room, guests included."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, "@eve:localhost", "join")
    marker = next(event for event in server.state[room_id] if event["type"] == "org.mindroom.personal_room")
    marker["content"] = {}
    with pytest.raises(RuntimeError, match=r"ownership|member"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert server.membership(room_id, "@eve:localhost") == "join"
    assert len(server.messages) == 1


@pytest.mark.asyncio
async def test_two_users_have_distinct_rooms_and_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Room lifecycle storage never reads or combines requester-private agent state."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    rooms = {await owner.ensure(user, "!lobby:localhost", server) for user in sorted(server.lobby_members)}
    owner.runtime.config.personal_rooms = None
    assert retained_personal_rooms(owner.runtime_paths, "helper") == rooms
    assert len(rooms) == 2


@pytest.mark.asyncio
async def test_alias_creation_response_loss_is_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An alias created before a lost response is validated and reused."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    create = server.room_create

    async def lose_response(**kwargs: Any) -> object:
        await create(**kwargs)
        return nio.RoomCreateError("response lost", "M_UNKNOWN")

    monkeypatch.setattr(server, "room_create", lose_response)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert room_id == "!personal1:localhost"
    assert server.create_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["creator", "marker", "join_rule", "history"])
async def test_alias_adoption_rejects_unrelated_or_exposed_rooms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    """Alias lookup alone cannot grant room ownership or weaken privacy."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    kinds = {
        "creator": "m.room.create",
        "marker": "org.mindroom.personal_room",
        "join_rule": "m.room.join_rules",
        "history": "m.room.history_visibility",
    }
    event = next(item for item in server.state[room_id] if item["type"] == kinds[tamper])
    if tamper == "creator":
        event["sender"] = "@eve:localhost"
    else:
        event["content"] = {}
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    record.room_id = None
    write_personal_room(path, record)
    with pytest.raises(RuntimeError, match="ownership"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)


@pytest.mark.asyncio
async def test_unconfigured_source_cannot_onboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit command callers cannot bypass the configured onboarding boundary."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    assert await owner.ensure("@alice:localhost", "!elsewhere:localhost", server) is None
    assert not server.state


def bots(tmp_path: Path, server: MatrixServer, monkeypatch: pytest.MonkeyPatch, **settings: object) -> tuple[Any, Any]:
    """Build real router and target bot shells around the stateful transport."""
    owner = service(tmp_path, server, monkeypatch, **settings)
    config = owner.runtime.config
    persist_entity_accounts(config, owner.runtime_paths)
    result = []
    for name in ("router", "helper"):
        user = AgentMatrixUser(
            agent_name=name,
            user_id=f"@mindroom_{name}:localhost",
            display_name=name,
            password=TEST_PASSWORD,
        )
        bot = make_test_agent_bot(user, tmp_path / name, config=config, runtime_paths=owner.runtime_paths)
        install_runtime_journal_support(bot)
        bot.client = server
        result.append(bot)
    router, target = result
    router.orchestrator = SimpleNamespace(agent_bots={"router": router, "helper": target})
    target._first_sync_done = True
    return router, target


@pytest.mark.asyncio
async def test_router_join_creates_room_without_hooks_or_target_lobby_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native onboarding must run even when no plugin registers a membership hook."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch)
    await _dispatch_member(router, nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id), _room_member_event())
    assert server.create_count == 1
    assert target.rooms == []


@pytest.mark.asyncio
async def test_self_command_ignores_forged_requester_and_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands onboard only the authenticated sender and accept no target argument."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch, commands=["!personal"])
    room = nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$cmd",
            "sender": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {
                "msgtype": "m.text",
                "body": "!personal",
                ORIGINAL_SENDER_KEY: "@bob:localhost",
                SOURCE_KIND_KEY: "hook_dispatch",
            },
        },
    )
    assert await router._on_message(room, event) is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert [record.user_id for record in _personal_room_records(router.runtime_paths, "helper")] == ["@alice:localhost"]
    event.body = "!personal @bob:localhost"
    assert not await router._personal_room_lifecycle.handle_command(room, event)
    assert server.create_count == 1


@pytest.mark.asyncio
async def test_optional_backfill_and_cleanup_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backfill creates both rooms and ordinary cleanup retains them after disabling."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch, backfill=True)
    await router._personal_room_lifecycle._reconcile()
    assert server.create_count == 2
    target.config.personal_rooms = None
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=list(server.state)))
    assert await target._room_lifecycle._rooms_to_leave() == []


@pytest.mark.asyncio
async def test_invalid_retained_room_does_not_block_router_sync_or_other_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One owner's invalid room stays retryable while sync readiness and other owners progress."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch, backfill=True)
    alice_room = await target.personal_rooms.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(alice_room, "@outsider:localhost", "invite")
    ready = AsyncMock()
    router.orchestrator.handle_bot_ready = ready
    monkeypatch.setattr(router, "_refresh_agent_reply_memberships_if_needed", AsyncMock())
    monkeypatch.setattr(router, "_schedule_delivery_recovery", lambda: None)

    await router._run_sync_response_side_effects(first_sync_response=False)

    ready.assert_awaited_once_with(router)
    assert await wait_for_background_tasks(timeout=2, owner=router._runtime_view)
    assert server.create_count == 2
    assert read_personal_room(personal_room_record_path(router.runtime_paths, "helper", "@alice:localhost"))
    assert read_personal_room(personal_room_record_path(router.runtime_paths, "helper", "@bob:localhost"))
    await router._personal_room_lifecycle._reconcile()
    assert server.create_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "revoke"])
async def test_policy_change_during_membership_lookup_stops_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A stale membership result cannot authorize creation or requester privileges."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, requester_admin=True)
    original = server.joined_members
    reads = 0

    async def joined_members(room_id: str) -> object:
        nonlocal reads
        result = await original(room_id)
        reads += 1
        if reads == 2:
            if change == "disable":
                owner.runtime.config.personal_rooms = None
            else:
                owner.runtime.config.agents["helper"].access.users = []
        return result

    monkeypatch.setattr(server, "joined_members", joined_members)
    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) is None
    assert server.create_count == 0
    assert not server.state


@pytest.mark.asyncio
async def test_policy_change_during_alias_lookup_stops_room_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alias resolution cannot carry stale creation authority past an await."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, requester_admin=True)
    original = server.room_resolve_alias

    async def resolve(alias: str) -> object:
        result = await original(alias)
        owner.runtime.config.agents["helper"].access.users = []
        return result

    monkeypatch.setattr(server, "room_resolve_alias", resolve)
    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) is None
    assert server.create_count == 0


@pytest.mark.asyncio
async def test_access_revoke_during_invite_prevents_admin_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invitation in flight cannot authorize a later admin grant."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, requester_admin=True)
    original = server.room_invite

    async def invite(room_id: str, user_id: str) -> object:
        result = await original(room_id, user_id)
        owner.runtime.config.agents["helper"].access.users = []
        return result

    monkeypatch.setattr(server, "room_invite", invite)
    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) is None
    assert server.create_count == 1
    power = next(
        event["content"] for event in server.state["!personal1:localhost"] if event["type"] == "m.room.power_levels"
    )
    assert power.get("users", {}).get("@alice:localhost", 0) < 100


@pytest.mark.asyncio
async def test_retention_recovers_create_before_local_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup cleanup cannot leave a created room whose local receipt was interrupted."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch)

    def fail_room_receipt(path: Path, record: Any) -> None:
        if record.room_id is not None:
            message = "interrupted receipt"
            raise OSError(message)
        write_personal_room(path, record)

    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", fail_room_receipt)
    with pytest.raises(OSError, match="interrupted"):
        await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=list(server.state)))
    assert await target._room_lifecycle._rooms_to_leave() == []
    # Alias resolution protects cleanup, but a missing room receipt cannot authorize a join.
    target.config.personal_rooms = None
    server.rooms.clear()
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    join_room = AsyncMock()
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    await target._room_lifecycle.join_configured_rooms()
    join_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_directory_room_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An owned room exposed in the directory cannot receive a private welcome."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    monkeypatch.setattr(
        server,
        "room_get_visibility",
        AsyncMock(
            return_value=nio.RoomGetVisibilityResponse.from_dict({"visibility": "public"}, "!personal1:localhost"),
        ),
    )
    with pytest.raises(RuntimeError, match=r"private|ownership"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert not server.messages


@pytest.mark.asyncio
async def test_welcome_receipt_failure_reuses_successful_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport success followed by local failure must not publish another welcome."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)

    def fail_welcome_receipt(path: Path, record: Any) -> None:
        if record.welcome_event_id is not None:
            message = "interrupted receipt"
            raise OSError(message)
        write_personal_room(path, record)

    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", fail_welcome_receipt)
    with pytest.raises(OSError, match="interrupted"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", write_personal_room)
    await service(tmp_path, server, monkeypatch).ensure("@alice:localhost", "!lobby:localhost", server)
    assert len(server.messages) == 1


@pytest.mark.asyncio
async def test_pending_dispatch_keeps_authorization_after_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling dispatch cannot send a previously frozen prompt for a departed user."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome_dispatch=True)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, "@alice:localhost", "join")
    server.fail_send = True
    with pytest.raises(RuntimeError, match="welcome"):
        await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    server.set_member(room_id, "@alice:localhost", "leave")
    server.fail_send = False
    owner.runtime.config.personal_rooms.welcome_dispatch = False
    await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert not server.messages


@pytest.mark.asyncio
async def test_requester_admin_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the configured policy grants the requester room administration."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, requester_admin=True)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    power = next(event["content"] for event in server.state[room_id] if event["type"] == "m.room.power_levels")
    assert power["users"]["@alice:localhost"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["access", "feature", "admin_setting"])
async def test_policy_change_during_admin_state_read_prevents_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoke: str,
) -> None:
    """A power-level read cannot carry stale authority into its following write."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, requester_admin=True)
    original = server.room_get_state_event

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        response = await original(room_id, event_type, state_key)
        if event_type == "m.room.power_levels":
            if revoke == "access":
                owner.runtime.config.agents["helper"].access.users = []
            elif revoke == "feature":
                owner.runtime.config.personal_rooms = None
            else:
                owner.runtime.config.personal_rooms.requester_admin = False
        return response

    monkeypatch.setattr(server, "room_get_state_event", read_state)
    await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    power = next(
        event["content"] for event in server.state["!personal1:localhost"] if event["type"] == "m.room.power_levels"
    )
    assert power.get("users", {}).get("@alice:localhost", 0) < 100


@pytest.mark.asyncio
async def test_access_revoke_during_avatar_upload_prevents_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uploaded bytes cannot authorize an avatar update after access is revoked."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, avatar="avatar.png", welcome="")

    async def upload(_client: object, _path: Path) -> str:
        owner.runtime.config.agents["helper"].access.users = []
        return "mxc://localhost/uploaded"

    monkeypatch.setattr("mindroom.matrix.avatar._upload_avatar_file", upload)
    await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert not any(event["type"] == "m.room.avatar" for event in server.state["!personal1:localhost"])
    record = read_personal_room(personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost"))
    assert not record.avatar_done


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["welcome", "confirmation"])
async def test_access_revoke_during_message_preparation_prevents_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    """A pending personal-room notice cannot send after preparation loses authority."""
    server = MatrixServer()
    options = {"welcome": "Ready"} if message == "welcome" else {"welcome": "", "confirmation": "Ready"}
    owner = service(tmp_path, server, monkeypatch, **options)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.send_message_result", send_message_result)
    server.room_send = AsyncMock(return_value=nio.RoomSendResponse("$unexpected", "!personal1:localhost"))

    async def prepare(_client: object, _room_id: str, content: dict[str, Any], **_kwargs: object) -> SimpleNamespace:
        owner.runtime.config.agents["helper"].access.users = []
        return SimpleNamespace(content=content, cache_bypass=False)

    monkeypatch.setattr("mindroom.matrix.client_delivery._prepare_matrix_message", prepare)
    await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_backfill_does_not_create_for_existing_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default startup touches only existing lifecycle records."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch)
    await router._personal_room_lifecycle._reconcile()
    assert not server.state


@pytest.mark.asyncio
async def test_config_reload_enables_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously disabled backfill must run after an ordinary config reload."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch)
    await router._personal_room_lifecycle._reconcile()
    config = personal_config(backfill=True)
    router.config = config
    target.config = config
    await router._personal_room_lifecycle._reconcile()
    assert server.create_count == 2


@pytest.mark.asyncio
async def test_private_welcome_requester_reaches_existing_ingress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private agents receive the human requester through the established trust boundary."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch, welcome_dispatch=True)
    target.config.agents["helper"].private = AgentPrivateConfig(per="user")
    await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    server.set_member("!personal1:localhost", "@alice:localhost", "join")
    await target.personal_rooms.owner_membership_event("!personal1:localhost", "@alice:localhost", "join")
    content = next(iter(server.messages.values()))["content"]
    assert (
        target._ingress_validator.requester_user_id(sender=server.user_id, source={"content": content})
        == "@alice:localhost"
    )


@pytest.mark.asyncio
async def test_notice_history_is_readable_after_invitation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Welcome sent after invitation must remain readable when the human joins later."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    history = next(event["content"] for event in server.state[room_id] if event["type"] == "m.room.history_visibility")
    assert history["history_visibility"] == "invited"
    assert len(server.messages) == 1


@pytest.mark.asyncio
async def test_pending_welcome_refuses_changed_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ambiguous welcome may not replay under another Matrix transaction scope."""
    server = MatrixServer()
    server.fail_send = True
    owner = service(tmp_path, server, monkeypatch)
    with pytest.raises(RuntimeError, match="welcome"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.fail_send = False
    server.device_id = "OTHER"
    with pytest.raises(RuntimeError, match="device"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert not server.messages


@pytest.mark.asyncio
async def test_native_onboarding_respects_existing_membership_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile events with unknown prior membership must honor the durable baseline."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch)
    await router.journal_principal().mark_room_member_join_completed("!lobby:localhost", "@alice:localhost")
    await _dispatch_member(
        router,
        nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id),
        _room_member_event(prev_membership=None),
    )
    assert not server.state


@pytest.mark.asyncio
async def test_genuine_rejoin_reinvites_existing_personal_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A known leave-to-join transition remains useful after hook completion."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch)
    room = nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id)
    await _dispatch_member(router, room, _room_member_event(event_id="$first"))
    server.set_member("!personal1:localhost", "@alice:localhost", "leave")
    await _dispatch_member(router, room, _room_member_event(event_id="$again"))
    membership = next(
        event for event in server.state["!personal1:localhost"] if event["state_key"] == "@alice:localhost"
    )
    assert membership["content"]["membership"] == "invite"
    assert server.create_count == 1


@pytest.mark.asyncio
async def test_reinvite_uses_current_onboarding_room_after_original_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified rejoin in the replacement onboarding room can re-invite the owner."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, "@alice:localhost", "leave")
    move_onboarding_to_new_room(owner, server)

    assert (
        await owner.ensure(
            "@alice:localhost",
            "!new:localhost",
            server,
            reinvite_departed_owner=True,
        )
        == room_id
    )
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {"membership": "invite"}


@pytest.mark.asyncio
async def test_banned_owner_is_not_reinvited_on_genuine_rejoin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An onboarding rejoin cannot override a personal-room ban."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch)
    room = nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id)
    await _dispatch_member(router, room, _room_member_event(event_id="$first"))
    server.set_member("!personal1:localhost", "@alice:localhost", "ban")
    await _dispatch_member(router, room, _room_member_event(event_id="$again"))
    membership = next(
        event for event in server.state["!personal1:localhost"] if event["state_key"] == "@alice:localhost"
    )
    assert membership["content"]["membership"] == "ban"


@pytest.mark.asyncio
@pytest.mark.parametrize(("adopted", "reinvite_departed_owner"), [(False, False), (True, False), (True, True)])
async def test_direct_reconciliation_preserves_departed_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adopted: bool,
    reinvite_departed_owner: bool,
) -> None:
    """A direct retry cannot silently re-invite a departed owner."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    if adopted:
        path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
        record = read_personal_room(path)
        record.adoption = PersonalRoomAdoption(creator_user_id=server.user_id, agent_user_id=server.user_id)
        write_personal_room(path, record)
    server.set_member(room_id, "@alice:localhost", "leave")
    assert (
        await service(tmp_path, server, monkeypatch, welcome="").ensure(
            "@alice:localhost",
            "!lobby:localhost",
            server,
            reinvite_departed_owner=reinvite_departed_owner,
        )
        == room_id
    )
    membership = next(event for event in server.state[room_id] if event["state_key"] == "@alice:localhost")
    assert membership["content"]["membership"] == "leave"


@pytest.mark.asyncio
async def test_restart_backfill_preserves_departed_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded startup work and onboarding backfill cannot undo a personal-room departure."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch, backfill=True)
    await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    room_id = "!personal1:localhost"
    server.set_member(room_id, "@alice:localhost", "leave")
    restarted_router, _ = bots(tmp_path, server, monkeypatch, backfill=True)
    await restarted_router._personal_room_lifecycle._reconcile()
    membership = next(event for event in server.state[room_id] if event["state_key"] == "@alice:localhost")
    assert membership["content"]["membership"] == "leave"


@pytest.mark.asyncio
async def test_authorization_rechecked_after_waiting_for_room_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued trigger loses authority when access changes before it owns the room."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    observed_members = asyncio.Event()
    joined_members = server.joined_members

    async def observe(room_id: str) -> object:
        result = await joined_members(room_id)
        observed_members.set()
        return result

    monkeypatch.setattr(server, "joined_members", observe)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    async with async_exclusive_file_lock(path.with_suffix(".lock")):
        pending = asyncio.create_task(owner.ensure("@alice:localhost", "!lobby:localhost", server))
        await observed_members.wait()
        owner.runtime.config.agents["helper"].access.users = []
    assert await pending is None
    assert not server.state


@pytest.mark.asyncio
async def test_avatar_missing_file_retries_without_recreating_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avatar configuration uses existing upload service and survives partial failure."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, avatar="personal.png")
    with pytest.raises(RuntimeError, match="avatar"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    (tmp_path / "personal.png").write_bytes(b"image")

    async def upload(_client: object, data: bytes, **_kwargs: object) -> object:
        assert data == b"image"
        return nio.UploadResponse("mxc://localhost/personal")

    monkeypatch.setattr("mindroom.matrix.avatar.upload_media_bytes", upload)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    avatar = next(event["content"] for event in server.state[room_id] if event["type"] == "m.room.avatar")
    assert avatar == {"url": "mxc://localhost/personal"}
    assert server.create_count == 1


@pytest.mark.asyncio
async def test_imported_welcome_completion_needs_no_event_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-recorded completed history cannot trigger another welcome."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    data = record.model_dump()
    data["welcome_completed"] = True
    path.write_text(json.dumps(data))
    restarted = service(tmp_path, server, monkeypatch)
    assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert not server.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [None, "mxc://localhost/custom"])
async def test_requester_avatar_fills_only_empty_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: str | None,
) -> None:
    """An optional requester avatar never replaces a room's chosen avatar."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    if existing:
        await server.room_put_state(room_id, "m.room.avatar", {"url": existing})
    server.get_profile = AsyncMock(return_value=nio.ProfileGetResponse("Alice", "mxc://localhost/alice", {}))
    restarted = service(tmp_path, server, monkeypatch, avatar_from_requester=True)
    await restarted.ensure("@alice:localhost", "!lobby:localhost", server)
    avatar = await server.room_get_state_event(room_id, "m.room.avatar")
    assert avatar.content == {"url": existing or "mxc://localhost/alice"}


@pytest.mark.asyncio
async def test_onboarding_confirmation_is_configurable_and_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful onboarding sends its configured confirmation once across restarts."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, confirmation="Ready {user}: {room}")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    await service(tmp_path, server, monkeypatch, confirmation="Changed").ensure(
        "@alice:localhost",
        "!lobby:localhost",
        server,
    )
    confirmations = [message for message in server.messages.values() if message["room_id"] == "!lobby:localhost"]
    assert len(confirmations) == 1
    assert confirmations[0]["content"]["msgtype"] == "m.notice"
    assert confirmations[0]["content"]["body"] == "Ready @alice:localhost: #personal_481a6d9da9e7b569dd3c:localhost"
    assert room_id == "!personal1:localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "unseeded", "creator", "agent", "router", "marker", "admin", "outsider"])
async def test_operator_seed_preserves_room_identity_with_strict_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str | None,
) -> None:
    """Only an exact trusted seed permits a router-created room with its router present."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    persist_entity_accounts(owner.runtime.config, owner.runtime_paths)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    router_id = "@mindroom_router:localhost"
    creator = next(event for event in server.state[room_id] if event["type"] == "m.room.create")
    creator["sender"] = router_id
    creator["content"]["creator"] = router_id
    server.set_member(room_id, router_id, "join")
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    data = read_personal_room(path).model_dump()
    data["welcome_completed"] = True
    if tamper != "unseeded":
        data["adoption"] = {
            "creator_user_id": router_id,
            "agent_user_id": server.user_id,
            "router_user_id": router_id,
        }
        if tamper in {"creator", "agent", "router"}:
            data["adoption"][f"{tamper}_user_id"] = "@wrong:localhost"
    if tamper == "marker":
        marker = next(event for event in server.state[room_id] if event["type"] == "org.mindroom.personal_room")
        marker["content"]["user_id"] = "@bob:localhost"
    if tamper == "admin":
        power = next(event for event in server.state[room_id] if event["type"] == "m.room.power_levels")
        power["content"]["users"][server.user_id] = 0
    if tamper == "outsider":
        server.set_member(room_id, "@eve:localhost", "invite")
    path.write_text(json.dumps(data))
    restarted = service(tmp_path, server, monkeypatch)
    if tamper:
        with pytest.raises(RuntimeError, match="ownership"):
            await restarted.ensure("@alice:localhost", "!lobby:localhost", server)
    else:
        assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
        assert retained_personal_rooms(owner.runtime_paths, "helper") == {room_id}
    assert server.create_count == 1
    assert not server.messages


@pytest.mark.asyncio
async def test_router_retains_explicitly_adopted_membership_after_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adopted router member survives cleanup even after onboarding is disabled."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch)
    await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    room_id = "!personal1:localhost"
    path = personal_room_record_path(router.runtime_paths, "helper", "@alice:localhost")
    data = read_personal_room(path).model_dump()
    data["adoption"] = {
        "creator_user_id": "@mindroom_router:localhost",
        "agent_user_id": server.user_id,
        "router_user_id": "@mindroom_router:localhost",
    }
    path.write_text(json.dumps(data))
    router.config.personal_rooms = None
    router.client = SimpleNamespace(user_id="@mindroom_router:localhost")
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[room_id]))
    assert await router._room_lifecycle._rooms_to_leave() == []
    target.config.personal_rooms = None
    assert await target._room_lifecycle._rooms_to_leave() == []


@pytest.mark.asyncio
async def test_deferred_join_rechecks_disabled_settings_after_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling onboarding while a join waits must prevent welcome dispatch."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome_dispatch=True)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, "@alice:localhost", "join")
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    async with async_exclusive_file_lock(path.with_suffix(".lock")):
        pending = asyncio.create_task(owner.owner_membership_event(room_id, "@alice:localhost", "join"))
        await asyncio.sleep(0)
        owner.runtime.config.personal_rooms = None
    await pending
    assert not server.messages


@pytest.mark.asyncio
async def test_confirmation_receipt_failure_preserves_frozen_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivered confirmation survives lost local receipt and changed template."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, confirmation="Ready {user}")

    def lose_confirmation_receipt(path: Path, record: Any) -> None:
        if record.confirmation_event_id is not None:
            message = "interrupted confirmation receipt"
            raise OSError(message)
        write_personal_room(path, record)

    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", lose_confirmation_receipt)
    with pytest.raises(OSError, match="confirmation"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", write_personal_room)
    await service(tmp_path, server, monkeypatch, confirmation="Changed").ensure(
        "@alice:localhost",
        "!lobby:localhost",
        server,
    )
    confirmations = [message for message in server.messages.values() if message["room_id"] == "!lobby:localhost"]
    assert len(confirmations) == 1
    assert confirmations[0]["content"]["body"] == "Ready @alice:localhost"


@pytest.mark.asyncio
async def test_avatar_unknown_state_does_not_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable avatar read cannot authorize replacement of existing state."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    await server.room_put_state(room_id, "m.room.avatar", {"url": "mxc://localhost/custom"})
    monkeypatch.setattr(
        server,
        "room_get_state_event",
        AsyncMock(return_value=nio.RoomGetStateEventError("retry", "M_UNKNOWN")),
    )
    restarted = service(tmp_path, server, monkeypatch, avatar_from_requester=True)
    with pytest.raises(RuntimeError, match="avatar state"):
        await restarted.ensure("@alice:localhost", "!lobby:localhost", server)
    avatar = next(event["content"] for event in server.state[room_id] if event["type"] == "m.room.avatar")
    assert avatar == {"url": "mxc://localhost/custom"}


def test_operator_seed_cannot_authorize_alias_only() -> None:
    """A durable trust seed must name its exact immutable room ID."""
    with pytest.raises(ValidationError, match="exact room_id"):
        PersonalRoomRecord(
            user_id="@alice:localhost",
            alias="#existing:localhost",
            source_room_id="!lobby:localhost",
            adoption=PersonalRoomAdoption(
                creator_user_id="@mindroom_router:localhost",
                agent_user_id="@mindroom_helper:localhost",
            ),
        )


@pytest.mark.parametrize("history", ["shared", "joined"])
@pytest.mark.asyncio
async def test_imported_private_policy_accepts_only_attested_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: str,
) -> None:
    """An imported room keeps its exact private history and permitted existing roster."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    record.welcome_completed = True
    record.adoption = PersonalRoomAdoption(
        creator_user_id=server.user_id,
        agent_user_id=server.user_id,
        expected_history_visibility=history,
        additional_user_ids=["@guest:localhost"],
    )
    write_personal_room(path, record)
    await server.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": history})
    server.set_member(room_id, "@guest:localhost", "join")

    restarted = service(tmp_path, server, monkeypatch)
    assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    server.set_member(room_id, "@guest:localhost", "leave")
    assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert next(event for event in server.state[room_id] if event["type"] == "m.room.history_visibility")[
        "content"
    ] == {
        "history_visibility": history,
    }
    assert next(event for event in server.state[room_id] if event.get("state_key") == "@guest:localhost")[
        "content"
    ] == {
        "membership": "leave",
    }
    power = next(event["content"] for event in server.state[room_id] if event["type"] == "m.room.power_levels")
    assert power.get("users", {}).get("@guest:localhost", 0) < 100
    assert server.create_count == 1
    assert not server.messages


@pytest.mark.parametrize("tamper", ["history", "outsider", "public_history", "public_directory", "public_join"])
@pytest.mark.asyncio
async def test_imported_private_policy_fails_closed_on_remote_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    """An attestation cannot authorize changed history, an outsider, or public access."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    record.adoption = PersonalRoomAdoption(
        creator_user_id=server.user_id,
        agent_user_id=server.user_id,
        expected_history_visibility="shared",
        additional_user_ids=["@guest:localhost"],
    )
    write_personal_room(path, record)
    await server.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": "shared"})
    server.set_member(room_id, "@guest:localhost", "invite")
    if tamper == "history":
        await server.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": "joined"})
    elif tamper == "outsider":
        server.set_member(room_id, "@eve:localhost", "knock")
    elif tamper == "public_history":
        await server.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": "world_readable"})
    elif tamper == "public_directory":
        monkeypatch.setattr(
            server,
            "room_get_visibility",
            AsyncMock(return_value=nio.RoomGetVisibilityResponse.from_dict({"visibility": "public"}, room_id)),
        )
    else:
        await server.room_put_state(room_id, "m.room.join_rules", {"join_rule": "public"})
    with pytest.raises(RuntimeError, match=r"directory|ownership"):
        await service(tmp_path, server, monkeypatch).ensure("@alice:localhost", "!lobby:localhost", server)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_history_visibility", "world_readable"),
        ("expected_history_visibility", "public"),
        ("additional_user_ids", ["guest:localhost"]),
        ("additional_user_ids", ["@guest:localhost", "@guest:localhost"]),
        ("additional_user_ids", ["@guest:*not-concrete"]),
    ],
)
def test_imported_policy_rejects_invalid_durable_records(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """A malformed operator seed cannot become trusted room policy."""
    paths = test_runtime_paths(tmp_path)
    path = personal_room_record_path(paths, "helper", "@alice:localhost")
    data = PersonalRoomRecord(
        user_id="@alice:localhost",
        alias="#existing:localhost",
        source_room_id="!lobby:localhost",
        room_id="!existing:localhost",
    ).model_dump()
    data["adoption"] = {"creator_user_id": "@creator:localhost", "agent_user_id": "@helper:localhost", field: value}
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        read_personal_room(path)


def test_imported_policy_defaults_remain_strict_after_durable_read(tmp_path: Path) -> None:
    """Older seeds retain invited history and no additional permitted member."""
    paths = test_runtime_paths(tmp_path)
    path = personal_room_record_path(paths, "helper", "@alice:localhost")
    record = PersonalRoomRecord(
        user_id="@alice:localhost",
        alias="#existing:localhost",
        source_room_id="!lobby:localhost",
        room_id="!existing:localhost",
        adoption=PersonalRoomAdoption(creator_user_id="@creator:localhost", agent_user_id="@helper:localhost"),
    )
    write_personal_room(path, record)
    adoption = read_personal_room(path).adoption
    assert adoption.expected_history_visibility == "invited"
    assert adoption.additional_user_ids == []


def test_durable_write_revalidates_mutated_import_attestation(tmp_path: Path) -> None:
    """A model changed after construction cannot persist invalid import authority."""
    paths = test_runtime_paths(tmp_path)
    path = personal_room_record_path(paths, "helper", "@alice:localhost")
    record = PersonalRoomRecord(
        user_id="@alice:localhost",
        alias="#existing:localhost",
        source_room_id="!lobby:localhost",
        room_id="!existing:localhost",
        adoption=PersonalRoomAdoption(creator_user_id="@creator:localhost", agent_user_id="@helper:localhost"),
    )
    record.adoption.additional_user_ids.append("guest:localhost")
    with pytest.raises(ValidationError, match="additional_user_ids"):
        write_personal_room(path, record)
    assert not path.exists()


@pytest.mark.asyncio
async def test_opt_in_initial_join_only_for_new_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new room may enroll its owner once through the source admin client."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    joins = 0

    async def admin_join(client: MatrixServer, room_id: str, user_id: str) -> bool:
        nonlocal joins
        assert client is server
        joins += 1
        server.set_member(room_id, user_id, "join")
        return True

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "join",
    }
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is False
    await service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="").ensure(
        "@alice:localhost",
        "!lobby:localhost",
        server,
    )
    assert server.create_count == 1
    assert joins == 1


@pytest.mark.asyncio
async def test_default_personal_room_stays_invited_without_admin_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path invites the owner and never requests admin enrollment."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")

    async def forbidden_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        pytest.fail("Default provisioning must not request an admin join")

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", forbidden_join)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "invite",
    }


@pytest.mark.asyncio
async def test_failed_initial_join_retries_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed admin join leaves durable initial enrollment pending."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    attempts = 0

    async def admin_join(_client: MatrixServer, room_id: str, user_id: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        server.set_member(room_id, user_id, "join")
        return True

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    with pytest.raises(RuntimeError, match="initial join"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is True
    room_id = await service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="").ensure(
        "@alice:localhost",
        "!lobby:localhost",
        server,
    )
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "join",
    }
    assert read_personal_room(path).initial_join_pending is False
    assert attempts == 2
    assert server.create_count == 1


@pytest.mark.asyncio
async def test_pending_initial_join_uses_current_onboarding_room_after_original_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending initial join retries through a replacement eligible onboarding room."""
    server = MatrixServer()
    owner = service(
        tmp_path,
        server,
        monkeypatch,
        auto_join_requester=True,
        requester_admin=True,
        avatar="avatar.png",
        welcome="Welcome {user}",
        confirmation="Created",
    )
    attempts = 0

    async def set_avatar(_client: object, room_id: str, _path: Path, **_kwargs: Any) -> bool:
        await server.room_put_state(room_id, "m.room.avatar", {"url": "mxc://localhost/avatar"})
        return True

    async def admin_join(_client: MatrixServer, room_id: str, user_id: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        server.set_member(room_id, user_id, "join")
        return True

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.set_room_avatar_from_file", set_avatar)
    with pytest.raises(RuntimeError, match="initial join"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is True
    move_onboarding_to_new_room(owner, server)

    assert await owner.ensure("@alice:localhost", "!new:localhost", server) == "!personal1:localhost"
    assert attempts == 2
    record = read_personal_room(path)
    assert record.initial_join_pending is False
    assert record.avatar_done is True
    assert record.welcome_completed is True
    assert record.confirmation_event_id is None
    assert len(server.messages) == 1
    assert next(iter(server.messages.values()))["room_id"] == "!personal1:localhost"
    assert (
        next(
            event["content"] for event in server.state["!personal1:localhost"] if event["type"] == "m.room.power_levels"
        )["users"]["@alice:localhost"]
        == 100
    )
    assert next(
        event["content"] for event in server.state["!personal1:localhost"] if event["type"] == "m.room.avatar"
    ) == {"url": "mxc://localhost/avatar"}
    assert next(
        event["content"]
        for event in server.state["!personal1:localhost"]
        if event.get("state_key") == "@alice:localhost"
    ) == {"membership": "join"}


@pytest.mark.asyncio
async def test_initial_join_rechecks_history_after_agent_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent setup cannot carry an earlier private-history decision into an admin join."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    admin_calls = 0

    async def agent_join(room_id: str, _action: str) -> bool:
        await server.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": "world_readable"})
        return True

    async def admin_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        return True

    owner.change_membership = agent_join
    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    with pytest.raises(RuntimeError, match="ownership"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert admin_calls == 0


@pytest.mark.asyncio
async def test_initial_join_rechecks_history_after_invitation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invitation that races with public history cannot authorize an admin join."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    original_invite = server.room_invite
    admin_calls = 0

    async def expose_after_invite(room_id: str, user_id: str) -> object:
        response = await original_invite(room_id, user_id)
        await server.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": "world_readable"})
        return response

    async def admin_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        return True

    monkeypatch.setattr(server, "room_invite", expose_after_invite)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    with pytest.raises(RuntimeError, match="ownership"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert admin_calls == 0


@pytest.mark.asyncio
async def test_initial_join_preserves_departure_during_agent_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A departure while the agent joins must be seen before inviting the owner."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    admin_calls = 0

    async def agent_join(room_id: str, _action: str) -> bool:
        server.set_member(room_id, "@alice:localhost", "leave")
        return True

    async def admin_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        return True

    owner.change_membership = agent_join
    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is False
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "leave",
    }
    assert admin_calls == 0


@pytest.mark.asyncio
async def test_initial_join_preserves_departure_during_invitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invitation response cannot hide a departure before admin joining."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    original_invite = server.room_invite
    admin_calls = 0

    async def depart_after_invite(room_id: str, user_id: str) -> object:
        response = await original_invite(room_id, user_id)
        server.set_member(room_id, user_id, "leave")
        return response

    async def admin_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        return True

    monkeypatch.setattr(server, "room_invite", depart_after_invite)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is False
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "leave",
    }
    assert admin_calls == 0


@pytest.mark.asyncio
async def test_observed_manual_join_retires_initial_admin_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual join ends initial authority even if a later invitation is present."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    admin_calls = 0

    async def failing_admin_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        return False

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", failing_admin_join)
    with pytest.raises(RuntimeError, match="initial join"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    room_id = "!personal1:localhost"
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is True

    server.set_member(room_id, "@alice:localhost", "join")
    await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    assert read_personal_room(path).initial_join_pending is False
    server.set_member(room_id, "@alice:localhost", "leave")
    await server.room_invite(room_id, "@alice:localhost")
    restarted = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert admin_calls == 1
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "invite",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed_membership", "previous_membership"),
    [("join", "invite"), ("join", "join"), ("leave", "invite"), ("ban", "invite")],
)
@pytest.mark.parametrize("policy_change", ["active", "disabled", "access_denied"])
async def test_owner_event_retires_initial_join_after_later_invite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_membership: str,
    previous_membership: str,
    policy_change: str,
) -> None:
    """A processed owner event ends initial authority even if remote state advances."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    admin_calls = 0

    async def admin_join(_client: MatrixServer, room_id: str, user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        if admin_calls == 1:
            return False
        server.set_member(room_id, user_id, "join")
        return True

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    with pytest.raises(RuntimeError, match="initial join"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    room_id = "!personal1:localhost"
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is True
    lifecycle = PersonalRoomLifecycle(
        "helper",
        owner.runtime,
        owner.runtime_paths,
        owner,
        lambda _agent: None,
        lambda event: event.sender,
    )
    server.set_member(room_id, "@alice:localhost", observed_membership)
    event = _room_member_event(membership=observed_membership, prev_membership=previous_membership)
    if observed_membership == "join":
        server.set_member(room_id, "@alice:localhost", "leave")
        await server.room_invite(room_id, "@alice:localhost")
    if policy_change == "disabled":
        owner.runtime.config.personal_rooms = None
    elif policy_change == "access_denied":
        owner.runtime.config.agents["helper"].access.users = []
    await lifecycle.member_event(nio.MatrixRoom(room_id, server.user_id), event)
    assert read_personal_room(path).initial_join_pending is False
    if observed_membership != "join":
        server.set_member(room_id, "@alice:localhost", "leave")
        await server.room_invite(room_id, "@alice:localhost")
    restarted = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert admin_calls == 1
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {"membership": "invite"}


@pytest.mark.asyncio
async def test_owner_event_retirement_requires_recorded_room_and_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated room or member event cannot retire another owner's intent."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="initial join"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    lifecycle = PersonalRoomLifecycle(
        "helper",
        owner.runtime,
        owner.runtime_paths,
        owner,
        lambda _agent: None,
        lambda event: event.sender,
    )
    leave = _room_member_event(membership="leave", prev_membership="invite")
    await lifecycle.member_event(nio.MatrixRoom("!other:localhost", server.user_id), leave)
    await lifecycle.member_event(
        nio.MatrixRoom("!personal1:localhost", server.user_id),
        _room_member_event(user_id="@bob:localhost", membership="leave", prev_membership="invite"),
    )
    assert read_personal_room(path).initial_join_pending is True
    await lifecycle.member_event(nio.MatrixRoom("!personal1:localhost", server.user_id), leave)
    assert read_personal_room(path).initial_join_pending is False


@pytest.mark.asyncio
async def test_unrecorded_owner_event_creates_no_lock_when_feature_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated member events do no disk or network setup without a record."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    owner.runtime.config.personal_rooms = None
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    lifecycle = PersonalRoomLifecycle(
        "helper",
        owner.runtime,
        owner.runtime_paths,
        owner,
        lambda _agent: None,
        lambda event: event.sender,
    )
    await lifecycle.member_event(
        nio.MatrixRoom("!other:localhost", server.user_id),
        _room_member_event(membership="leave", prev_membership="invite"),
    )
    assert not path.exists()
    assert not path.with_suffix(".lock").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("join_timing", ["before_setup", "during_setup"])
async def test_reconciliation_retires_observed_join_before_later_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    join_timing: str,
) -> None:
    """A failed later step cannot preserve initial authority after a validated manual join."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    admin_calls = 0

    async def admin_join(_client: MatrixServer, room_id: str, user_id: str) -> bool:
        nonlocal admin_calls
        admin_calls += 1
        if admin_calls == 1:
            return False
        server.set_member(room_id, user_id, "join")
        return True

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    with pytest.raises(RuntimeError, match="initial join"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    room_id = "!personal1:localhost"
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is True

    if join_timing == "before_setup":
        server.set_member(room_id, "@alice:localhost", "join")

        async def fail_agent_setup(_room_id: str, _action: str) -> bool:
            return False

        owner.change_membership = fail_agent_setup
        failure = "agent membership"
    else:

        async def join_during_setup(_room_id: str, _action: str) -> bool:
            server.set_member(room_id, "@alice:localhost", "join")
            return True

        async def fail_after_join(
            _record: PersonalRoomRecord,
            _membership: str | None,
            _source_room_id: str,
        ) -> None:
            msg = "Later setup failed"
            raise RuntimeError(msg)

        owner.change_membership = join_during_setup
        monkeypatch.setattr(owner, "_invite_owner_if_needed", fail_after_join)
        failure = "Later setup failed"

    with pytest.raises(RuntimeError, match=failure):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert read_personal_room(path).initial_join_pending is False

    server.set_member(room_id, "@alice:localhost", "leave")
    await server.room_invite(room_id, "@alice:localhost")
    restarted = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    assert await restarted.ensure("@alice:localhost", "!lobby:localhost", server) == room_id
    assert admin_calls == 1
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {"membership": "invite"}


@pytest.mark.asyncio
@pytest.mark.parametrize("departure", ["leave", "ban"])
async def test_uncertain_initial_join_never_rejoins_departed_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    departure: str,
) -> None:
    """A lost local completion receipt cannot turn a later departure into another forced join."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    joins = 0

    async def admin_join(_client: MatrixServer, room_id: str, user_id: str) -> bool:
        nonlocal joins
        joins += 1
        server.set_member(room_id, user_id, "join")
        return True

    original_write = write_personal_room

    def lose_completion_receipt(record_path: Path, record: PersonalRoomRecord) -> None:
        if record.room_id and not record.initial_join_pending and joins:
            msg = "lost initial join receipt"
            raise OSError(msg)
        original_write(record_path, record)

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", admin_join)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", lose_completion_receipt)
    with pytest.raises(OSError, match="receipt"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert read_personal_room(path).initial_join_pending is True
    server.set_member("!personal1:localhost", "@alice:localhost", departure)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", original_write)
    await service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="").ensure(
        "@alice:localhost",
        "!lobby:localhost",
        server,
    )
    assert joins == 1
    assert read_personal_room(path).initial_join_pending is False
    assert next(
        event["content"]
        for event in server.state["!personal1:localhost"]
        if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": departure,
    }


@pytest.mark.asyncio
async def test_opt_in_join_never_applies_to_adopted_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An imported room does not inherit newly enabled initial enrollment."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    record.adoption = PersonalRoomAdoption(creator_user_id=server.user_id, agent_user_id=server.user_id)
    write_personal_room(path, record)
    server.set_member(room_id, "@alice:localhost", "leave")

    async def forbidden_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        pytest.fail("An adopted room must not request an admin join")

    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", forbidden_join)
    await service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="").ensure(
        "@alice:localhost",
        "!lobby:localhost",
        server,
    )
    assert read_personal_room(path).initial_join_pending is False
    membership = next(event for event in server.state[room_id] if event.get("state_key") == "@alice:localhost")
    assert membership["content"]["membership"] == "leave"


@pytest.mark.asyncio
async def test_alias_recovery_does_not_claim_initial_join_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost creation response cannot authorize a forced join through alias recovery."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, auto_join_requester=True, welcome="")
    create = server.room_create

    async def lose_response(**kwargs: Any) -> object:
        await create(**kwargs)
        return nio.RoomCreateError("response lost", "M_UNKNOWN")

    async def forbidden_join(_client: MatrixServer, _room_id: str, _user_id: str) -> bool:
        pytest.fail("Alias recovery must not request an admin join")

    monkeypatch.setattr(server, "room_create", lose_response)
    monkeypatch.setattr("mindroom.matrix.personal_rooms.admin_join_room_user", forbidden_join)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    assert read_personal_room(path).initial_join_pending is False
    assert next(
        event["content"] for event in server.state[room_id] if event.get("state_key") == "@alice:localhost"
    ) == {
        "membership": "invite",
    }


@pytest.mark.asyncio
async def test_imported_policy_does_not_change_new_room_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later fresh room cannot inherit another requester's relaxed attestation."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    alice_room = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    path = personal_room_record_path(owner.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    record.adoption = PersonalRoomAdoption(
        creator_user_id=server.user_id,
        agent_user_id=server.user_id,
        expected_history_visibility="shared",
        additional_user_ids=["@guest:localhost"],
    )
    write_personal_room(path, record)
    await server.room_put_state(alice_room, "m.room.history_visibility", {"history_visibility": "shared"})
    server.set_member(alice_room, "@guest:localhost", "join")
    assert await owner.ensure("@alice:localhost", "!lobby:localhost", server) == alice_room

    bob_room = await owner.ensure("@bob:localhost", "!lobby:localhost", server)
    history = next(event["content"] for event in server.state[bob_room] if event["type"] == "m.room.history_visibility")
    assert history == {"history_visibility": "invited"}
    server.set_member(bob_room, "@guest:localhost", "invite")
    assert await owner.ensure("@bob:localhost", "!lobby:localhost", server) == bob_room
    assert server.membership(bob_room, "@guest:localhost") == "leave"
    assert server.membership(alice_room, "@guest:localhost") == "join"


@pytest.mark.asyncio
async def test_extra_router_member_does_not_grant_cleanup_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the separate router field lets cleanup keep a departed router room."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch)
    await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    room_id = "!personal1:localhost"
    path = personal_room_record_path(router.runtime_paths, "helper", "@alice:localhost")
    record = read_personal_room(path)
    record.adoption = PersonalRoomAdoption(
        creator_user_id=server.user_id,
        agent_user_id=server.user_id,
        additional_user_ids=["@mindroom_router:localhost"],
    )
    write_personal_room(path, record)
    router.config.personal_rooms = None
    router.client = SimpleNamespace(user_id="@mindroom_router:localhost")
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[room_id]))
    assert await router._room_lifecycle._rooms_to_leave() == [room_id]


@pytest.mark.asyncio
async def test_self_command_accepts_human_bridge_alias_without_trusting_forged_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured alias keeps its authenticated membership and matches join onboarding."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch, commands=["!personal"])
    router.config.authorization.aliases = {"@alice:localhost": ["@bridge_alice:localhost"]}
    server.lobby_members = {"@bridge_alice:localhost"}
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$alias-command",
            "sender": "@bridge_alice:localhost",
            "origin_server_ts": 1,
            "content": {
                "msgtype": "m.text",
                "body": "!personal",
                ORIGINAL_SENDER_KEY: "@bob:localhost",
                SOURCE_KIND_KEY: "hook_dispatch",
            },
        },
    )
    room = nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id)
    assert await router._on_message(room, event) is TurnDispatchOutcome.INTENTIONALLY_IGNORED
    assert [record.user_id for record in _personal_room_records(router.runtime_paths, "helper")] == [
        "@bridge_alice:localhost",
    ]
    await router._personal_room_lifecycle._onboard(event.sender, room.room_id)
    assert server.create_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("welcome", [None, "Help {user} get started.", "Get started.", "Ask @bob:localhost."])
async def test_dispatched_welcome_selects_target_through_real_turn_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    welcome: str | None,
) -> None:
    """Default and custom onboarding prompts select the private agent without altering requester."""
    server = MatrixServer()
    settings = {} if welcome is None else {"welcome": welcome}
    router, target = bots(tmp_path, server, monkeypatch, welcome_dispatch=True, **settings)
    target.config.agents["helper"].private = AgentPrivateConfig(per="user")
    await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    room_id = "!personal1:localhost"
    server.set_member(room_id, "@alice:localhost", "join")
    await target.personal_rooms.owner_membership_event(room_id, "@alice:localhost", "join")
    content = next(iter(server.messages.values()))["content"]
    room = nio.MatrixRoom(room_id, server.user_id)
    room.add_member(server.user_id, "Helper", None)
    room.add_member("@alice:localhost", "Alice", None)
    server.rooms[room_id] = room
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$onboarding",
            "sender": server.user_id,
            "origin_server_ts": 1,
            "content": content,
        },
    )
    requester = await target._ingress_validator.precheck_event(room, event)
    assert requester == "@alice:localhost"
    prepared = await target._turn_controller._prepare_dispatch(
        room,
        event,
        requester,
        event_label="message",
        handled_turn=TurnRecord.create([event.event_id]),
    )
    assert prepared is not None
    dispatch = prepared.dispatch
    plan = await target._turn_policy.plan_turn(
        room,
        event,
        dispatch,
        is_dm=False,
        has_active_response_for_target=lambda _target: False,
    )
    assert plan.kind == "respond"
    assert plan.response_action.kind == "individual"
    assert dispatch.requester_user_id == "@alice:localhost"
    identity = target._tool_runtime_support.build_execution_identity(
        target=dispatch.target,
        user_id=dispatch.requester_user_id,
    )
    runtime = resolve_agent_runtime("helper", target.config, target.runtime_paths, identity)
    assert runtime.execution.execution_identity.requester_id == "@alice:localhost"
    assert runtime.execution.worker_key == "v1:default:user:~@alice:localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender",
    ["@mindroom_helper:localhost", "@mindroom_router:localhost", "@bridge_bot:localhost"],
)
async def test_self_command_rejects_service_account_original_sender_relays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: str,
) -> None:
    """Service transport cannot request a personal room on behalf of a human."""
    server = MatrixServer()
    router, _ = bots(tmp_path, server, monkeypatch, commands=["!personal"])
    router.config.bot_accounts.append("@bridge_bot:localhost")
    router.config.authorization.aliases = {"@alice:localhost": [sender]}
    server.lobby_members.add(sender)
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$relay-command",
            "sender": sender,
            "origin_server_ts": 1,
            "content": {
                "msgtype": "m.text",
                "body": "!personal",
                ORIGINAL_SENDER_KEY: "@alice:localhost",
                SOURCE_KIND_KEY: "hook_dispatch",
            },
        },
    )
    room = nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id)
    assert await router._personal_room_lifecycle.handle_command(room, event)
    assert server.create_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_welcome", ["Changed", ""])
async def test_deferred_welcome_freezes_intent_before_human_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_welcome: str,
) -> None:
    """An invited human's pending dispatch retains its original content and mode."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome_dispatch=True, welcome="Original {user}")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    assert not server.messages
    owner.runtime.config.personal_rooms.welcome = changed_welcome
    owner.runtime.config.personal_rooms.welcome_dispatch = False
    server.set_member(room_id, "@alice:localhost", "join")
    await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    assert len(server.messages) == 1
    content = next(iter(server.messages.values()))["content"]
    assert content["body"] == "Original @alice:localhost"
    assert content[SOURCE_KIND_KEY] == "hook_dispatch"
    assert content["m.mentions"]["user_ids"] == [server.user_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_record", [False, True])
async def test_unrelated_join_does_not_require_personal_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_record: bool,
) -> None:
    """Unrelated rooms cannot enter personal-room authorization backoff."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    if has_record:
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    before = dict(server.messages)
    owner.runtime.config.agents["helper"].access.users = []
    owner.runtime.config.agents["helper"].access.members_of_rooms = ["lobby"]
    await owner.owner_membership_event("!unrelated:localhost", "@alice:localhost", "join")
    assert server.messages == before


@pytest.mark.asyncio
async def test_bot_personal_service_observes_target_config_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition keeps personal-room policy bound to the target's live runtime."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch, welcome="Before")
    target.config = personal_config(welcome="After")
    await router._personal_room_lifecycle._onboard("@alice:localhost", "!lobby:localhost")
    assert next(iter(server.messages.values()))["content"]["body"] == "After"


@pytest.mark.asyncio
async def test_unready_target_keeps_durable_join_for_dispatcher_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target startup failure remains pending and a fresh dispatcher finishes onboarding."""
    server = MatrixServer()
    router, target = bots(tmp_path, server, monkeypatch)
    target.client = None
    room = nio.MatrixRoom("!lobby:localhost", router.agent_user.user_id)
    event = _room_member_event(event_id="$pending-onboarding")
    dispatcher = router._journal_dispatcher
    await admit_dispatch_event(dispatcher, room, event, EventKind.ROOM_LIFECYCLE, EventClass.ACTIONABLE)
    await dispatcher.drain_once()
    assert await dispatcher.store.is_pending(event.event_id)
    assert server.create_count == 0
    await dispatcher.stop()
    target.client = server
    restarted = JournalDispatcher(
        store=dispatcher.store,
        callbacks=dispatcher.callbacks,
        room_for_id=dispatcher.room_for_id,
        runtime_generation=dispatcher.runtime_generation,
    )
    try:
        await restarted.drain_once()
        assert not await restarted.store.is_pending(event.event_id)
        assert server.create_count == 1
        assert len(server.messages) == 1
    finally:
        await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("membership", "sender"),
    [
        ("leave", "@mindroom_helper:localhost"),
        ("leave", "@alice:localhost"),
        ("ban", "@alice:localhost"),
    ],
)
async def test_personal_service_does_not_override_departed_agent_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    membership: str,
    sender: str,
) -> None:
    """Room ownership is never a license to override agent leave, kick, or ban."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch)
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, server.user_id, membership)
    member = next(event for event in server.state[room_id] if event["state_key"] == server.user_id)
    member["sender"] = sender

    async def change_membership(room_id: str, value: str) -> bool:
        server.set_member(room_id, server.user_id, value)
        return True

    owner.change_membership = change_membership
    with pytest.raises(RuntimeError, match="ownership"):
        await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    member = next(event for event in server.state[room_id] if event["state_key"] == server.user_id)
    assert member["content"]["membership"] == membership
    assert member["sender"] == sender
    assert len(server.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["feature", "access"])
async def test_welcome_delivery_rechecks_policy_after_intent_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoke: str,
) -> None:
    """Durable intent does not authorize a send after policy changes during its write."""
    server = MatrixServer()
    owner = service(tmp_path, server, monkeypatch, welcome="")
    room_id = await owner.ensure("@alice:localhost", "!lobby:localhost", server)
    server.set_member(room_id, "@alice:localhost", "join")
    owner.runtime.config.personal_rooms.welcome = "Original {user}"
    owner.runtime.config.personal_rooms.welcome_dispatch = True

    def write_and_revoke(path: Path, record: Any) -> None:
        write_personal_room(path, record)
        if record.welcome_content is not None:
            if revoke == "feature":
                owner.runtime.config.personal_rooms = None
            else:
                owner.runtime.config.agents["helper"].access.users = []

    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", write_and_revoke)
    await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    assert not server.messages
    monkeypatch.setattr("mindroom.matrix.personal_rooms.write_personal_room", write_personal_room)
    owner.runtime.config = personal_config(welcome="Changed", welcome_dispatch=False)
    await owner.owner_membership_event(room_id, "@alice:localhost", "join")
    assert next(iter(server.messages.values()))["content"]["body"] == "Original @alice:localhost"
