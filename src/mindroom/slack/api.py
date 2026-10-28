"""Minimal Slack Web API client and conversation target parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

SLACK_API_BASE = "https://slack.com/api/"
_TIMEOUT_SECONDS = 10.0
_DEFAULT_RETRY_AFTER_SECONDS = 1
_CONVERSATION_ID = r"[CGD][A-Z0-9]{8,}"
_CONVERSATION_RE = re.compile(_CONVERSATION_ID)
_USER_RE = re.compile(r"[UW][A-Z0-9]{8,}")
_TS_RE = re.compile(r"\d{10}\.\d{6}")
_CONVERSATION_TS_RE = re.compile(rf"({_CONVERSATION_ID}):(\d{{10}}\.\d{{6}})")
_PERMALINK_PATH_RE = re.compile(rf"/archives/({_CONVERSATION_ID})/p(\d{{10}})(\d{{6}})")
_INVALID_TARGET = "Use a Slack conversation ID, <conversation>:<ts>, a message link, or a user ID"


class SlackApiError(Exception):
    """A Slack call that failed; `str()` is Slack's error code."""

    def __init__(self, error: str, *, retry_after: int | None = None, needed_scope: str | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.retry_after = retry_after
        self.needed_scope = needed_scope


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


async def slack_call(client: httpx.AsyncClient, method: str, token: str, **params: object) -> dict[str, Any]:
    """Call one Slack Web API method with a bearer token and return its body."""
    response = await client.post(
        f"{SLACK_API_BASE}{method}",
        headers={"Authorization": f"Bearer {token}"},
        data={key: _form_value(value) for key, value in params.items() if value is not None},
    )
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        retry_after = int(response.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_SECONDS))
        error = "ratelimited"
        raise SlackApiError(error, retry_after=retry_after)
    if not response.is_success:
        error = f"http_{response.status_code}"
        raise SlackApiError(error)
    body: dict[str, Any] = response.json()
    if not body.get("ok"):
        error = str(body.get("error", "unknown_error"))
        raise SlackApiError(error, needed_scope=body.get("needed"))
    return body


def _parse_permalink(target: str) -> SlackTarget | None:
    parts = urlsplit(target)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(".slack.com"):
        return None
    match = _PERMALINK_PATH_RE.fullmatch(parts.path)
    if match is None:
        return None
    channel_id, seconds, micros = match.groups()
    thread_ts = parse_qs(parts.query).get("thread_ts", [None])[0]
    if thread_ts is not None and not _TS_RE.fullmatch(thread_ts):
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


def reply_root(target: SlackTarget) -> str | None:
    """Return the thread root a reply to this target posts under."""
    return target.thread_ts or target.ts
