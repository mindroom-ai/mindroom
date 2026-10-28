"""Tests for the minimal Slack Web API client and target parsing."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from mindroom.slack.api import (
    SlackApiError,
    SlackTarget,
    parse_slack_target,
    reply_root,
    slack_call,
    slack_http_client,
)

_MESSAGE_TS = "1712687433.668649"
_ROOT_TS = "1712687000.000100"
_REJECT_MESSAGE = "Use a Slack conversation ID, <conversation>:<ts>, a message link, or a user ID"


def _client(handler: Any) -> httpx.AsyncClient:  # noqa: ANN401
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10.0)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("C0123ABCD", SlackTarget("C0123ABCD", None, None, None)),
        ("G0123ABCD", SlackTarget("G0123ABCD", None, None, None)),
        ("D0123ABCD", SlackTarget("D0123ABCD", None, None, None)),
        (f"C0123ABCD:{_MESSAGE_TS}", SlackTarget("C0123ABCD", _MESSAGE_TS, _MESSAGE_TS, None)),
        (
            "https://example.slack.com/archives/C0123ABCD/p1712687433668649",
            SlackTarget("C0123ABCD", _MESSAGE_TS, None, None),
        ),
        (
            f"https://example.slack.com/archives/C0123ABCD/p1712687433668649?thread_ts={_ROOT_TS}&cid=C0123ABCD",
            SlackTarget("C0123ABCD", _MESSAGE_TS, _ROOT_TS, None),
        ),
        ("U0123ABCD", SlackTarget(None, None, None, "U0123ABCD")),
        ("W0123ABCD", SlackTarget(None, None, None, "W0123ABCD")),
    ],
)
def test_parse_targets(target: str, expected: SlackTarget) -> None:
    """Every accepted form parses to its channel, message, thread root, or user."""
    assert parse_slack_target(target) == expected


@pytest.mark.parametrize(
    "target",
    [
        "general",
        "",
        "#general",
        "C0123ABCD:not-a-ts",
        "https://example.com/archives/C0123ABCD/p1712687433668649",
        "https://example.slack.com.evil.test/archives/C0123ABCD/p1712687433668649",
        "http://example.slack.com/archives/C0123ABCD/p1712687433668649",
        "https://example.slack.com/archives/C0123ABCD/p17126874336686",
        "https://example.slack.com/archives/C0123ABCD/p1712687433668649?thread_ts=oops",
        "https://example.slack.com/team/U0123ABCD",
    ],
)
def test_parse_target_rejects_everything_else(target: str) -> None:
    """Names, foreign hosts, plain HTTP, and malformed links are refused with one message."""
    with pytest.raises(ValueError, match="Use a Slack conversation ID") as excinfo:
        parse_slack_target(target)
    assert str(excinfo.value) == _REJECT_MESSAGE


def test_reply_root_prefers_the_thread_root() -> None:
    """A reply posts under the thread root when the link names one, else under the message."""
    assert reply_root(parse_slack_target(f"C0123ABCD:{_MESSAGE_TS}")) == _MESSAGE_TS
    assert (
        reply_root(parse_slack_target("https://example.slack.com/archives/C0123ABCD/p1712687433668649")) == _MESSAGE_TS
    )
    reply_link = f"https://example.slack.com/archives/C0123ABCD/p1712687433668649?thread_ts={_ROOT_TS}"
    assert reply_root(parse_slack_target(reply_link)) == _ROOT_TS
    assert reply_root(parse_slack_target("C0123ABCD")) is None


@pytest.mark.asyncio
async def test_slack_call_maps_errors() -> None:
    """Slack's failure shapes become SlackApiError; a good call returns the body."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "ok.method":
            return httpx.Response(200, json={"ok": True, "channel": "C0123ABCD"})
        if method == "fail.method":
            return httpx.Response(200, json={"ok": False, "error": "missing_scope", "needed": "channels:history"})
        if method == "limited.method":
            return httpx.Response(429, headers={"Retry-After": "30"}, json={"ok": False, "error": "ratelimited"})
        return httpx.Response(500, text="boom")

    async with _client(handler) as client:
        assert await slack_call(client, "ok.method", "xoxb-test") == {"ok": True, "channel": "C0123ABCD"}

        with pytest.raises(SlackApiError) as missing:
            await slack_call(client, "fail.method", "xoxb-test")
        assert str(missing.value) == "missing_scope"
        assert missing.value.error == "missing_scope"
        assert missing.value.needed_scope == "channels:history"
        assert missing.value.retry_after is None

        with pytest.raises(SlackApiError) as limited:
            await slack_call(client, "limited.method", "xoxb-test")
        assert str(limited.value) == "ratelimited"
        assert limited.value.retry_after == 30
        assert limited.value.needed_scope is None

        with pytest.raises(SlackApiError) as broken:
            await slack_call(client, "broken.method", "xoxb-test")
        assert str(broken.value) == "http_500"
        assert broken.value.retry_after is None

    async with _client(lambda _request: httpx.Response(200, json={"ok": False, "error": "not_in_channel"})) as client:
        with pytest.raises(SlackApiError, match="not_in_channel") as not_in_channel:
            await slack_call(client, "chat.postMessage", "xoxb-test", channel="C0123ABCD")
    assert not_in_channel.value.needed_scope is None


@pytest.mark.asyncio
async def test_token_sent_as_bearer_header_only() -> None:
    """The token travels only in the Authorization header; parameters are form-encoded."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        await slack_call(
            client,
            "chat.postMessage",
            "xoxb-secret-token",
            channel="C0123ABCD",
            text="hello world",
            blocks=[{"type": "markdown", "text": "hi"}],
            unfurl_links=False,
            reply_broadcast=True,
            limit=5,
            thread_ts=None,
        )

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://slack.com/api/chat.postMessage"
    assert request.headers["Authorization"] == "Bearer xoxb-secret-token"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert "xoxb-secret-token" not in str(request.url)
    assert "xoxb-secret-token" not in request.content.decode()
    form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
    assert form == {
        "channel": "C0123ABCD",
        "text": "hello world",
        "blocks": json.dumps([{"type": "markdown", "text": "hi"}]),
        "unfurl_links": "false",
        "reply_broadcast": "true",
        "limit": "5",
    }


def test_slack_http_client_does_not_follow_redirects() -> None:
    """The shared client never follows a redirect and gives up after ten seconds."""
    client = slack_http_client()
    assert client.follow_redirects is False
    assert client.timeout == httpx.Timeout(10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["ok"]),
        httpx.Response(200, json="ok"),
    ],
    ids=["not-json", "json-array", "json-string"],
)
async def test_slack_call_rejects_a_body_that_is_not_a_json_object(response: httpx.Response) -> None:
    """A 2xx answer Slack never sends for a Web API method is an invalid response, not a crash."""
    async with _client(lambda _request: response) as client:
        with pytest.raises(SlackApiError) as invalid:
            await slack_call(client, "auth.test", "xoxb-test")

    assert invalid.value.error == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(("header", "retry_after"), [("abc", None), ("1.5", None), ("-1", None), (None, 1)])
async def test_slack_call_keeps_a_rate_limit_without_an_integer_retry_after(
    header: str | None,
    retry_after: int | None,
) -> None:
    """A 429 stays ratelimited; only a whole-second Retry-After is reported, and a missing one waits one second."""
    headers = {} if header is None else {"Retry-After": header}
    async with _client(lambda _request: httpx.Response(429, headers=headers)) as client:
        with pytest.raises(SlackApiError) as limited:
            await slack_call(client, "auth.test", "xoxb-test")

    assert (limited.value.error, limited.value.retry_after) == ("ratelimited", retry_after)
