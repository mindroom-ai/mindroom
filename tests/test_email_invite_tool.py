"""Tests for the email_invite toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.approval.auto_approve_windows import start_auto_approve
from mindroom.approval.tool_approval import human_gate
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.custom_tools import email_invite as tool_module
from mindroom.custom_tools.email_invite import EmailInviteTools
from mindroom.email_invites import store
from mindroom.email_invites.mail import EmailInviteMailError, invite_link
from mindroom.message_target import MessageTarget
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from email.message import EmailMessage
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM = "!project:example.org"
BAS = "@bas:example.org"
CAROL = "@carol:example.org"
ROUTER = "@mindroom_router:example.org"


@dataclass
class Harness:
    """One toolkit with its fakes."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    rights: dict[str, bool | None] = field(default_factory=dict)
    router_right: bool | None = True
    smtp: bool = True
    mails: list[EmailMessage] = field(default_factory=list)
    mail_error: EmailInviteMailError | None = None
    approvers: list[str] = field(default_factory=list)
    personal_rooms: dict[str, object] | None = None

    def __post_init__(self) -> None:
        """Build the config, the client and the fakes."""
        self.config = bind_runtime_paths(
            Config(
                agents={"code": AgentConfig(display_name="Code", role="Write code", include_default_tools=False)},
                router=RouterConfig(model="default"),
                models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
                email_invites={"approvers": self.approvers, "chat_url": "https://chat.example.org"},
            ),
            test_runtime_paths(self.tmp_path),
        )
        self.paths: RuntimePaths = runtime_paths_for(self.config)
        self.router_id = tool_module.entity_identity_registry(self.config, self.paths).current_id("router").full_id
        self.client = AsyncMock()

        async def may_invite(_client: object, room_id: str, user_id: str, approvers: object = ()) -> bool | None:
            assert room_id == ROOM
            if user_id == self.router_id:
                return self.router_right
            if user_id in (approvers or ()):
                return True
            return self.rights.get(user_id, False)

        async def send(_paths: object, message: EmailMessage) -> None:
            self.mails.append(message)
            if self.mail_error is not None:
                raise self.mail_error

        self.monkeypatch.setattr(tool_module, "may_invite", may_invite)
        self.monkeypatch.setattr(tool_module, "send_invite_mail", send)
        self.monkeypatch.setattr(tool_module, "smtp_configured", lambda _paths: self.smtp)
        self.monkeypatch.setattr(tool_module, "get_room_name", AsyncMock(return_value="Project"))
        self.monkeypatch.setattr(tool_module, "room_encryption_enabled", AsyncMock(return_value=False))
        self.monkeypatch.setattr(tool_module, "refused_room_kind", AsyncMock(return_value=None))

    async def call(self, function: str, requester: str, *args: str, room_id: str = ROOM) -> str:
        """Run one toolkit function as `requester`."""
        context = make_test_tool_runtime_context(
            agent_name="code",
            target=MessageTarget.resolve(room_id=room_id, thread_id="$thread", reply_to_event_id=None),
            requester_id=requester,
            client=self.client,
            config=self.config,
            runtime_paths=self.paths,
            conversation_reader=make_conversation_reader_mock(),
            relations=make_relation_lookup(),
        )
        with tool_runtime_context(context):
            return await getattr(EmailInviteTools(), function)(*args)

    def rows(self, room_id: str = ROOM) -> list[store.EmailInvite]:
        """Return the open rows of a room."""
        return store.open_invites(self.paths, room_id)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """A toolkit where Bas may invite and Carol may not."""
    return Harness(tmp_path, monkeypatch, rights={BAS: True, CAROL: False})


def _code_from(text: str) -> str:
    return text.rsplit("&c=", 1)[1]


def test_invite_by_email_is_confirm_gated() -> None:
    """Only invite_by_email asks for confirmation."""
    gates = {function.name: function.gate for function in adapt_toolkit(EmailInviteTools()).functions}
    assert gates["invite_by_email"] is ToolGate.CONFIRM
    assert gates["list_email_invites"] is not ToolGate.CONFIRM
    assert gates["revoke_email_invite"] is not ToolGate.CONFIRM
    assert set(gates) == {"invite_by_email", "list_email_invites", "revoke_email_invite"}


def test_auto_approve_window_never_approves_an_invite(harness: Harness) -> None:
    """Inside a window the invite still asks for its card, at the reply and script gates alike."""
    start_auto_approve(harness.paths, room_id=ROOM, thread_id="$thread", requester_id=BAS, duration=timedelta(hours=1))
    gate = {"approver_id": BAS, "room_id": ROOM, "thread_id": "$thread"}
    assert human_gate(harness.paths, **gate, tool_name="invite_by_email") is None
    approved = human_gate(harness.paths, **gate, tool_name="list_email_invites")
    assert approved is not None
    assert approved.approved is True


@pytest.mark.asyncio
async def test_refuses_bad_address(harness: Harness) -> None:
    """A second address or a header line is refused."""
    for raw in ("alice@example.com, bob@example.com", "alice@example.com\nBcc: x@example.org"):
        result = await harness.call("invite_by_email", BAS, raw)
        assert result == "Error: that is not an email address this tool accepts (one plain ASCII address)."
    assert harness.rows() == []
    assert harness.mails == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "text"),
    [("personal", "a personal room"), ("onboarding", "an onboarding room"), ("space", "a space")],
)
async def test_refuses_refused_rooms(harness: Harness, kind: str, text: str) -> None:
    """Personal and onboarding rooms are refused."""
    harness.monkeypatch.setattr(tool_module, "refused_room_kind", AsyncMock(return_value=kind))
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == f"Error: people cannot be invited by email to {text}."
    assert harness.rows() == []
    assert harness.mails == []


@pytest.mark.asyncio
@pytest.mark.parametrize("router_right", [False, None])
async def test_refuses_when_router_may_not_invite(harness: Harness, router_right: bool | None) -> None:
    """A router without the invite right refuses."""
    harness.router_right = router_right
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == "Error: the router may not invite people to this room."
    assert harness.rows() == []
    assert harness.mails == []


@pytest.mark.asyncio
async def test_unreadable_asker_rights(harness: Harness) -> None:
    """Unreadable room state refuses without storing."""
    harness.rights[BAS] = None
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert "room state unreadable" in result
    assert harness.rows() == []


@pytest.mark.asyncio
async def test_asker_without_right_stores_waiting(harness: Harness) -> None:
    """An asker who may not invite stores a waiting request and sends nothing."""
    result = await harness.call("invite_by_email", CAROL, " Alice@Example.com ")
    assert result == (
        f"Stored, nothing sent: {CAROL} may not invite people to this room. Someone who may invite can approve it "
        'by asking an agent here: "approve the invite for alice@example.com".'
    )
    (row,) = harness.rows()
    assert row.inviter_id is None
    assert row.sent_at is None
    assert row.requested_by == CAROL
    assert row.thread_id == "$thread"
    assert harness.mails == []


@pytest.mark.asyncio
async def test_asker_without_right_asks_again(harness: Harness) -> None:
    """Asking again reports the waiting request, then the sent one."""
    await harness.call("invite_by_email", CAROL, "alice@example.com")
    result = await harness.call("invite_by_email", CAROL, "alice@example.com")
    assert result == "An invite for alice@example.com is already waiting for someone who may invite."
    assert len(harness.rows()) == 1
    await harness.call("invite_by_email", BAS, "alice@example.com")
    (row,) = harness.rows()
    date = _date(row.expires_at)
    result = await harness.call("invite_by_email", CAROL, "alice@example.com")
    assert result == f"An invite for alice@example.com was already sent; it is valid until {date}."
    assert len(harness.mails) == 1


def _date(timestamp: int) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_approver_sends_waiting_request(harness: Harness) -> None:
    """A person who may invite sends the waiting request in their name."""
    await harness.call("invite_by_email", CAROL, "alice@example.com")
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    (row,) = harness.rows()
    assert (
        result == f"Invitation mailed to alice@example.com. The link works once and expires on {_date(row.expires_at)}."
    )
    assert row.requested_by == CAROL
    assert row.inviter_id == BAS
    (message,) = harness.mails
    assert message["To"] == "alice@example.com"
    body = message.get_content()
    assert f'({BAS}) invited you to the room "Project"' in body
    code = _code_from(body.split("\n")[3])
    found = store.find_by_code(harness.paths, code)
    assert found is not None
    assert found.id == row.id
    assert invite_link("https://chat.example.org", harness.router_id, code) in body


@pytest.mark.asyncio
async def test_approver_resend_revokes_old_code(harness: Harness) -> None:
    """Sending again for the same address makes the first link stop working."""
    harness.smtp = False
    first = _code_from(await harness.call("invite_by_email", BAS, "alice@example.com"))
    second = _code_from(await harness.call("invite_by_email", BAS, "alice@example.com"))
    assert first != second
    assert store.find_by_code(harness.paths, first) is None
    assert store.find_by_code(harness.paths, second) is not None
    assert len(harness.rows()) == 1


@pytest.mark.asyncio
async def test_listed_approver_not_in_room_may_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listed approver may send without room power."""
    harness = Harness(tmp_path, monkeypatch, approvers=["@ops:example.org"])
    result = await harness.call("invite_by_email", "@ops:example.org", "alice@example.com")
    assert result.startswith("Invitation mailed to alice@example.com.")
    assert harness.rows()[0].inviter_id == "@ops:example.org"


@pytest.mark.asyncio
async def test_smtp_mode_returns_no_code(harness: Harness) -> None:
    """With SMTP the result never carries the code or the link."""
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    body = harness.mails[0].get_content()
    code = _code_from(body.split("\n")[3])
    assert code not in result
    assert "/invite/#" not in result


@pytest.mark.asyncio
async def test_no_smtp_mode_returns_link(harness: Harness) -> None:
    """Without SMTP the result carries the link to forward by hand."""
    harness.smtp = False
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    code = _code_from(result)
    (row,) = harness.rows()
    assert result == (
        "No mail server is configured, so nothing was mailed. Give this link to alice@example.com yourself. "
        f"It works once, expires on {_date(row.expires_at)}, and lets whoever opens it join this room: "
        f"{invite_link('https://chat.example.org', harness.router_id, code)}"
    )
    assert harness.mails == []


@pytest.mark.asyncio
async def test_send_failure_revokes(harness: Harness) -> None:
    """A failed send cancels the invite, and the attempt still counts."""
    harness.mail_error = EmailInviteMailError("connection refused")
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == "Error: the mail could not be sent (connection refused). The invite was cancelled."
    assert harness.rows() == []
    harness.mail_error = None
    for _ in range(2):
        await harness.call("invite_by_email", BAS, "alice@example.com")
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == "Error: limit reached: at most 3 mails per address per day."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "text"),
    [
        ("asker", "at most 5 invites per person per day"),
        ("room", "at most 20 open invites per room"),
        ("address", "at most 3 mails per address per day"),
        ("deployment", "at most 50 mails per day"),
    ],
)
async def test_cap_texts(harness: Harness, limit: str, text: str) -> None:
    """Each cap names itself."""

    def raise_limit(*_args: object, **_kwargs: object) -> None:
        raise store.EmailInviteLimitError(limit)  # type: ignore[arg-type]

    harness.monkeypatch.setattr(tool_module.store, "issue_code", raise_limit)
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == f"Error: limit reached: {text}."
    assert harness.mails == []


@pytest.mark.asyncio
async def test_list_is_room_scoped(harness: Harness) -> None:
    """Only this room's open requests are listed, with their state."""
    await harness.call("invite_by_email", CAROL, "waiting@example.com")
    await harness.call("invite_by_email", BAS, "sent@example.com")
    store.create_request(
        harness.paths,
        store.Request("!other:example.org", None, "elsewhere@example.com", BAS, "code"),
        expiry_days=7,
    )
    result = await harness.call("list_email_invites", BAS)
    assert "elsewhere@example.com" not in result
    waiting_line = next(line for line in result.splitlines() if "waiting@example.com" in line)
    sent_line = next(line for line in result.splitlines() if "sent@example.com" in line)
    assert "waiting" in waiting_line
    assert CAROL in waiting_line
    assert "sent" in sent_line.replace("sent@example.com", "")
    assert BAS in sent_line
    assert await harness.call("list_email_invites", BAS, room_id="!empty:example.org") == (
        "No open email invites in this room."
    )


@pytest.mark.asyncio
async def test_revoke_rules(harness: Harness) -> None:
    """The asker or anyone who may invite may revoke; a stranger may not."""
    await harness.call("invite_by_email", CAROL, "alice@example.com")
    stranger = "@dave:example.org"
    assert await harness.call("revoke_email_invite", stranger, "alice@example.com") == (
        "Error: only the asker, the approver, or someone who may invite can revoke this."
    )
    assert await harness.call("revoke_email_invite", CAROL, "alice@example.com") == (
        "Revoked the invite for alice@example.com."
    )
    assert harness.rows() == []
    assert await harness.call("revoke_email_invite", CAROL, "alice@example.com") == (
        "No open invite for alice@example.com in this room."
    )
    await harness.call("invite_by_email", CAROL, "bob@example.com")
    assert (
        await harness.call("revoke_email_invite", BAS, "bob@example.com") == "Revoked the invite for bob@example.com."
    )


@pytest.mark.asyncio
async def test_revoke_after_use_reports_claimant(harness: Harness) -> None:
    """A used invite is not changed, and the account that used it is named."""
    harness.smtp = False
    code = _code_from(await harness.call("invite_by_email", BAS, "alice@example.com"))
    (row,) = harness.rows()
    assert store.bind_claim(harness.paths, row.id, "@alice:example.org")
    store.mark_invited(harness.paths, row.id)
    result = await harness.call("revoke_email_invite", BAS, "alice@example.com")
    assert result == "The invite for alice@example.com was already used by @alice:example.org; nothing was changed."
    used = store.find_by_code(harness.paths, code)
    assert used is not None
    assert used.revoked is False


@pytest.mark.asyncio
async def test_resend_at_cap_keeps_old_link(harness: Harness) -> None:
    """A resend that a cap refuses leaves the link already sent working."""
    harness.smtp = False
    codes = [_code_from(await harness.call("invite_by_email", BAS, "alice@example.com")) for _ in range(3)]
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == "Error: limit reached: at most 3 mails per address per day."
    assert store.find_by_code(harness.paths, codes[-1]) is not None
    assert len(harness.rows()) == 1


@pytest.mark.asyncio
async def test_unbuildable_mail_cancels_the_invite(harness: Harness) -> None:
    """A sender with a line break cancels the invite with a clean error and leaves no row shown as sent."""
    real = tool_module.build_invite_mail
    bad_sender = {"sender": "a@example.org\nBcc: x@example.org"}
    harness.monkeypatch.setattr(tool_module, "build_invite_mail", lambda **kwargs: real(**{**kwargs, **bad_sender}))
    result = await harness.call("invite_by_email", BAS, "alice@example.com")
    assert result == "Error: the mail could not be sent (the sender address is invalid). The invite was cancelled."
    assert harness.rows() == []


@pytest.mark.asyncio
async def test_revoke_losing_to_a_claim_reports_the_claimant(harness: Harness) -> None:
    """A revoke after the invite was used changes nothing and names the account."""
    harness.smtp = False
    await harness.call("invite_by_email", BAS, "alice@example.com")
    (row,) = harness.rows()
    assert store.bind_claim(harness.paths, row.id, "@alice:example.org")
    assert store.mark_invited(harness.paths, row.id)
    result = await harness.call("revoke_email_invite", BAS, "alice@example.com")
    assert result == "The invite for alice@example.com was already used by @alice:example.org; nothing was changed."
    used = store.used_invite(harness.paths, room_id=ROOM, email="alice@example.com")
    assert used is not None
    assert used.revoked is False
