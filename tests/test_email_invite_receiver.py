"""Tests for the email-invite claim receiver of the account that sent a link."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
import pytest_asyncio

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.email_invites import receiver as receiver_module
from mindroom.email_invites import store
from mindroom.email_invites.receiver import _CLAIM_EVENT_TYPE, _RESULT_EVENT_TYPE, register_email_invite_receiver
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix import client_delivery
from mindroom.matrix.client_delivery import send_message_result
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM = "!project:example.org"
ALICE = "@alice:example.org"
MALLORY = "@mallory:example.org"
BAS = "@bas:example.org"


class Harness:
    """A registered receiver, a fake client, and a real store."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Register the receiver with fakes for room state."""
        self.config = bind_runtime_paths(
            Config(
                agents={"code": AgentConfig(display_name="Code", role="Write code")},
                router=RouterConfig(model="default"),
                models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
                bot_accounts=["@bridge:example.org"],
            ),
            test_runtime_paths(tmp_path),
        )
        self.paths: RuntimePaths = runtime_paths_for(self.config)
        self.account_id = entity_identity_registry(self.config, self.paths).current_id("code").full_id
        self.memberships: dict[str, str | None] = {}
        self.rights: dict[str, bool | None] = {BAS: True, self.account_id: True}
        self.rights_hook: Callable[[], None] = lambda: None
        self.refused: str | None = None
        self.events: list[str] = []
        self.client = AsyncMock()
        self.client.user_id, self.client.device_id = self.account_id, "CODEDEV"
        self.client.rooms = {}
        self.client.room_invite = AsyncMock(return_value=nio.RoomInviteResponse())
        self.notices: list[dict[str, Any]] = []
        self.notice_error = False

        async def membership(room_id: str, _event_type: str, user_id: str) -> object:
            assert room_id == ROOM
            value = self.memberships.get(user_id)
            if value is None:
                return nio.RoomGetStateEventError("absent", "M_NOT_FOUND")
            if value == "unreadable":
                return nio.RoomGetStateEventError("busy", "M_LIMIT_EXCEEDED")
            return nio.RoomGetStateEventResponse({"membership": value}, "m.room.member", user_id, room_id)

        async def may_invite(_client: object, room_id: str, user_id: str, _approvers: object = ()) -> bool | None:
            assert room_id == ROOM
            self.rights_hook()
            return self.rights.get(user_id, False)

        async def send_notice(_client: object, room_id: str, content: dict[str, Any]) -> object:
            assert room_id == ROOM
            self.events.append("notice")
            self.notices.append(content)
            return None if self.notice_error else MagicMock()

        async def refused(*_args: object) -> str | None:
            return self.refused

        async def to_device(_message: object) -> nio.ToDeviceResponse:
            self.events.append("result")
            return nio.ToDeviceResponse("txn")

        self.client.room_get_state_event = AsyncMock(side_effect=membership)
        self.client.to_device = AsyncMock(side_effect=to_device)
        monkeypatch.setattr(receiver_module, "may_invite", may_invite)
        monkeypatch.setattr(receiver_module, "send_message_result", send_notice)
        monkeypatch.setattr(receiver_module, "refused_room_kind", refused)
        self.callbacks: list[Callable[[Any], Awaitable[None]]] = []
        self.client.add_to_device_callback = MagicMock(
            side_effect=lambda callback, _type: self.callbacks.append(callback),
        )

    async def start(self) -> None:
        """Register the receiver, as the `code` agent's bot does at each start."""
        self.callbacks.clear()
        await register_email_invite_receiver(
            client=self.client,
            agent_name="code",
            runtime_paths=self.paths,
            config_getter=lambda: self.config,
            callback_wrapper=lambda callback: callback,
        )

    def sent_invite(self) -> tuple[store.EmailInvite, str]:
        """Store one sent invite and return it with its code."""
        return store.issue_code(
            self.paths,
            store.Request(ROOM, "$thread", "alice@example.com", BAS, "code"),
            expiry_days=7,
        )

    async def claim(
        self,
        sender: str,
        code: str,
        request_id: str = "r1",
        *,
        content: dict[str, object] | None = None,
        event_class: type[nio.UnknownToDeviceEvent] = nio.UnknownToDeviceEvent,
    ) -> dict[str, object] | None:
        """Deliver one claim and return the result content, or None without an answer."""
        self.client.to_device.reset_mock()
        body = content if content is not None else {"version": 1, "request_id": request_id, "code": code}
        source = {"type": _CLAIM_EVENT_TYPE, "sender": sender, "content": body}
        if event_class is nio.AuthenticatedToDeviceEvent:
            device = nio.AuthenticatedDevice(sender, "DEV", "curve-key", "ed-key")
            event: nio.UnknownToDeviceEvent = nio.AuthenticatedToDeviceEvent(source, sender, _CLAIM_EVENT_TYPE, device)
        else:
            event = nio.UnknownToDeviceEvent(source, sender, _CLAIM_EVENT_TYPE)
        (callback,) = self.callbacks
        await callback(event)
        if not self.client.to_device.await_count:
            return None
        (message,) = self.client.to_device.await_args.args
        assert message.type == _RESULT_EVENT_TYPE
        assert (message.recipient, message.recipient_device) == (sender, "*")
        return message.content

    def row(self, invite_id: int) -> store.EmailInvite:
        """Return one row whatever its state."""
        with store._connection(self.paths) as connection:
            (row,) = store._select(connection, "id = ?", (invite_id,))
        return row


@pytest_asyncio.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """The `code` agent's receiver with one approver who may invite."""
    started = Harness(tmp_path, monkeypatch)
    await started.start()
    return started


@pytest.mark.asyncio
async def test_valid_claim_invites_answers_and_posts_notice(harness: Harness) -> None:
    """A valid claim invites the sender, answers invited, and tells the room."""
    invite, code = harness.sent_invite()
    result = await harness.claim(ALICE, code)
    assert result == {"version": 1, "request_id": "r1", "status": "invited", "room_id": ROOM}
    harness.client.room_invite.assert_awaited_once_with(ROOM, ALICE)
    (notice,) = harness.notices
    assert notice["body"] == f"Invite for alice@example.com (approved by {BAS}) was used by {ALICE}."
    assert notice["msgtype"] == "m.notice"
    assert notice["m.relates_to"]["event_id"] == "$thread"
    row = harness.row(invite.id)
    assert row.claimed_by == ALICE
    assert row.invited_at is not None


@pytest.mark.asyncio
async def test_unknown_revoked_and_expired_codes_answer_rejected_alike(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown, revoked and expired codes get the same answer."""
    revoked, revoked_code = harness.sent_invite()
    store.revoke(harness.paths, revoked.id)
    results = [await harness.claim(ALICE, "unknown-code", "r1"), await harness.claim(ALICE, revoked_code, "r2")]
    expired, expired_code = harness.sent_invite()
    real_find = store.find_by_code
    monkeypatch.setattr(
        receiver_module.store,
        "find_by_code",
        lambda paths, code: real_find(paths, code, now=expired.created_at + 8 * 86400),
    )
    results.append(await harness.claim(ALICE, expired_code, "r3"))
    assert [result and {**result, "request_id": None} for result in results] == [
        {"version": 1, "request_id": None, "status": "rejected"},
    ] * 3
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {"version": 1, "request_id": "r1", "code": "c", "extra": 1},
        {"version": "1", "request_id": "r1", "code": "c"},
        {"version": True, "request_id": "r1", "code": "c"},
        {"version": 1, "request_id": "r1", "code": ""},
        {"version": 1, "request_id": "r1", "code": "c" * 129},
        {"version": 1, "request_id": "r" * 65, "code": "c"},
        {"version": 1, "code": "c"},
    ],
)
async def test_malformed_claims_get_no_answer(harness: Harness, content: dict[str, object]) -> None:
    """Any other shape is ignored without an answer."""
    assert await harness.claim(ALICE, "", content=content) is None


@pytest.mark.asyncio
async def test_agent_and_bot_senders_are_ignored(harness: Harness) -> None:
    """An agent's own account and configured bot accounts are not claimants."""
    _invite, code = harness.sent_invite()
    assert await harness.claim(harness.account_id, code) is None
    assert await harness.claim("@bridge:example.org", code) is None
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_per_sender_and_global_rate_limits(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 11th claim of one sender and the 61st overall in a minute are dropped before the store."""
    reads: list[str] = []
    monkeypatch.setattr(receiver_module.store, "find_by_code", lambda _paths, code: reads.append(code))
    for index in range(10):
        assert await harness.claim(ALICE, f"c{index}") is not None
    assert await harness.claim(ALICE, "c10") is None
    assert len(reads) == 10
    for index in range(50):
        assert await harness.claim(f"@user{index}:example.org", "c") is not None
    assert await harness.claim("@late:example.org", "c") is None
    assert len(reads) == 60


@pytest.mark.asyncio
async def test_second_account_is_rejected(harness: Harness) -> None:
    """Once Alice used the code, Mallory gets rejected."""
    _invite, code = harness.sent_invite()
    await harness.claim(ALICE, code)
    assert (await harness.claim(MALLORY, code) or {})["status"] == "rejected"
    harness.client.room_invite.assert_awaited_once_with(ROOM, ALICE)


@pytest.mark.asyncio
async def test_restart_between_bind_and_invite(harness: Harness) -> None:
    """A row bound before a restart invites its account and rejects any other."""
    invite, code = harness.sent_invite()
    assert store.bind_claim(harness.paths, invite.id, ALICE)
    assert (await harness.claim(MALLORY, code) or {})["status"] == "rejected"
    assert (await harness.claim(ALICE, code) or {})["status"] == "invited"
    harness.client.room_invite.assert_awaited_once_with(ROOM, ALICE)


@pytest.mark.asyncio
async def test_approver_demoted_revokes(harness: Harness) -> None:
    """An approver who lost the right revokes the invite."""
    invite, code = harness.sent_invite()
    harness.rights[BAS] = False
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    assert harness.row(invite.id).revoked is True
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_room_revokes(harness: Harness) -> None:
    """A room that became a personal room revokes the invite."""
    invite, code = harness.sent_invite()
    harness.refused = "personal"
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    assert harness.row(invite.id).revoked is True


@pytest.mark.asyncio
async def test_unreadable_state_answers_retry_and_keeps_binding(harness: Harness) -> None:
    """Unreadable approver rights answer retry; the claim stays bound and open."""
    invite, code = harness.sent_invite()
    harness.rights[BAS] = None
    assert (await harness.claim(ALICE, code) or {})["status"] == "retry"
    row = harness.row(invite.id)
    assert row.claimed_by == ALICE
    assert row.revoked is False


@pytest.mark.asyncio
async def test_account_without_right_answers_retry(harness: Harness) -> None:
    """An account that lost the invite right answers retry."""
    _invite, code = harness.sent_invite()
    harness.rights[harness.account_id] = False
    assert (await harness.claim(ALICE, code) or {})["status"] == "retry"
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [nio.RoomInviteError("forbidden", "M_FORBIDDEN"), nio.RoomInviteError("unknown", "M_UNKNOWN"), TimeoutError()],
)
async def test_failed_invite_kills_the_link_with_one_notice(harness: Harness, outcome: object) -> None:
    """Any outcome but success revokes the link with one notice; it never sends again, and a new link can be sent."""
    invite, code = harness.sent_invite()
    harness.client.room_invite = AsyncMock(side_effect=[outcome])
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    assert (await harness.claim(ALICE, code, "r2") or {})["status"] == "rejected"
    harness.client.room_invite.assert_awaited_once()
    assert harness.row(invite.id).revoked is True
    (notice,) = harness.notices
    assert "may or may not have gone out" in notice["body"]
    assert f"{BAS} can send a new link if needed" in notice["body"]
    fresh, fresh_code = harness.sent_invite()
    assert fresh.id != invite.id
    assert fresh_code != code


@pytest.mark.asyncio
async def test_removed_member_gets_no_second_invite(harness: Harness) -> None:
    """A used link answers invited again without a second Matrix invite."""
    _invite, code = harness.sent_invite()
    await harness.claim(ALICE, code)
    harness.memberships[ALICE] = "leave"
    assert (await harness.claim(ALICE, code) or {})["status"] == "invited"
    harness.client.room_invite.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_joined_claimant_does_not_use_up_the_code(harness: Harness) -> None:
    """A member who presents an unbound code is answered invited, and the code stays unbound."""
    invite, code = harness.sent_invite()
    harness.memberships["@bob:example.org"] = "join"
    assert (await harness.claim("@bob:example.org", code) or {})["status"] == "invited"
    row = harness.row(invite.id)
    assert row.claimed_by is None
    assert row.invited_at is None


@pytest.mark.asyncio
async def test_banned_claimant_is_rejected(harness: Harness) -> None:
    """A banned account is rejected."""
    _invite, code = harness.sent_invite()
    harness.memberships[ALICE] = "ban"
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_failure_keeps_result(harness: Harness) -> None:
    """A failed notice still answers invited."""
    invite, code = harness.sent_invite()
    harness.notice_error = True
    assert (await harness.claim(ALICE, code) or {})["status"] == "invited"
    assert harness.row(invite.id).invited_at is not None


@pytest.mark.asyncio
async def test_a_link_is_answered_and_abandoned_only_by_the_account_that_sent_it(harness: Harness) -> None:
    """Another entity's link is rejected here, and this account's restart leaves that entity's send alone."""
    harness.client.add_to_device_callback.assert_called_once()
    assert harness.client.add_to_device_callback.call_args.args[1] is nio.UnknownToDeviceEvent
    request = store.Request(ROOM, "$thread", "bob@example.com", BAS, "other")
    invite, code = store.issue_code(harness.paths, request, expiry_days=7)
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    assert harness.row(invite.id).claimed_by is None
    assert store.bind_claim(harness.paths, invite.id, ALICE)
    assert store.start_sending(harness.paths, invite.id, ALICE)
    await harness.start()
    assert harness.row(invite.id).revoked is False
    assert harness.notices == []
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_claim_is_handled(harness: Harness) -> None:
    """The same claim decrypted from Olm invites as well."""
    _invite, code = harness.sent_invite()
    result = await harness.claim(ALICE, code, event_class=nio.AuthenticatedToDeviceEvent)
    assert (result or {})["status"] == "invited"


@pytest.mark.asyncio
async def test_revoke_during_rights_read_wins(harness: Harness) -> None:
    """A revoke that lands while the rights are read means no Matrix invite follows."""
    invite, code = harness.sent_invite()
    revoked: list[bool] = []
    harness.rights_hook = lambda: revoked.append(store.revoke(harness.paths, invite.id)) if not revoked else None
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    assert revoked == [True]
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_during_rights_read_wins(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link that expires while the rights are read lets no one in."""
    invite, code = harness.sent_invite()
    harness.rights_hook = lambda: monkeypatch.setattr(store.time, "time", lambda: invite.expires_at + 1)
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlapping_claims_send_one_invite(harness: Harness) -> None:
    """Two claims of the same account at once: exactly one Matrix invite and one notice; the loser never sends."""
    _invite, code = harness.sent_invite()
    release = asyncio.Event()

    async def slow_invite(_room: str, _user: str) -> nio.RoomInviteResponse:
        await release.wait()
        return nio.RoomInviteResponse()

    harness.client.room_invite = AsyncMock(side_effect=slow_invite)
    (callback,) = harness.callbacks
    claims = [asyncio.create_task(callback(_event(ALICE, code, f"r{n}"))) for n in (1, 2)]
    await asyncio.wait(claims, return_when=asyncio.FIRST_COMPLETED)
    release.set()
    await asyncio.gather(*claims)
    statuses = [call.args[0].content["status"] for call in harness.client.to_device.await_args_list]
    assert statuses == ["rejected", "invited"]
    assert len(harness.notices) == 1
    harness.client.room_invite.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreadable_membership_answers_retry_without_binding(harness: Harness) -> None:
    """A membership read that fails is not taken as a fresh claimant."""
    invite, code = harness.sent_invite()
    harness.memberships[ALICE] = "unreadable"
    assert (await harness.claim(ALICE, code) or {})["status"] == "retry"
    assert harness.row(invite.id).claimed_by is None
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_crash_while_sending_revokes_the_link_at_restart(harness: Harness) -> None:
    """A crash after the invite went out but before it was recorded: at restart the link is revoked, with one notice.

    The newcomer joined and was removed meanwhile; the same link sends no second invite.
    """
    invite, code = harness.sent_invite()
    assert store.bind_claim(harness.paths, invite.id, ALICE)
    assert store.start_sending(harness.paths, invite.id, ALICE)
    harness.memberships[ALICE] = "leave"
    await harness.start()
    assert harness.row(invite.id).revoked is True
    (notice,) = harness.notices
    assert "may or may not have gone out" in notice["body"]
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    harness.client.room_invite.assert_not_awaited()
    await harness.start()
    assert len(harness.notices) == 1


@pytest.mark.asyncio
async def test_notice_in_an_encrypted_room_survives_the_claimants_join(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The join right after the result makes nio's member read refuse once; the notice is still sent, encrypted."""
    _invite, code = harness.sent_invite()
    delivered: list[object] = []

    async def recorded_send(client: nio.AsyncClient, room_id: str, content: dict[str, Any]) -> object:
        delivered.append(await send_message_result(client, room_id, content))
        return delivered[-1]

    monkeypatch.setattr(receiver_module, "send_message_result", recorded_send)
    monkeypatch.setattr(client_delivery.crypto, "ENCRYPTION_ENABLED", True)
    harness.client.olm = MagicMock()
    harness.client.rooms = {ROOM: MagicMock(encrypted=True)}
    members: list[str] = []
    refusals = [nio.LocalProtocolError("room membership changed during lookup")]

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        if refusals:
            raise refusals.pop()
        members.append(room_id)
        return nio.JoinedMembersResponse([], room_id)

    async def room_send(room_id: str, **_kwargs: object) -> nio.RoomSendResponse:
        # As nio's encrypted send: it reads the members first unless they are already known.
        if room_id not in members:
            await harness.client.joined_members(room_id)
        return nio.RoomSendResponse("$notice", room_id)

    harness.client.joined_members = AsyncMock(side_effect=joined_members)
    harness.client.room_send = AsyncMock(side_effect=room_send)
    assert (await harness.claim(ALICE, code) or {})["status"] == "invited"
    (notice,) = delivered
    assert notice is not None
    assert harness.client.room_send.await_args.kwargs["content"]["msgtype"] == "m.notice"


@pytest.mark.asyncio
async def test_result_is_answered_before_the_notice(harness: Harness) -> None:
    """The claimant hears the result before the room notice is sent."""
    _invite, code = harness.sent_invite()
    await harness.claim(ALICE, code)
    assert harness.events == ["result", "notice"]


@pytest.mark.asyncio
async def test_room_that_became_a_space_revokes(harness: Harness) -> None:
    """A space refuses the claim and revokes the invite."""
    invite, code = harness.sent_invite()
    harness.refused = "space"
    assert (await harness.claim(ALICE, code) or {})["status"] == "rejected"
    assert harness.row(invite.id).revoked is True


def _event(sender: str, code: str, request_id: str) -> nio.UnknownToDeviceEvent:
    content = {"version": 1, "request_id": request_id, "code": code}
    return nio.UnknownToDeviceEvent(
        {"type": _CLAIM_EVENT_TYPE, "sender": sender, "content": content},
        sender,
        _CLAIM_EVENT_TYPE,
    )
