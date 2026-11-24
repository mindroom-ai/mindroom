"""Minimal Slack Web API client and conversation target parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

_SLACK_API_BASE = "https://slack.com/api/"
_TIMEOUT_SECONDS = 10.0
_DEFAULT_RETRY_AFTER_SECONDS = 1
# A public or private channel; a conversation is a channel or a direct message.
SLACK_CHANNEL_ID_RE = re.compile(r"[CG][A-Z0-9]{8,}")
# A message's `ts`, in ASCII digits only, which `\d` would not ensure.
SLACK_TS_RE = re.compile(r"[0-9]{10}\.[0-9]{6}")
_CONVERSATION_ID = rf"(?:{SLACK_CHANNEL_ID_RE.pattern}|D[A-Z0-9]{{8,}})"
_CONVERSATION_RE = re.compile(_CONVERSATION_ID)
_USER_RE = re.compile(r"[UW][A-Z0-9]{8,}")
_CONVERSATION_TS_RE = re.compile(rf"({_CONVERSATION_ID}):({SLACK_TS_RE.pattern})")
_PERMALINK_PATH_RE = re.compile(rf"/archives/({_CONVERSATION_ID})/p([0-9]{{10}})([0-9]{{6}})")
_INVALID_TARGET = "Use a Slack conversation ID, <conversation>:<ts>, a message link, or a user ID"
_NOT_A_CONVERSATION = "Use a Slack conversation ID, <conversation>:<ts>, or a message link"
SLACK_UNREACHABLE = "Slack could not be reached; try again"
# Slack's own failures, which a later call may not meet: its HTTP 5xx answers, a request it received cut off
# (`request_timeout`: POST data missing or truncated in transit), its rate limit and an answer that is not JSON.
# Any other Slack error, such as a revoked token or a missing scope, fails every attempt alike.
_TRANSIENT_ERRORS = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout", "invalid_body"},
)
_SERVER_ERROR = re.compile(r"http_5[0-9]{2}")


class SlackApiError(Exception):
    """A Slack call that failed; `str()` is Slack's error code."""

    def __init__(self, error: str, *, retry_after: int | None = None, needed_scope: str | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.retry_after = retry_after
        self.needed_scope = needed_scope

    @property
    def transient(self) -> bool:
        """Whether this is Slack's own failure, which a later call may not meet."""
        return self.error in _TRANSIENT_ERRORS or _SERVER_ERROR.fullmatch(self.error) is not None


@dataclass(frozen=True)
class SlackTarget:
    """What a tool argument points at: a conversation, a message, or a user.

    `ts` is the linked message and `thread_ts` is the root of its thread when the target names one.
    """

    channel_id: str | None
    ts: str | None
    thread_ts: str | None
    user_id: str | None


def slack_http_client() -> httpx.AsyncClient:
    """Return the HTTP client every Slack call uses."""
    return httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT_SECONDS)


def _form_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _retry_after(header: str | None) -> int | None:
    """Return the whole seconds a 429 asks to wait, one without the header, and None for any other value."""
    if header is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    return int(header) if header.isascii() and header.isdigit() else None


async def slack_call(client: httpx.AsyncClient, method: str, token: str, **params: object) -> dict[str, Any]:
    """Call one Slack Web API method with a bearer token and return its body."""
    response = await client.post(
        f"{_SLACK_API_BASE}{method}",
        headers={"Authorization": f"Bearer {token}"},
        data={key: _form_value(value) for key, value in params.items() if value is not None},
    )
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        error = "ratelimited"
        raise SlackApiError(error, retry_after=_retry_after(response.headers.get("Retry-After")))
    if not response.is_success:
        error = f"http_{response.status_code}"
        raise SlackApiError(error)
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        error = "invalid_body"
        raise SlackApiError(error)
    if not body.get("ok"):
        error = str(body.get("error", "unknown_error"))
        raise SlackApiError(error, needed_scope=body.get("needed"))
    return body


def _parse_permalink(target: str) -> SlackTarget | None:
    try:
        parts = urlsplit(target)
    except ValueError:
        # Some malformed links make `urlsplit` raise, such as an unclosed `[` in the host.
        return None
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(".slack.com"):
        return None
    match = _PERMALINK_PATH_RE.fullmatch(parts.path)
    if match is None:
        return None
    channel_id, seconds, micros = match.groups()
    thread_ts = parse_qs(parts.query).get("thread_ts", [None])[0]
    if thread_ts is not None and not SLACK_TS_RE.fullmatch(thread_ts):
        return None
    return SlackTarget(channel_id, f"{seconds}.{micros}", thread_ts, None)


def parse_slack_target(target: str) -> SlackTarget:
    """Parse a conversation ID, `<conversation>:<ts>`, a message permalink, or a user ID."""
    target = target.strip()
    if _CONVERSATION_RE.fullmatch(target):
        return SlackTarget(target, None, None, None)
    if _USER_RE.fullmatch(target):
        return SlackTarget(None, None, None, target)
    if match := _CONVERSATION_TS_RE.fullmatch(target):
        channel_id, ts = match.groups()
        return SlackTarget(channel_id, ts, ts, None)
    if (permalink := _parse_permalink(target)) is not None:
        return permalink
    raise ValueError(_INVALID_TARGET)


def parse_conversation_target(target: str) -> tuple[str, SlackTarget]:
    """Parse a target that names a conversation, refusing a user ID, and return its conversation too."""
    parsed = parse_slack_target(target)
    if parsed.channel_id is None:
        raise ValueError(_NOT_A_CONVERSATION)
    return parsed.channel_id, parsed


def slack_permalink(workspace_url: str, channel_id: str, ts: str, *, thread_ts: str | None = None) -> str:
    """Return the link to one message, as Slack builds it; a reply also names its thread, so the link opens there.

    `workspace_url` is `auth.test`'s `url`, which ends in a slash.
    """
    link = f"{workspace_url}archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts is not None and thread_ts != ts:
        link += f"?thread_ts={thread_ts}&cid={channel_id}"
    return link


def reply_root(target: SlackTarget) -> str | None:
    """Return the thread root a reply to this target posts under."""
    return target.thread_ts or target.ts


def neutralize_broadcasts(text: str) -> str:
    """Defuse Slack's `<!...>` control sequences, such as `<!channel>`, `<!here>` and `<!subteam^...>`.

    Text an agent writes may repeat what untrusted people or documents said, and Slack turns these sequences into
    notifications for everyone in a channel or a user group.
    A word joiner between `<` and `!` keeps them readable but makes Slack treat them as plain text.
    """
    return text.replace("<!", "<\u2060!")
