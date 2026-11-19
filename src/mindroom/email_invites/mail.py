"""Address check, invite link, the fixed invite mail, and its SMTP send."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import smtplib
import ssl
from contextlib import suppress
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_SMTP_URL_ENV = "MINDROOM_SMTP_URL"
SMTP_FROM_ENV = "MINDROOM_SMTP_FROM"
_MAIL_SUBJECT = "You are invited to a MindRoom room"

_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
_MAX_ADDRESS_LENGTH = 254
_MAX_NAME_LENGTH = 80
_SMTP_TIMEOUT_SECONDS = 20
_DEFAULT_PORTS = {"smtps": 465, "smtp": 587}
_ENCRYPTED_LINE = "This room is encrypted: you will not see messages sent before you joined.\n"


class EmailInviteMailError(Exception):
    """The invite mail could not be sent; the text never holds the SMTP URL."""


def normalize_email(raw: str) -> str | None:
    """Return one plain ASCII address lower-cased, or None when it is anything else."""
    address = raw.strip()
    if len(address) > _MAX_ADDRESS_LENGTH or not _ADDRESS.fullmatch(address):
        return None
    return address.lower()


def invite_link(chat_url: str, router_id: str, code: str) -> str:
    """Return the Chat link that carries the router ID and the code in its fragment."""
    return f"{chat_url}/invite/#r={quote(router_id, safe='')}&c={quote(code, safe='')}"


def smtp_configured(paths: RuntimePaths) -> bool:
    """Return whether both SMTP variables are set."""
    return bool(paths.env_value(_SMTP_URL_ENV)) and bool(paths.env_value(SMTP_FROM_ENV))


def _clean_name(value: str) -> str:
    """Collapse whitespace, drop non-printable characters, and cut to 80 characters."""
    printable = "".join(character for character in " ".join(value.split()) if character.isprintable())
    return " ".join(printable.split())[:_MAX_NAME_LENGTH].rstrip()


def build_invite_mail(
    *,
    sender: str,
    recipient: str,
    inviter_name: str,
    inviter_id: str,
    room_name: str,
    link: str,
    expires_at: int,
    encrypted: bool,
) -> EmailMessage:
    """Build the fixed plain-text invite mail for one recipient."""
    expiry = datetime.fromtimestamp(expires_at, UTC).strftime("%Y-%m-%d")
    body = (
        f'{_clean_name(inviter_name)} ({inviter_id}) invited you to the room "{_clean_name(room_name)}" on MindRoom.\n'
        "\n"
        "Open this link to join. You can create an account or sign in with one you have:\n"
        f"{link}\n"
        "\n"
        f"The link works once and expires on {expiry} (UTC).\n"
        f"{_ENCRYPTED_LINE if encrypted else ''}"
        "If you did not expect this invitation, ignore this mail.\n"
    )
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = _MAIL_SUBJECT
    message.set_content(body)
    return message


def _is_loopback(host: str) -> bool:
    with suppress(ValueError):
        return ipaddress.ip_address(host).is_loopback
    return host.lower() == "localhost"


def _send(smtp_url: str, message: EmailMessage) -> None:
    try:
        parsed = urlsplit(smtp_url)
        host, port = parsed.hostname, parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
    except ValueError:
        host = port = None
    if not host or port is None:
        msg = f"{_SMTP_URL_ENV} is malformed"
        raise EmailInviteMailError(msg)
    context = ssl.create_default_context()
    try:
        if parsed.scheme == "smtps":
            client = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
        with client:
            if parsed.scheme == "smtp" and not _is_loopback(host):
                client.starttls(context=context)
            if parsed.username:
                client.login(unquote(parsed.username), unquote(parsed.password or ""))
            client.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        code = f" {exc.smtp_code}" if isinstance(exc, smtplib.SMTPResponseException) else ""
        raise EmailInviteMailError(type(exc).__name__ + code) from None


async def send_invite_mail(paths: RuntimePaths, message: EmailMessage) -> None:
    """Send one invite mail over the configured SMTP server in a worker thread."""
    await asyncio.to_thread(_send, paths.env_value(_SMTP_URL_ENV, default=""), message)
