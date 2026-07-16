"""Tests for cloud-side desktop response correlation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from mindroom.desktop.client import DesktopRequestError, DesktopResponseRouter
from mindroom.desktop.protocol import DESKTOP_RESPONSE_EVENT_TYPE, DesktopCommand, DesktopResponse
from mindroom.matrix.olm_to_device import PinnedMatrixDevice
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

TARGET = PinnedMatrixDevice("@desktop:example.org", "DESKTOP", "fingerprint")


class FakeClient:
    """Keep the response callback registered by the router."""

    def __init__(self) -> None:
        self.callback: object | None = None

    def add_to_device_callback(self, callback: object, _event_type: object) -> None:
        """Record callback."""
        self.callback = callback


def _command(*, request_id: str = "request-1", action: str = "status") -> DesktopCommand:
    return DesktopCommand(
        request_id=request_id,
        session_id="session-1",
        sequence=1,
        issued_at_ms=1_000,
        expires_at_ms=2_000,
        action=action,
        requester_id="@alice:example.org",
        agent_name="computer",
    )


def _event(response: DesktopResponse) -> AuthenticatedToDeviceEvent:
    return AuthenticatedToDeviceEvent(
        source={"content": response.to_content()},
        sender=TARGET.user_id,
        type=DESKTOP_RESPONSE_EVENT_TYPE,
        authenticated_device_id=TARGET.device_id,
    )


@pytest.mark.asyncio
async def test_request_waits_for_exact_correlated_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only matching request and session IDs from the pinned device resolve a waiter."""
    client = FakeClient()
    router = DesktopResponseRouter(client)  # type: ignore[arg-type]
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", send)
    monkeypatch.setattr("mindroom.desktop.client.authenticated_sender_matches", lambda *_args: True)
    command = _command()

    pending = asyncio.create_task(router.request(TARGET, command, timeout_seconds=1))
    await asyncio.sleep(0)
    router.on_to_device_event(_event(DesktopResponse("request-1", "wrong-session", True)))
    assert not pending.done()

    expected = DesktopResponse("request-1", "session-1", True, result={"online": True})
    router.on_to_device_event(_event(expected))

    assert await pending == expected
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An offline desktop cannot hold an agent tool call open indefinitely."""
    client = FakeClient()
    router = DesktopResponseRouter(client)  # type: ignore[arg-type]
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(DesktopRequestError, match="did not answer"):
        await router.request(TARGET, _command(), timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_control_timeout_reports_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out control action cannot be presented as safe to retry."""
    client = FakeClient()
    router = DesktopResponseRouter(client)  # type: ignore[arg-type]
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(DesktopRequestError, match=r"outcome is unknown.*do not repeat"):
        await router.request(TARGET, _command(action="click"), timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_only_one_request_per_target_can_be_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parallel tool calls cannot reorder or preplan multiple desktop actions."""
    client = FakeClient()
    router = DesktopResponseRouter(client)  # type: ignore[arg-type]
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", send)

    first = asyncio.create_task(router.request(TARGET, _command(), timeout_seconds=1))
    await asyncio.sleep(0)
    with pytest.raises(DesktopRequestError, match="already in progress"):
        await router.request(TARGET, _command(request_id="request-2"), timeout_seconds=1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    send.assert_awaited_once()
