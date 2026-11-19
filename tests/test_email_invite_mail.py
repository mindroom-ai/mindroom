"""Tests for email-invite config, address check, link, and SMTP mail."""

from __future__ import annotations

import smtplib
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from pydantic import ValidationError

from mindroom.config.email_invites import EmailInvitesConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.email_invites import mail

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

EXPIRES_AT = 1_800_000_000  # 2027-01-15 08:00 UTC


def _paths(tmp_path: Path, **env: str) -> RuntimePaths:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": "", **env},
    )


def _message(**overrides: Any) -> mail.EmailMessage:  # noqa: ANN401
    values: dict[str, Any] = {
        "sender": "MindRoom <invites@example.org>",
        "recipient": "alice@example.com",
        "inviter_name": "Bas",
        "inviter_id": "@bas:example.org",
        "room_name": "Lobby",
        "link": "https://chat.example.org/invite/#r=%40router%3Aexample.org&c=abc",
        "expires_at": EXPIRES_AT,
        "encrypted": False,
    }
    values.update(overrides)
    return mail.build_invite_mail(**values)


def test_normalize_email() -> None:
    """One plain ASCII address is accepted and lower-cased; anything else is refused."""
    assert mail.normalize_email(" Alice@Example.COM ") == "alice@example.com"
    for raw in (
        "Alice <a@b.co>",
        "a@b.co, c@d.co",
        "a@b.co\nBcc: x@y.co",
        "a@b",
        "ä@b.co",
        "a@bücher.de",
        "a" * 64 + "@" + "b" * 186 + ".com",
    ):
        assert mail.normalize_email(raw) is None, raw
    assert len("a" * 64 + "@" + "b" * 186 + ".com") == 255
    assert mail.normalize_email("a@xn--bcher-kva.de") == "a@xn--bcher-kva.de"


def test_invite_link() -> None:
    """Both values are URL-encoded into the fragment."""
    assert (
        mail.invite_link("https://chat.example.org", "@router:example.org", "ab-c_d")
        == "https://chat.example.org/invite/#r=%40router%3Aexample.org&c=ab-c_d"
    )


def test_config_defaults_and_bounds() -> None:
    """Defaults and validation of the email_invites section."""
    config = EmailInvitesConfig()
    assert config.chat_url == "https://chat.mindroom.chat"
    assert config.expiry_days == 7
    assert config.approvers == []
    assert Config().email_invites == config
    for bad in (
        {"expiry_days": 0},
        {"expiry_days": 31},
        {"chat_url": "https://x.org/path"},
        {"chat_url": "http://x.org"},
    ):
        with pytest.raises(ValidationError):
            EmailInvitesConfig(**bad)
    with pytest.raises(ValidationError):
        EmailInvitesConfig(approvers=["bas"])
    with pytest.raises(ValidationError):
        EmailInvitesConfig.model_validate({"unknown": 1})
    assert EmailInvitesConfig(chat_url="http://localhost:4200").chat_url == "http://localhost:4200"
    assert EmailInvitesConfig(approvers=["@bas:example.org"]).approvers == ["@bas:example.org"]


def test_mail_text() -> None:
    """Fixed subject, one recipient, and the body template verbatim."""
    message = _message()
    assert message["Subject"] == mail._MAIL_SUBJECT == "You are invited to a MindRoom room"
    assert message.get_all("To") == ["alice@example.com"]
    assert message["From"] == "MindRoom <invites@example.org>"
    assert message.get_content_type() == "text/plain"
    expected = (
        'Bas (@bas:example.org) invited you to the room "Lobby" on MindRoom.\n'
        "\n"
        "Open this link to join. You can create an account or sign in with one you have:\n"
        "https://chat.example.org/invite/#r=%40router%3Aexample.org&c=abc\n"
        "\n"
        "The link works once and expires on 2027-01-15 (UTC).\n"
        "If you did not expect this invitation, ignore this mail.\n"
    )
    assert message.get_content() == expected
    encrypted = _message(encrypted=True).get_content()
    assert encrypted == expected.replace(
        "(UTC).\n",
        "(UTC).\nThis room is encrypted: you will not see messages sent before you joined.\n",
    )
    assert "encrypted" not in expected


def test_names_are_cleaned() -> None:
    """Names arrive on one line, without control characters, at most 80 characters long."""
    raw = "Mallory\r\nBcc: x@example.org\t" + "x" * 200
    body = _message(inviter_name=raw, room_name=raw).get_content()
    first_line = body.splitlines()[0]
    inviter = first_line.split(" (@bas:example.org)")[0]
    assert inviter.startswith("Mallory Bcc: x@example.org x")
    assert len(inviter) == 80
    room = first_line.split('the room "')[1].split('" on MindRoom')[0]
    assert room == inviter
    assert "\r" not in body
    assert "\t" not in body


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]]

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:  # noqa: ANN401
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        type(self).instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *_args: object) -> None:
        self.calls.append(("quit", ()))

    def starttls(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.calls.append(("starttls", (kwargs.get("context"),)))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))

    def send_message(self, message: mail.EmailMessage) -> None:
        self.calls.append(("send_message", (message,)))


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[_FakeSMTP]]:
    """Replace both smtplib client classes."""

    class FakeSSL(_FakeSMTP):
        instances: ClassVar[list[_FakeSMTP]] = []

    class FakePlain(_FakeSMTP):
        instances: ClassVar[list[_FakeSMTP]] = []

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(smtplib, "SMTP", FakePlain)
    return {"ssl": FakeSSL, "plain": FakePlain}


@pytest.mark.asyncio
async def test_smtps_and_starttls(tmp_path: Path, fake_smtp: dict[str, type[_FakeSMTP]]) -> None:
    """Smtps uses TLS from the start; smtp requires STARTTLS except on loopback."""
    message = _message()
    sender = {"MINDROOM_SMTP_FROM": "invites@example.org"}
    await mail.send_invite_mail(
        _paths(tmp_path / "a", MINDROOM_SMTP_URL="smtps://u:p%40x@mail.example.org", **sender),
        message,
    )
    (ssl_client,) = fake_smtp["ssl"].instances
    assert (ssl_client.host, ssl_client.port) == ("mail.example.org", 465)
    assert ssl_client.kwargs["timeout"] == 20
    assert ssl_client.kwargs["context"] is not None
    assert [name for name, _ in ssl_client.calls] == ["login", "send_message", "quit"]
    assert ssl_client.calls[0][1] == ("u", "p@x")

    await mail.send_invite_mail(_paths(tmp_path / "b", MINDROOM_SMTP_URL="smtp://mail.example.org", **sender), message)
    (plain_client,) = fake_smtp["plain"].instances
    assert (plain_client.host, plain_client.port) == ("mail.example.org", 587)
    assert [name for name, _ in plain_client.calls] == ["starttls", "send_message", "quit"]
    assert plain_client.calls[0][1][0] is not None

    await mail.send_invite_mail(_paths(tmp_path / "c", MINDROOM_SMTP_URL="smtp://localhost:2525", **sender), message)
    local_client = fake_smtp["plain"].instances[1]
    assert (local_client.host, local_client.port) == ("localhost", 2525)
    assert [name for name, _ in local_client.calls] == ["send_message", "quit"]


@pytest.mark.asyncio
async def test_send_failure_hides_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send errors become EmailInviteMailError without the URL or the password."""

    class Failing(_FakeSMTP):
        instances: ClassVar[list[_FakeSMTP]] = []

        def login(self, user: str, password: str) -> None:
            msg = f"auth failed for {user}:{password} smtps://u:p%40x@mail.example.org"
            raise smtplib.SMTPException(msg)

    monkeypatch.setattr(smtplib, "SMTP_SSL", Failing)
    paths = _paths(tmp_path, MINDROOM_SMTP_URL="smtps://u:p%40x@mail.example.org", MINDROOM_SMTP_FROM="a@example.org")
    with pytest.raises(mail.EmailInviteMailError) as caught:
        await mail.send_invite_mail(paths, _message())
    assert "p%40x" not in str(caught.value)
    assert "p@x" not in str(caught.value)
    assert "smtps://" not in str(caught.value)

    for bad_url in ("ftp://mail.example.org", "smtp://", "smtps://u:p%40x@mail.example.org:notaport"):
        bad = _paths(tmp_path / bad_url.split(":")[0], MINDROOM_SMTP_URL=bad_url, MINDROOM_SMTP_FROM="a@example.org")
        with pytest.raises(mail.EmailInviteMailError) as caught:
            await mail.send_invite_mail(bad, _message())
        assert "p%40x" not in str(caught.value)
        assert "mail.example.org" not in str(caught.value)


def test_smtp_configured_needs_both(tmp_path: Path) -> None:
    """SMTP counts as configured only when both variables are set."""
    url = {"MINDROOM_SMTP_URL": "smtps://mail.example.org"}
    sender = {"MINDROOM_SMTP_FROM": "invites@example.org"}
    assert mail.smtp_configured(_paths(tmp_path / "none")) is False
    assert mail.smtp_configured(_paths(tmp_path / "url", **url)) is False
    assert mail.smtp_configured(_paths(tmp_path / "from", **sender)) is False
    assert mail.smtp_configured(_paths(tmp_path / "both", **url, **sender)) is True
