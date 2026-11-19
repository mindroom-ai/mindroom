"""Tests for the router's email-invite claim receiver."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.email_invites import receiver as receiver_module
from mindroom.email_invites import store
from mindroom.email_invites.receiver import _CLAIM_EVENT_TYPE, _RESULT_EVENT_TYPE, register_email_invite_receiver
from mindroom.entity_resolution import entity_identity_registry
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
                router=RouterConfig(model="default"),
                models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
                bot_accounts=["@bridge:example.org"],
            ),
            test_runtime_paths(tmp_path),
        )
        self.paths: RuntimePaths = runtime_paths_for(self.config)
        self.router_id = entity_identity_registry(self.config, self.paths).current_id(ROUTER_AGENT_NAME).full_id
        self.memberships: dict[str, str | None] = {}
        self.rights: dict[str, bool | None] = {BAS: True, self.router_id: True}
        self.refused: str | None = None
        self.client = AsyncMock()
        self.client.room_invite = AsyncMock(return_value=nio.RoomInviteResponse())
        self.client.to_device = AsyncMock(return_value=nio.ToDeviceResponse("txn"))
        self.notices: list[dict[str, Any]] = []
        self.notice_error = False

        async def membership(_client: object, room_id: str, user_id: str) -> str | None:
            assert room_id == ROOM
            return self.memberships.get(user_id)

        async def may_invite(_client: object, room_id: str, user_id: str, _approvers: object = ()) -> bool | None:
            assert room_id == ROOM
            return self.rights.get(user_id, False)

        async def send_notice(_client: object, room_id: str, content: dict[str, Any]) -> object:
            assert room_id == ROOM
            self.notices.append(content)
            return None if self.notice_error else MagicMock()

        monkeypatch.setattr(receiver_module, "get_room_membership", membership)
        monkeypatch.setattr(receiver_module, "may_invite", may_invite)
        monkeypatch.setattr(receiver_module, "send_message_result", send_notice)
        monkeypatch.setattr(receiver_module, "refused_room_kind", lambda *_args: self.refused)
        self.callbacks: list[Callable[[Any], Awaitable[None]]] = []
        self.client.add_to_device_callback = MagicMock(
            side_effect=lambda callback, _type: self.callbacks.append(callback),
        )
        register_email_invite_receiver(
            client=self.client,
            agent_name=ROUTER_AGENT_NAME,
            runtime_paths=self.paths,
            config_getter=lambda: self.config,
            callback_wrapper=lambda callback: callback,
        )

    def sent_invite(self) -> tuple[store.EmailInvite, str]:
        """Store one sent invite and return it with its code."""
        invite = store.create_request(
            self.paths,
            room_id=ROOM,
            thread_id="$thread",
            email="alice@example.com",
            requested_by=BAS,
            agent_name="code",
            expiry_days=7,
        )
        return invite, store.issue_code(self.paths, invite.id, inviter_id=BAS, expiry_days=7)

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
            return store._select(connection, "id = ?", (invite_id,))[0]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """A router receiver with one approver who may invite."""
    return Harness(tmp_path, monkeypatch)


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
    """The router itself and configured bot accounts are not claimants."""
    _invite, code = harness.sent_invite()
    assert await harness.claim(harness.router_id, code) is None
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
async def test_router_without_right_answers_retry(harness: Harness) -> None:
    """A router that lost the invite right answers retry."""
    _invite, code = harness.sent_invite()
    harness.rights[harness.router_id] = False
    assert (await harness.claim(ALICE, code) or {})["status"] == "retry"
    harness.client.room_invite.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (nio.RoomInviteError("forbidden", "M_FORBIDDEN"), "rejected"),
        (nio.RoomInviteError("unknown", "M_UNKNOWN"), "retry"),
        (TimeoutError(), "retry"),
    ],
)
async def test_forbidden_invite_is_rejected_other_errors_retry(
    harness: Harness,
    outcome: object,
    status: str,
) -> None:
    """M_FORBIDDEN is final; other failures may be retried; the claim stays bound."""
    invite, code = harness.sent_invite()
    harness.client.room_invite = (
        AsyncMock(side_effect=outcome)
        if isinstance(outcome, Exception)
        else AsyncMock(
            return_value=outcome,
        )
    )
    assert (await harness.claim(ALICE, code) or {})["status"] == status
    row = harness.row(invite.id)
    assert row.claimed_by == ALICE
    assert row.invited_at is None


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
async def test_registered_for_router_only(harness: Harness) -> None:
    """Only the router registers the to-device callback."""
    harness.client.add_to_device_callback.assert_called_once()
    assert harness.client.add_to_device_callback.call_args.args[1] is nio.UnknownToDeviceEvent
    other = MagicMock()
    register_email_invite_receiver(
        client=other,
        agent_name="code",
        runtime_paths=harness.paths,
        config_getter=lambda: harness.config,
        callback_wrapper=lambda callback: callback,
    )
    other.add_to_device_callback.assert_not_called()


@pytest.mark.asyncio
async def test_encrypted_claim_is_handled(harness: Harness) -> None:
    """The same claim decrypted from Olm invites as well."""
    _invite, code = harness.sent_invite()
    result = await harness.claim(ALICE, code, event_class=nio.AuthenticatedToDeviceEvent)
    assert (result or {})["status"] == "invited"
