"""Tests for local desktop policy and accessibility-first execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.desktop.accessibility import (
    AccessibilityElement,
    AccessibilityError,
    AccessibilityState,
    DesktopApp,
    DesktopRect,
)
from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.command_journal import DesktopCommandJournalError
from mindroom.desktop.media import DesktopMediaError
from mindroom.desktop.playwright_mcp import (
    BrowserImage,
    BrowserProviderResult,
    PlaywrightActionOutcomeUnknownError,
)
from mindroom.desktop.protocol import (
    DESKTOP_APP_ACTIONS,
    DESKTOP_COMMAND_EVENT_TYPE,
    DesktopCommand,
    DesktopResponse,
    EncryptedDesktopMedia,
)
from mindroom.desktop.provider import DesktopEmergencyStopError, DesktopProviderError, ScreenCapture
from mindroom.matrix.olm_to_device import PinnedMatrixDevice
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

if TYPE_CHECKING:
    from pathlib import Path

NOW_SECONDS = 10.0
APP_ID = "com.example.Editor"
CONTROLLER = PinnedMatrixDevice("@cloud:example.org", "CLOUD", "cloud-fingerprint")
WINDOW = DesktopRect(100, 50, 800, 600)
ELEMENT = AccessibilityElement(
    index=0,
    depth=0,
    parent_index=None,
    role="AXButton",
    subrole=None,
    name="Save",
    value=None,
    enabled=True,
    settable=False,
    bounds=DesktopRect(120, 80, 80, 30),
    actions=("AXPress",),
)
STATE = AccessibilityState("state-1", APP_ID, "Editor", WINDOW, (ELEMENT,), False)
SCREENSHOT = ScreenCapture(
    b"\xff\xd8\xffimage",
    "image/jpeg",
    1920,
    1080,
    800,
    600,
    100,
    50,
    800,
    600,
)
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
    stale_state: bool = False
    state_error_after: int | None = None
    state_count: int = 0

    def status(self) -> dict[str, object]:
        """Record status."""
        self.calls.append(("status", None))
        return {
            "screen": {"width": 1920, "height": 1080},
            "accessibility": {"available": True, "backend": "fake"},
        }

    def check_emergency_stop(self) -> None:
        """Model the pointer fail-safe checked before every browser control call."""
        self.calls.append(("check_emergency_stop", None))
        if self.emergency_stop:
            msg = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
            raise DesktopEmergencyStopError(msg)

    def list_apps(self) -> list[DesktopApp]:
        """Return only the configured fake application."""
        self.calls.append(("list_apps", None))
        return [DesktopApp(APP_ID, "Editor", True)]

    def launch_app(self, app_id: str) -> None:
        """Record one exact allowlisted application launch."""
        self.calls.append(("launch_app", app_id))

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Return a fresh state or fail after the configured number of reads."""
        self.calls.append(("get_app_state", app_id))
        if self.state_error_after is not None and self.state_count >= self.state_error_after:
            msg = "App state failed."
            raise AccessibilityError(msg)
        self.state_count += 1
        return replace(STATE, state_id=f"state-{self.state_count}")

    def screenshot(self, *, app_id: str, state_id: str) -> ScreenCapture:
        """Record the exact window crop."""
        self.calls.append(("screenshot", (app_id, state_id)))
        if self.screenshot_error:
            msg = "Screenshot failed."
            raise DesktopProviderError(msg)
        return SCREENSHOT

    def click_element(self, *, app_id: str, state_id: str, element_index: int) -> None:
        """Record one semantic press."""
        self.calls.append(("click_element", (app_id, state_id, element_index)))

    def set_value(self, *, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Record one semantic value change."""
        self.calls.append(("set_value", (app_id, state_id, element_index, value)))

    def scroll_element(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        direction: str,
        pages: int,
    ) -> None:
        """Record one element-scoped scroll."""
        self.calls.append(("scroll_element", (app_id, state_id, element_index, direction, pages)))

    def perform_action(
        self,
        *,
        app_id: str,
        state_id: str,
        element_index: int,
        action_name: str,
    ) -> None:
        """Record one advertised accessibility action."""
        self.calls.append(("perform_action", (app_id, state_id, element_index, action_name)))

    def click(self, *, app_id: str, state_id: str, x: int, y: int, button: str) -> None:
        """Record one normalized fallback click."""
        self.calls.append(("click", (app_id, state_id, x, y, button)))
        if self.emergency_stop:
            msg = "Desktop emergency stop engaged; restart the bridge locally before granting control again."
            raise DesktopEmergencyStopError(msg)
        if self.stale_state:
            msg = "Accessibility state is stale; request get_app_state again before acting."
            raise AccessibilityError(msg)
        if self.click_error:
            msg = "Unexpected click failure."
            raise RuntimeError(msg)

    def type_text(self, *, app_id: str, state_id: str, text: str) -> None:
        """Record fallback text."""
        self.calls.append(("type_text", (app_id, state_id, text)))

    def scroll(
        self,
        *,
        app_id: str,
        state_id: str,
        direction: str,
        pages: int,
        x: int | None,
        y: int | None,
    ) -> None:
        """Record fallback scroll."""
        self.calls.append(("scroll", (app_id, state_id, direction, pages, x, y)))

    def keypress(self, *, app_id: str, state_id: str, keys: list[str]) -> None:
        """Record fallback keypress."""
        self.calls.append(("keypress", (app_id, state_id, keys)))


@dataclass
class FakeBrowserProvider:
    """Record browser actions handled inside the local Matrix bridge."""

    result: BrowserProviderResult = field(
        default_factory=lambda: BrowserProviderResult(
            {"action": "snapshot", "provider": "playwright_mcp_extension", "result": "snapshot", "status": "ok"},
        ),
    )
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    error: Exception | None = None

    async def execute(self, action: str, parameters: dict[str, object]) -> BrowserProviderResult:
        """Record and return the planned result."""
        self.calls.append((action, parameters))
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        """Satisfy the provider lifecycle contract."""


def _command(
    action: str = "screenshot",
    *,
    request_id: str = "request-1",
    sequence: int = 1,
    requester_id: str = "@alice:example.org",
    agent_name: str = "computer",
    parameters: dict[str, object] | None = None,
) -> DesktopCommand:
    if parameters is None:
        parameters = {"app": APP_ID} if action in DESKTOP_APP_ACTIONS else {}
    return DesktopCommand(
        request_id=request_id,
        session_id="session-1",
        sequence=sequence,
        issued_at_ms=9_000,
        expires_at_ms=11_000,
        action=action,
        requester_id=requester_id,
        agent_name=agent_name,
        parameters=parameters,
    )


def _event(command: DesktopCommand) -> AuthenticatedToDeviceEvent:
    return AuthenticatedToDeviceEvent(
        source={"content": command.to_content()},
        sender=CONTROLLER.user_id,
        type=DESKTOP_COMMAND_EVENT_TYPE,
        authenticated_device_id=CONTROLLER.device_id,
    )


def _policy(*, allow_control: bool = False, browser_enabled: bool = False) -> DesktopBridgePolicy:
    return DesktopBridgePolicy(
        controller=CONTROLLER,
        allowed_requester_ids=frozenset({"@alice:example.org"}),
        allowed_agent_names=frozenset({"computer"}),
        allowed_app_ids=frozenset({APP_ID}),
        allow_control=allow_control,
        control_lease_expires_at_ms=20_000 if allow_control else None,
        browser_enabled=browser_enabled,
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
async def test_observe_only_bridge_returns_state_and_window_screenshot(transport: AsyncMock) -> None:
    """Observation returns semantic state and captures only that app window."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await bridge.on_to_device_event(_event(_command()))

    response = _response(transport)
    assert response.ok
    assert response.screenshot == MEDIA
    assert response.result["state"] == STATE.to_result()
    assert response.result["capture"] == WINDOW.to_result()
    assert response.result["image"] == {"width": 800, "height": 600}
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]


@pytest.mark.asyncio
async def test_bridge_rejects_command_from_unpinned_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge wiring drops a valid command unless its Olm sender matches the exact controller pin."""
    provider = FakeProvider()
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.bridge.authenticated_sender_matches", lambda *_args: False)
    monkeypatch.setattr("mindroom.desktop.bridge.send_encrypted_to_device", send)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await bridge.on_to_device_event(_event(_command()))

    assert provider.calls == []
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_apps_and_status_expose_only_coarse_local_authority(transport: AsyncMock) -> None:
    """The agent can discover allowed apps and local mode without a screenshot."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(_event(_command("list_apps")))
    assert _response(transport).result == {"apps": [DesktopApp(APP_ID, "Editor", True).to_result()]}

    await bridge.on_to_device_event(_event(_command("status", request_id="request-2", sequence=2)))
    assert _response(transport).result["bridge"] == {
        "mode": "control",
        "control_available": True,
        "emergency_stop_latched": False,
        "allowed_app_count": 1,
        "browser_enabled": False,
        "control_lease_expires_at_ms": 20_000,
    }


@pytest.mark.asyncio
async def test_launch_app_requires_control_and_returns_fresh_state(transport: AsyncMock) -> None:
    """Launching stays behind the local lease and returns state bound to the resulting app window."""
    provider = FakeProvider()
    observe_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
    )

    await observe_bridge.on_to_device_event(_event(_command("launch_app")))

    assert _response(transport).error == "Desktop control is disabled; this bridge is observe-only."
    assert provider.calls == []
    transport.reset_mock()
    control_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await control_bridge.on_to_device_event(_event(_command("launch_app")))

    response = _response(transport)
    assert response.ok
    assert response.result["state"] == STATE.to_result()
    assert provider.calls == [
        ("launch_app", APP_ID),
        ("get_app_state", APP_ID),
        ("screenshot", (APP_ID, "state-1")),
    ]


@pytest.mark.asyncio
async def test_browser_observation_uses_optional_provider_without_control_lease(transport: AsyncMock) -> None:
    """Tabs and accessibility snapshots remain available in observe-only mode."""
    browser = FakeBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )
    command = _command(
        "browser_observe",
        parameters={"browser_action": "snapshot", "browser_parameters": {}},
    )

    await bridge.on_to_device_event(_event(command))

    response = _response(transport)
    assert response.ok
    assert response.result["provider"] == "playwright_mcp_extension"
    assert browser.calls == [("snapshot", {})]


@pytest.mark.asyncio
async def test_browser_tab_selection_cannot_hide_inside_observe_action(transport: AsyncMock) -> None:
    """Selecting a tab mutates visible browser state and therefore requires a control command and lease."""
    browser = FakeBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "browser_observe",
                parameters={"browser_action": "snapshot", "browser_parameters": {"targetId": "1"}},
            ),
        ),
    )

    assert _response(transport).error == "Desktop browser command used the wrong observe/control classification."
    assert browser.calls == []


@pytest.mark.asyncio
async def test_browser_control_requires_same_local_lease_as_accessibility(transport: AsyncMock) -> None:
    """Installing the extension does not bypass the bridge's local control authority."""
    browser = FakeBrowserProvider()
    parameters = {
        "browser_action": "act",
        "browser_parameters": {"request": {"kind": "click", "ref": "e3"}},
    }
    observe_only = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await observe_only.on_to_device_event(_event(_command("browser_control", parameters=parameters)))

    assert _response(transport).error == "Desktop control is disabled; this bridge is observe-only."
    assert browser.calls == []
    transport.reset_mock()

    controlled = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )
    await controlled.on_to_device_event(_event(_command("browser_control", parameters=parameters)))

    assert _response(transport).ok
    assert browser.calls == [("act", {"request": {"kind": "click", "ref": "e3"}})]


@pytest.mark.asyncio
async def test_browser_control_honors_pointer_emergency_stop(transport: AsyncMock) -> None:
    """The local PyAutoGUI fail-safe latches before Playwright receives a control action."""
    provider = FakeProvider(emergency_stop=True)
    browser = FakeBrowserProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "browser_control",
                parameters={"browser_action": "navigate", "browser_parameters": {"targetUrl": "https://example.com"}},
            ),
        ),
    )

    response = _response(transport)
    assert not response.ok
    assert "emergency stop" in (response.error or "").lower()
    assert provider.calls == [("check_emergency_stop", None)]
    assert browser.calls == []


@pytest.mark.asyncio
async def test_browser_control_failure_requires_fresh_observation(transport: AsyncMock) -> None:
    """A dispatched browser mutation is never presented as safe to retry automatically."""
    browser = FakeBrowserProvider(error=PlaywrightActionOutcomeUnknownError("extension disconnected"))
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(allow_control=True, browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "browser_control",
                parameters={"browser_action": "navigate", "browser_parameters": {"targetUrl": "https://example.com"}},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    warning = str(response.result["warning"])
    assert "outcome is unknown" in warning
    assert "browser(action='tabs' or 'snapshot', target='desktop')" in warning


@pytest.mark.asyncio
async def test_browser_screenshot_is_uploaded_as_encrypted_matrix_media(transport: AsyncMock) -> None:
    """Browser-native screenshots use the same encrypted media path as desktop captures."""
    browser = FakeBrowserProvider(
        BrowserProviderResult(
            {"action": "screenshot", "result": "captured", "status": "ok"},
            BrowserImage(b"\x89PNGbrowser", "image/png"),
        ),
    )
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(browser_enabled=True),
        browser_provider=browser,
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "browser_observe",
                parameters={"browser_action": "screenshot", "browser_parameters": {}},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot == MEDIA
    upload = pytest.importorskip("mindroom.desktop.bridge").upload_encrypted_screenshot
    upload.assert_awaited_once_with(
        bridge.client,
        b"\x89PNGbrowser",
        mime_type="image/png",
        filename="browser-request-1.png",
    )


@pytest.mark.asyncio
async def test_disallowed_app_is_rejected_before_provider_access(transport: AsyncMock) -> None:
    """Payload parameters cannot broaden the exact local app allowlist."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await bridge.on_to_device_event(_event(_command(parameters={"app": "com.example.Secret"})))

    response = _response(transport)
    assert not response.ok
    assert "local allowlist" in (response.error or "")
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issued_at_ms", "expires_at_ms", "expected_error"),
    [
        (8_000, 10_000, "expired before local execution"),
        (40_001, 41_000, "too far in the future"),
    ],
)
async def test_command_time_window_is_enforced(
    transport: AsyncMock,
    issued_at_ms: int,
    expires_at_ms: int,
    expected_error: str,
) -> None:
    """Expired and implausibly future commands fail before any local observation or input."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)
    command = replace(_command(), issued_at_ms=issued_at_ms, expires_at_ms=expires_at_ms)

    await bridge.on_to_device_event(_event(command))

    assert expected_error in (_response(transport).error or "")
    assert provider.calls == []


@pytest.mark.asyncio
async def test_get_state_survives_window_screenshot_failure(transport: AsyncMock) -> None:
    """A useful accessibility tree is returned even when pixels cannot be captured."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await bridge.on_to_device_event(_event(_command("get_app_state")))

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["state"] == STATE.to_result()
    assert "warning" in response.result


@pytest.mark.asyncio
async def test_screenshot_action_still_requires_pixels(transport: AsyncMock) -> None:
    """An explicit screenshot request remains a normal retryable observation failure."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await bridge.on_to_device_event(_event(_command("screenshot")))

    response = _response(transport)
    assert not response.ok
    assert response.error == "Screenshot failed."


@pytest.mark.asyncio
async def test_control_is_denied_without_local_lease(transport: AsyncMock) -> None:
    """Cloud configuration alone cannot enable semantic or fallback control."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)
    command = _command(
        "click_element",
        parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
    )

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
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: wall_clock[0],
        monotonic_clock=lambda: monotonic_clock[0],
    )
    wall_clock[0] = 5.0
    monotonic_clock[0] = 111.0

    await bridge.on_to_device_event(
        _event(
            _command(
                "click",
                parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
            ),
        ),
    )

    assert _response(transport).error == "Local desktop control lease has expired."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_control_lease_expires_across_system_sleep(transport: AsyncMock) -> None:
    """Wall time expiry revokes control even when the macOS monotonic clock paused during sleep."""
    wall_clock = [NOW_SECONDS]
    monotonic_clock = [100.0]
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: wall_clock[0],
        monotonic_clock=lambda: monotonic_clock[0],
    )
    wall_clock[0] = 21.0
    command = replace(
        _command(
            "click",
            parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
        ),
        issued_at_ms=20_000,
        expires_at_ms=22_000,
    )

    await bridge.on_to_device_event(_event(command))

    assert _response(transport).error == "Local desktop control lease has expired."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_semantic_action_returns_fresh_state_and_window_capture(transport: AsyncMock) -> None:
    """A leased semantic action is followed by new indexes and app-scoped visual feedback."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["state"] == STATE.to_result()
    assert provider.calls == [
        ("click_element", (APP_ID, "state-1", 0)),
        ("get_app_state", APP_ID),
        ("screenshot", (APP_ID, "state-1")),
    ]


@pytest.mark.asyncio
async def test_bridge_allows_empty_semantic_value_but_rejects_shortcut_chord(transport: AsyncMock) -> None:
    """Clearing a field is supported while global keyboard shortcuts stay local-policy errors."""
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "set_value",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0, "value": ""},
            ),
        ),
    )

    assert _response(transport).ok
    assert ("set_value", (APP_ID, "state-1", 0, "")) in provider.calls
    transport.reset_mock()

    await bridge.on_to_device_event(
        _event(
            _command(
                "keypress",
                request_id="request-2",
                sequence=2,
                parameters={"app": APP_ID, "state_id": "state-1", "keys": ["command", "tab"]},
            ),
        ),
    )

    response = _response(transport)
    assert not response.ok
    assert "locally safe" in (response.error or "")
    assert all(call[0] != "keypress" for call in provider.calls)


@pytest.mark.asyncio
async def test_stale_state_is_a_safe_rejection_not_unknown_input(transport: AsyncMock) -> None:
    """Local stale-state validation happens before fallback input is attempted."""
    provider = FakeProvider(stale_state=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "click",
                parameters={"app": APP_ID, "state_id": "old", "x": 10, "y": 20, "button": "left"},
            ),
        ),
    )

    response = _response(transport)
    assert not response.ok
    assert "stale" in (response.error or "")


@pytest.mark.asyncio
async def test_completed_action_is_partial_when_follow_up_state_fails(transport: AsyncMock) -> None:
    """A post-action observation failure warns against retrying known-completed input."""
    provider = FakeProvider(state_error_after=0)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True
    assert response.result["follow_up_state"] == "failed"
    assert "do not repeat" in str(response.result["warning"])


@pytest.mark.asyncio
async def test_completed_action_is_partial_when_follow_up_capture_fails(transport: AsyncMock) -> None:
    """A capture failure after fresh state warns against retrying the action."""
    provider = FakeProvider(screenshot_error=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True
    assert response.result["follow_up_screenshot"] == "failed"


@pytest.mark.asyncio
async def test_unexpected_control_failure_reports_unknown_outcome(transport: AsyncMock) -> None:
    """An input exception cannot make a potentially completed action look retryable."""
    provider = FakeProvider(click_error=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "click",
                parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    assert "do not repeat" in str(response.result["warning"])


@pytest.mark.asyncio
async def test_completed_action_is_partial_when_upload_fails(
    transport: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An encrypted-media upload failure cannot make completed input look retryable."""
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_screenshot",
        AsyncMock(side_effect=DesktopMediaError("Upload failed.")),
    )
    provider = FakeProvider()
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )

    await bridge.on_to_device_event(
        _event(
            _command(
                "click_element",
                parameters={"app": APP_ID, "state_id": "state-1", "element_index": 0},
            ),
        ),
    )

    response = _response(transport)
    assert response.ok
    assert response.screenshot is None
    assert response.result["action_completed"] is True


@pytest.mark.asyncio
async def test_requester_agent_replay_and_sequence_are_enforced(transport: AsyncMock) -> None:
    """Provenance and idempotency remain enforced before reading the allowed app."""
    provider = FakeProvider()
    bridge = DesktopBridge(client=object(), provider=provider, policy=_policy(), clock=lambda: NOW_SECONDS)

    await bridge.on_to_device_event(_event(_command(requester_id="@mallory:example.org")))
    assert not _response(transport).ok
    assert provider.calls == []

    await bridge.on_to_device_event(_event(_command(request_id="bad-agent", agent_name="other")))
    assert not _response(transport).ok
    assert provider.calls == []

    first = _command(request_id="request-2", sequence=2)
    await bridge.on_to_device_event(_event(first))
    first_response_content = transport.await_args.kwargs["content"]
    await bridge.on_to_device_event(_event(first))
    assert transport.await_args.kwargs["content"] == first_response_content
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]

    await bridge.on_to_device_event(_event(_command("status", request_id="request-2", sequence=2)))
    assert "reused with different command content" in (_response(transport).error or "")
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]

    await bridge.on_to_device_event(_event(_command(request_id="request-3", sequence=2)))
    assert "sequence" in (_response(transport).error or "")


@pytest.mark.asyncio
async def test_started_control_is_not_repeated_after_bridge_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: AsyncMock,
) -> None:
    """A redelivered command with an interrupted durable record returns an unknown outcome."""
    journal_path = tmp_path / "desktop_bridge" / "command_journal.json"
    provider = FakeProvider()
    command = _command(
        "click",
        parameters={"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"},
    )
    first_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    monkeypatch.setattr(first_bridge, "_execute_safely", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await first_bridge.on_to_device_event(_event(command))

    transport.assert_not_awaited()
    restarted_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await restarted_bridge.on_to_device_event(_event(command))

    response = _response(transport)
    assert response.ok
    assert response.result["action_outcome"] == "unknown"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_completed_response_is_replayed_after_bridge_restart(tmp_path: Path, transport: AsyncMock) -> None:
    """A completed durable record returns its cached response without repeating local work."""
    journal_path = tmp_path / "desktop_bridge" / "command_journal.json"
    provider = FakeProvider()
    command = _command()
    first_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await first_bridge.on_to_device_event(_event(command))
    first_response_content = transport.await_args.kwargs["content"]

    restarted_bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await restarted_bridge.on_to_device_event(_event(command))

    assert transport.await_args.kwargs["content"] == first_response_content
    assert provider.calls == [("get_app_state", APP_ID), ("screenshot", (APP_ID, "state-1"))]


@pytest.mark.asyncio
async def test_bridge_refuses_permissive_command_journal(tmp_path: Path, transport: AsyncMock) -> None:
    """A restored journal cannot expose retained desktop or browser response content."""
    journal_path = tmp_path / "desktop_bridge" / "command_journal.json"
    bridge = DesktopBridge(
        client=object(),
        provider=FakeProvider(),
        policy=_policy(),
        clock=lambda: NOW_SECONDS,
        journal_path=journal_path,
    )
    await bridge.on_to_device_event(_event(_command()))
    transport.assert_awaited_once()
    journal_path.chmod(0o644)

    with pytest.raises(DesktopCommandJournalError, match="group or other users"):
        DesktopBridge(
            client=object(),
            provider=FakeProvider(),
            policy=_policy(),
            clock=lambda: NOW_SECONDS,
            journal_path=journal_path,
        )


@pytest.mark.asyncio
async def test_emergency_stop_latches_control_off_until_local_restart(transport: AsyncMock) -> None:
    """Moving to the fail-safe corner revokes later input in the same process."""
    provider = FakeProvider(emergency_stop=True)
    bridge = DesktopBridge(
        client=object(),
        provider=provider,
        policy=_policy(allow_control=True),
        clock=lambda: NOW_SECONDS,
    )
    parameters = {"app": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"}

    await bridge.on_to_device_event(_event(_command("click", parameters=parameters)))

    assert "emergency stop" in (_response(transport).error or "")
    provider.emergency_stop = False
    await bridge.on_to_device_event(
        _event(_command("click", request_id="request-2", sequence=2, parameters=parameters)),
    )

    assert "latched" in (_response(transport).error or "")
    assert provider.calls == [("click", (APP_ID, "state-1", 10, 20, "left"))]
