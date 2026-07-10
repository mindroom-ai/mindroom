"""Wire-format tests for MatrixRTC membership and frame-key events."""

from __future__ import annotations

import httpx
import pytest

from mindroom.matrix_rtc.events import (
    CALL_MEMBER_EVENT_TYPE,
    DEFAULT_MEMBERSHIP_EXPIRES_MS,
    build_key_to_device_content,
    build_membership_content,
    membership_state_key,
    parse_key_to_device_content,
    parse_membership_event,
)
from mindroom.matrix_rtc.focus import OpenIDToken, discover_livekit_service_url, request_sfu_grant

USER = "@alice:example.org"
DEVICE = "DEVICEID"
SERVICE_URL = "https://rtc.example.org"


def _membership_event(content: dict) -> dict:
    return {
        "type": CALL_MEMBER_EVENT_TYPE,
        "state_key": membership_state_key(USER, DEVICE),
        "sender": USER,
        "origin_server_ts": 1_000,
        "content": content,
    }


def test_membership_content_round_trip() -> None:
    """Membership content round trip."""
    content = build_membership_content(
        user_id=USER,
        device_id=DEVICE,
        livekit_service_url=SERVICE_URL,
        expires_ms=DEFAULT_MEMBERSHIP_EXPIRES_MS,
    )
    member = parse_membership_event(_membership_event(content))
    assert member is not None
    assert member.user_id == USER
    assert member.device_id == DEVICE
    assert member.livekit_service_url == SERVICE_URL
    assert member.created_ts == 1_000
    assert not member.is_expired(1_000 + DEFAULT_MEMBERSHIP_EXPIRES_MS - 1)
    assert member.is_expired(1_000 + DEFAULT_MEMBERSHIP_EXPIRES_MS)


def test_membership_content_matches_element_call_shape() -> None:
    """Membership content matches element call shape."""
    content = build_membership_content(
        user_id=USER,
        device_id=DEVICE,
        livekit_service_url=SERVICE_URL,
        expires_ms=123,
        created_ts=42,
    )
    assert content == {
        "application": "m.call",
        "call_id": "",
        "scope": "m.room",
        "device_id": DEVICE,
        "membershipID": f"{USER}:{DEVICE}",
        "expires": 123,
        "created_ts": 42,
        "m.call.intent": "audio",
        "focus_active": {"type": "livekit", "focus_selection": "oldest_membership"},
        "foci_preferred": [{"type": "livekit", "livekit_service_url": SERVICE_URL}],
    }


def test_membership_state_key_has_underscore_prefix_and_application() -> None:
    """Membership state key has underscore prefix and application."""
    assert membership_state_key(USER, DEVICE) == f"_{USER}_{DEVICE}_m.call"


def test_parse_membership_rejects_other_applications_and_leaves() -> None:
    """Parse membership rejects other applications and leaves."""
    content = build_membership_content(
        user_id=USER,
        device_id=DEVICE,
        livekit_service_url=SERVICE_URL,
        expires_ms=123,
    )
    other_app = _membership_event({**content, "application": "m.whiteboard"})
    assert parse_membership_event(other_app) is None
    leave = _membership_event({})
    assert parse_membership_event(leave) is None


@pytest.mark.parametrize("scope", ["m.user", "org.example.other"])
def test_parse_membership_rejects_non_room_scope(scope: str) -> None:
    """User-scoped and unknown sessions cannot enter the room-call roster."""
    content = build_membership_content(
        user_id=USER,
        device_id=DEVICE,
        livekit_service_url=SERVICE_URL,
        expires_ms=123,
    )

    assert parse_membership_event(_membership_event({**content, "scope": scope})) is None


@pytest.mark.parametrize(
    "focus_active",
    [
        None,
        {"type": "other", "focus_selection": "oldest_membership"},
        {"type": "livekit", "focus_selection": "multi_sfu"},
    ],
)
def test_parse_membership_rejects_unsupported_focus_mode(focus_active: object) -> None:
    """Only LiveKit oldest-membership focus mode enters the single-SFU bridge."""
    content = build_membership_content(
        user_id=USER,
        device_id=DEVICE,
        livekit_service_url=SERVICE_URL,
        expires_ms=123,
    )

    assert parse_membership_event(_membership_event({**content, "focus_active": focus_active})) is None


def test_parse_membership_does_not_scan_past_primary_focus() -> None:
    """An incompatible primary transport cannot be bypassed by a later LiveKit entry."""
    content = build_membership_content(
        user_id=USER,
        device_id=DEVICE,
        livekit_service_url=SERVICE_URL,
        expires_ms=123,
    )
    content["foci_preferred"] = [
        {"type": "other", "url": "https://other.example.org"},
        {"type": "livekit", "livekit_service_url": SERVICE_URL},
    ]

    member = parse_membership_event(_membership_event(content))
    assert member is not None
    assert member.livekit_service_url is None


def test_parse_membership_defaults_expiry() -> None:
    """Parse membership defaults expiry."""
    event = _membership_event(
        {
            "application": "m.call",
            "call_id": "",
            "device_id": DEVICE,
            "focus_active": {"type": "livekit", "focus_selection": "oldest_membership"},
            "foci_preferred": [],
        },
    )
    member = parse_membership_event(event)
    assert member is not None
    assert member.expires_ms == DEFAULT_MEMBERSHIP_EXPIRES_MS


def test_key_to_device_content_round_trip() -> None:
    """Key to device content round trip."""
    content = build_key_to_device_content(
        key_base64="a2V5a2V5a2V5a2V5a2V5a2U=",
        key_index=3,
        room_id="!room:example.org",
        member_id=f"{USER}:{DEVICE}",
        device_id=DEVICE,
        sent_ts=777,
    )
    received = parse_key_to_device_content(
        USER,
        content,
        room_id="!room:example.org",
        received_at_ms=888,
    )
    assert received is not None
    assert received.key_index == 3
    assert received.received_at_ms == 888
    # A key for a different room must not leak into this session.
    assert parse_key_to_device_content(USER, content, room_id="!other:example.org", received_at_ms=888) is None


def test_parse_key_content_requires_key_and_claimed_device() -> None:
    """Parse key content requires key and claimed device."""
    room_id = "!room:example.org"
    assert parse_key_to_device_content(USER, {"room_id": room_id}, room_id=room_id, received_at_ms=0) is None
    assert (
        parse_key_to_device_content(
            USER,
            {"room_id": room_id, "keys": {"index": 0, "key": "abc"}, "member": {}},
            room_id=room_id,
            received_at_ms=0,
        )
        is None
    )
    assert (
        parse_key_to_device_content(
            USER,
            {
                "room_id": room_id,
                "keys": {"index": 256, "key": "abc"},
                "member": {"claimed_device_id": DEVICE},
            },
            room_id=room_id,
            received_at_ms=0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_request_sfu_grant_posts_openid_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """Request sfu grant posts openid exchange."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={"url": "wss://sfu.example.org", "jwt": "token123"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def patched_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        kwargs.pop("transport", None)
        return original_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("mindroom.matrix_rtc.focus.httpx.AsyncClient", patched_client)

    def guarded_transport(**kwargs: object) -> httpx.MockTransport:
        seen["transport_kwargs"] = kwargs
        return transport

    monkeypatch.setattr("mindroom.matrix_rtc.focus.ServerFetchAsyncHTTPTransport", guarded_transport)
    grant = await request_sfu_grant(
        "https://rtc.example.org/",
        room_id="!room:example.org",
        device_id=DEVICE,
        openid_token=OpenIDToken(
            access_token="opaque",  # noqa: S106
            expires_in=3600,
            matrix_server_name="example.org",
            token_type="Bearer",  # noqa: S106
        ),
    )
    assert grant.url == "wss://sfu.example.org"
    assert grant.jwt == "token123"
    assert seen["transport_kwargs"]["allow_private_networks"] is False
    assert seen["url"] == "https://rtc.example.org/sfu/get"
    assert b'"room": "!room:example.org"' in seen["body"] or b'"room":"!room:example.org"' in seen["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sfu_url", ["ws://127.0.0.1:7880", "wss://10.0.0.8:7880"])
async def test_request_sfu_grant_accepts_operator_trusted_private_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    sfu_url: str,
) -> None:
    """A trusted local authorization service may return its private LiveKit endpoint."""
    _patched_httpx_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"url": sfu_url, "jwt": "token123"}),
    )

    grant = await request_sfu_grant(
        "http://rtc.internal.example",
        room_id="!room:example.org",
        device_id=DEVICE,
        openid_token=OpenIDToken(
            access_token="opaque",  # noqa: S106
            expires_in=3600,
            matrix_server_name="example.org",
            token_type="Bearer",  # noqa: S106
        ),
        allow_private_networks=True,
    )

    assert grant.url == sfu_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sfu_url",
    [
        "not-a-url",
        "https://sfu.example.org",
        "wss://user:secret@sfu.example.org",
        "wss://sfu.example.org/#fragment",
    ],
)
async def test_request_sfu_grant_rejects_malformed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    sfu_url: str,
) -> None:
    """A trusted authorization service must still return a valid WebSocket URL."""
    _patched_httpx_client(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"url": sfu_url, "jwt": "token123"}),
    )

    with pytest.raises(ValueError, match="invalid SFU URL"):
        await request_sfu_grant(
            "http://rtc.internal.example",
            room_id="!room:example.org",
            device_id=DEVICE,
            openid_token=OpenIDToken(
                access_token="opaque",  # noqa: S106
                expires_in=3600,
                matrix_server_name="example.org",
                token_type="Bearer",  # noqa: S106
            ),
            allow_private_networks=True,
        )


def _patched_httpx_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def patched_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        kwargs.pop("transport", None)
        return original_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("mindroom.matrix_rtc.focus.httpx.AsyncClient", patched_client)


@pytest.mark.asyncio
async def test_request_sfu_grant_rejects_non_object_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-object JSON grant surfaces as ValueError, not AttributeError."""
    _patched_httpx_client(monkeypatch, lambda _request: httpx.Response(200, json=["not", "a", "grant"]))
    with pytest.raises(ValueError, match="non-object grant"):
        await request_sfu_grant(
            "https://rtc.example.org",
            room_id="!room:example.org",
            device_id=DEVICE,
            openid_token=OpenIDToken(
                access_token="opaque",  # noqa: S106
                expires_in=3600,
                matrix_server_name="example.org",
                token_type="Bearer",  # noqa: S106
            ),
        )


@pytest.mark.asyncio
async def test_discovery_tolerates_non_object_well_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-object well-known body yields None instead of AttributeError."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=["not", "a", "well-known"])

    _patched_httpx_client(monkeypatch, handler)
    assert await discover_livekit_service_url("example.org") is None
    assert seen_urls == ["https://example.org/.well-known/matrix/client"]
