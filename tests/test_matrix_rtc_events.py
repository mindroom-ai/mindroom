"""Wire-format tests for MatrixRTC membership and frame-key events."""

from __future__ import annotations

import httpx
import pytest

from mindroom.matrix_rtc.events import (
    CALL_MEMBER_EVENT_TYPE,
    DEFAULT_MEMBERSHIP_EXPIRES_MS,
    build_key_to_device_content,
    build_membership_content,
    livekit_service_url_from_foci,
    membership_state_key,
    parse_key_to_device_content,
    parse_membership_event,
)
from mindroom.matrix_rtc.focus import OpenIDToken, request_sfu_grant

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
    assert member.membership_id == f"{USER}:{DEVICE}"
    assert member.created_ts == 1_000
    assert not member.is_expired(1_000 + DEFAULT_MEMBERSHIP_EXPIRES_MS - 1)
    assert member.is_expired(1_000 + DEFAULT_MEMBERSHIP_EXPIRES_MS)
    assert livekit_service_url_from_foci([member]) == SERVICE_URL


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


def test_parse_membership_defaults_membership_id_and_expiry() -> None:
    """Parse membership defaults membership id and expiry."""
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
    assert member.membership_id == f"{USER}:{DEVICE}"
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
    received = parse_key_to_device_content(USER, content, room_id="!room:example.org")
    assert received is not None
    assert received.member_id == f"{USER}:{DEVICE}"
    assert received.key_index == 3
    assert received.sent_ts == 777
    # A key for a different room must not leak into this session.
    assert parse_key_to_device_content(USER, content, room_id="!other:example.org") is None


def test_parse_key_content_requires_key_and_claimed_device() -> None:
    """Parse key content requires key and claimed device."""
    room_id = "!room:example.org"
    assert parse_key_to_device_content(USER, {"room_id": room_id}, room_id=room_id) is None
    assert (
        parse_key_to_device_content(
            USER,
            {"room_id": room_id, "keys": {"index": 0, "key": "abc"}, "member": {}},
            room_id=room_id,
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
        return original_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("mindroom.matrix_rtc.focus.httpx.AsyncClient", patched_client)
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
    assert seen["url"] == "https://rtc.example.org/sfu/get"
    assert b'"room": "!room:example.org"' in seen["body"] or b'"room":"!room:example.org"' in seen["body"]
