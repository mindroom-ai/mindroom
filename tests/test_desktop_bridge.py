"""Tests for local desktop policy and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.media import DesktopMediaError
from mindroom.desktop.protocol import (
    DESKTOP_COMMAND_EVENT_TYPE,
    DesktopCommand,
    DesktopResponse,
    EncryptedDesktopMedia,
)
from mindroom.desktop.provider import DesktopEmergencyStopError, DesktopProviderError, ScreenCapture
from mindroom.matrix.olm_to_device import PinnedMatrixDevice
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

NOW_SECONDS = 10.0
CONTROLLER = PinnedMatrixDevice("@cloud:example.org", "CLOUD", "cloud-fingerprint")
SCREENSHOT = ScreenCapture(b"\xff\xd8\xffimage", "image/jpeg", 1920, 1080, 1600, 900)
MEDIA = EncryptedDesktopMedia(
    url="mxc://example.org/screenshot",
    key="key",
    iv="iv",
    sha256="hash",
    mime_type="image/jpeg",
    size=len(SCREENSHOT.content),
)


@dataclass
class FakeProvider:
    """Record the local operations the bridge actually authorized."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    emergency_stop: bool = False
    screenshot_error: bool = False
    click_error: bool = False

    def status(self) -> dict[str, object]:
        """Record status."""
        self.calls.append(("status", None))
        return {"screen": {"width": 1920, "height": 1080}}

    def screenshot(self) -> ScreenCapture:
        """Record screenshot."""
        self.calls.append(("screenshot", None))
        if self.screenshot_error:
            msg = "Screenshot failed."
            raise DesktopProviderError(msg)
        return SCREENSHOT

    def click(self, *, x: int, y: int, button: str) -> None:
        """Record click."""
        self.calls.append(("click", (x, y, button)))
        if self.emergency_stop:
            msg = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
            raise DesktopEmergencyStopError(msg)
        if self.click_error:
            msg = "Unexpected click failure."
            raise RuntimeError(msg)

    def type_text(self, *, text: str) -> None:
        """Record text."""
        self.calls.append(("type_text", text))

    def scroll(self, *, clicks: int, x: int | None, y: int | None) -> None:
        """Record scroll."""
        self.calls.append(("scroll", (clicks, x, y)))

    def keypress(self, *, keys: list[str]) -> None:
        """Record keypress."""
        self.calls.append(("keypress", keys))


def _command(
    action: str = "screenshot",
    *,
    request_id: str = "request-1",
    sequence: int = 1,
    requester_id: str = "@alice:example.org",
    agent_name: str = "computer",
    parameters: dict[str, object] | None = None,
) -> DesktopCommand:
    return DesktopCommand(
        request_id=request_id,
        session_id="session-1",
        sequence=sequence,
        issued_at_ms=9_000,
        expires_at_ms=11_000,
        action=action,
        requester_id=requester_id,
        agent_name=agent_name,
        parameters=parameters or {},
    )


def _event(command: DesktopCommand) -> AuthenticatedToDeviceEvent:
    return AuthenticatedToDeviceEvent(
        source={"content": command.to_content()},
        sender=CONTROLLER.user_id,
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_device_id=CONTROLLER.device_id,
    )


def _policy(*, allow_control: bool = False) -> DesktopBridgePolicy:
    return DesktopBridgePolicy(
        controller=CONTROLLER,
        allowed_requester_ids=frozenset({"@alice:example.org"}),
        allowed_agent_names=frozenset({"computer"}),
        allow_control=allow_control,
        control_lease_expires_at_ms=20_000 if allow_control else None,
    )


def _response(send: AsyncMock) -> DesktopResponse:
    content = send.await_args.kwargs["content"]
    return DesktopResponse.from_content(content)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Accept the exact controller identity while capturing encrypted responses."""
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: True)
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_screenshot",
        AsyncMock(return_value=MEDIA),
    )
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.bridge.send_encrypted_to_device", send)
    return send


@pytest.mark.asyncio
async def test_observe_only_bridge_returns_encrypted_screenshot(transport: AsyncMock) -> None:
    """Observation works without enabling the local control lease."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)  # type: ignore[arg-type]

    await bridge.on_to_device_event(_event(_command()))

    response = _response(transport)
    assert response.ok
    assert response.screenshot == MEDIA
    assert response.result["screen"] == {"width": 1920, "height": 1080}
    assert response.result["image"] == {"width": 1600, "height": 900}
    assert provider.calls == [("screenshot", None)]


@pytest.mark.asyncio
async def test_status_reports_local_bridge_authority(transport: AsyncMock) -> None:
    """The cloud agent can inspect local mode and emergency-stop state before acting."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(_event(_command("status")))

    response = _response(transport)
    assert response.ok
    assert response.result["bridge"] == {
        "mode": "control",
        "control_available": True,
        "emergency_stop_latched": False,
        "control_lease_expires_at_ms": 20_000,
    }


@pytest.mark.asyncio
async def test_failed_observation_is_reported_as_an_error(transport: AsyncMock) -> None:
    """A screenshot with no preceding action remains a normal retryable failure."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)  # type: ignore[arg-type]

    await bridge.on_to_device_event(_event(_command("screenshot")))

    response = _response(transport)
    assert not response.ok
    assert response.error == "Screenshot failed."
    assert provider.calls == [("screenshot", None)]


@pytest.mark.asyncio
async def test_control_is_denied_without_local_lease(transport: AsyncMock) -> None:
    """Cloud configuration alone cannot enable local keyboard or pointer control."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)  # type: ignore[arg-type]
    command = _command("click", parameters={"x": 10, "y": 20, "button": "left"})

    await bridge.on_to_device_event(_event(command))

    response = _response(transport)
    assert not response.ok
    assert response.error == "Desktop control is disabled; this bridge is observe-only."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_control_lease_uses_monotonic_deadline(transport: AsyncMock) -> None:
    """Rolling the wall clock backward cannot extend locally granted control."""
    wall_clock = [NOW_SECONDS]
    monotonic_clock = [100.0]
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: wall_clock[0],
        monotonic_clock=lambda: monotonic_clock[0],
    )
    wall_clock[0] = 5.0
    monotonic_clock[0] = 111.0

    await bridge.on_to_device_event(
        _event(_command("click", parameters={"x": 10, "y": 20, "button": "left"})),
    )

    response = _response(transport)
    assert not response.ok
    assert response.error == "Local desktop control lease has expired."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_control_lease_performs_one_action_then_captures_state(transport: AsyncMock) -> None:
    """A locally leased action is serialized and followed by visual feedback."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(_command("click", parameters={"x": 10, "y": 20, "button": "left"})),
    )

    assert _response(transport).ok
    assert provider.calls == [("click", (10, 20, "left")), ("screenshot", None)]


@pytest.mark.asyncio
async def test_completed_action_is_not_reported_failed_when_follow_up_capture_fails(transport: AsyncMock) -> None:
    """A capture failure after input warns against retrying the action."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(_command("click", parameters={"x": 10, "y": 20, "button": "left"})),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True
    assert "do not repeat" in str(response.result["warning"])
    assert provider.calls == [("click", (10, 20, "left")), ("screenshot", None)]


@pytest.mark.asyncio
async def test_unexpected_control_failure_reports_unknown_outcome(transport: AsyncMock) -> None:
    """An input exception cannot make a potentially completed action look retryable."""
    provider = FakeProvider(click_error=True)
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(_command("click", parameters={"x": 10, "y": 20, "button": "left"})),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    assert "do not repeat" in str(response.result["warning"])
    assert provider.calls == [("click", (10, 20, "left"))]


@pytest.mark.asyncio
async def test_completed_action_is_not_reported_failed_when_upload_fails(
    transport: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An encrypted-media upload failure cannot make a completed click look retryable."""
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_screenshot",
        AsyncMock(side_effect=DesktopMediaError("Upload failed.")),
    )
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(_command("click", parameters={"x": 10, "y": 20, "button": "left"})),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True
    assert "do not repeat" in str(response.result["warning"])


@pytest.mark.asyncio
async def test_requester_and_agent_must_match_exact_local_allowlists(transport: AsyncMock) -> None:
    """Wildcard-like or payload-only provenance cannot broaden local authority."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)  # type: ignore[arg-type]

    await bridge.on_to_device_event(_event(_command(requester_id="@mallory:example.org")))

    assert not _response(transport).ok
    assert provider.calls == []


@pytest.mark.asyncio
async def test_duplicate_request_is_idempotent_and_sequence_reuse_is_rejected(transport: AsyncMock) -> None:
    """Delivery retries cannot repeat input, and new IDs cannot reuse a sequence."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)  # type: ignore[arg-type]
    first = _command()

    await bridge.on_to_device_event(_event(first))
    await bridge.on_to_device_event(_event(first))

    assert provider.calls == [("screenshot", None)]
    assert transport.await_count == 2

    await bridge.on_to_device_event(_event(_command(request_id="request-2", sequence=first.sequence)))

    assert not _response(transport).ok
    assert "sequence" in (_response(transport).error or "")
    assert provider.calls == [("screenshot", None)]


@pytest.mark.asyncio
async def test_emergency_stop_latches_control_off_until_local_restart(transport: AsyncMock) -> None:
    """Moving to the fail-safe corner revokes later input in the same process."""
    provider = FakeProvider(emergency_stop=True)
    bridge = DesktopBridge(
        client=object(),  # type: ignore[arg-type]
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(_command("click", parameters={"x": 10, "y": 20, "button": "left"})),
    )

    assert not _response(transport).ok
    assert "emergency stop" in (_response(transport).error or "")
    provider.emergency_stop = False
    await bridge.on_to_device_event(
        _event(
            _command(
                "click",
                request_id="request-2",
                sequence=2,
                parameters={"x": 30, "y": 40, "button": "left"},
            ),
        ),
    )

    assert not _response(transport).ok
    assert "latched" in (_response(transport).error or "")
    assert provider.calls == [("click", (10, 20, "left"))]
