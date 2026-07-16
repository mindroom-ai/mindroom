"""Tests for portable accessibility state and stale-state safety."""

# ruff: noqa: D102, N802, N815

from __future__ import annotations

from collections import UserList
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from mindroom.desktop.accessibility import (
    PRIMARY_SCREEN_APP_ID,
    AccessibilityElement,
    AccessibilityError,
    AccessibilityState,
    DesktopRect,
    MacAccessibilityBackend,
    ScreenshotOnlyAccessibilityBackend,
)


class FakeRunningApplication:
    """Small NSRunningApplication-compatible test object."""

    def __init__(self, pid: int = 42) -> None:
        self.pid = pid
        self.activation_hook: Callable[[], None] | None = None

    def bundleIdentifier(self) -> str:
        return "com.example.Editor"

    def localizedName(self) -> str:
        return "Editor"

    def processIdentifier(self) -> int:
        return self.pid

    def isActive(self) -> bool:
        return True

    def activateWithOptions_(self, _options: int) -> bool:
        if self.activation_hook is not None:
            self.activation_hook()
        return True


class FakeWorkspace:
    """Mutable app list used to simulate process replacement."""

    def __init__(self, application: FakeRunningApplication) -> None:
        self.applications = [application]

    def runningApplications(self) -> list[FakeRunningApplication]:
        return self.applications


class FakeMacServices:
    """Small AXUIElement surface for deterministic tree and pinning tests."""

    kAXErrorSuccess = 0
    kAXFocusedWindowAttribute = "focused_window"
    kAXWindowsAttribute = "windows"
    kAXMinimizedAttribute = "minimized"
    kAXPositionAttribute = "position"
    kAXSizeAttribute = "size"
    kAXRoleAttribute = "role"
    kAXTitleAttribute = "title"
    kAXDescriptionAttribute = "description"
    kAXHelpAttribute = "help"
    kAXIdentifierAttribute = "identifier"
    kAXValueAttribute = "value"
    kAXEnabledAttribute = "enabled"
    kAXChildrenAttribute = "children"
    kAXValueCGPointType = "point"
    kAXValueCGSizeType = "size_value"
    kAXPressAction = "AXPress"

    def __init__(self) -> None:
        self.window = "window-1"
        self.attributes: dict[object, dict[str, object]] = {
            "window-1": {
                "role": "AXWindow",
                "title": "Editor",
                "position": SimpleNamespace(x=10, y=20),
                "size": SimpleNamespace(width=800, height=600),
                "children": UserList(["button", "password"]),
            },
            "window-2": {
                "role": "AXWindow",
                "title": "Editor",
                "position": SimpleNamespace(x=10, y=20),
                "size": SimpleNamespace(width=800, height=600),
                "children": UserList(["button", "password"]),
            },
            "button": {
                "role": "AXButton",
                "title": "Save",
                "value": "draft",
                "enabled": True,
            },
            "password": {
                "role": "AXSecureTextField",
                "title": "Password",
                "value": "must-not-leave-machine",
                "enabled": True,
            },
        }
        self.actions = {"button": UserList(["AXPress", "Name:Unsafe\nTarget:0x0"])}

    @staticmethod
    def AXIsProcessTrusted() -> bool:
        return True

    @staticmethod
    def AXUIElementCreateApplication(pid: int) -> tuple[str, int]:
        return "app", pid

    def AXUIElementCopyAttributeValue(
        self,
        reference: object,
        attribute: str,
        _unused: object,
    ) -> tuple[int, object | None]:
        if isinstance(reference, tuple) and reference[0] == "app":
            if attribute == self.kAXFocusedWindowAttribute:
                return 0, self.window
            if attribute == self.kAXWindowsAttribute:
                return 0, UserList([self.window])
        value = self.attributes.get(reference, {}).get(attribute)
        return (0, value) if value is not None else (1, None)

    @staticmethod
    def AXValueGetValue(value: object, _kind: str, _unused: object) -> tuple[bool, object]:
        return True, value

    def AXUIElementCopyActionNames(self, reference: object, _unused: object) -> tuple[int, object]:
        return 0, self.actions.get(reference, UserList())

    def AXUIElementIsAttributeSettable(
        self,
        reference: object,
        attribute: str,
        _unused: object,
    ) -> tuple[int, bool]:
        return 0, reference == "password" and attribute == self.kAXValueAttribute


def _fake_mac_backend() -> tuple[MacAccessibilityBackend, FakeMacServices, FakeWorkspace]:
    application = FakeRunningApplication()
    services = FakeMacServices()
    workspace = FakeWorkspace(application)
    backend = object.__new__(MacAccessibilityBackend)
    backend._allowed_app_ids = frozenset({"com.example.Editor"})
    backend._screen_size = lambda: (1920, 1080)
    backend._appkit = SimpleNamespace(NSApplicationActivateIgnoringOtherApps=1)
    backend._services = services
    backend._workspace = workspace
    backend._states = {}
    return backend, services, workspace


def test_accessibility_state_serializes_bounded_semantic_fields() -> None:
    """The cloud sees stable indexes, hierarchy, actions, and app-window geometry."""
    element = AccessibilityElement(
        index=3,
        depth=2,
        parent_index=1,
        role="AXButton",
        name="Save",
        value=None,
        enabled=True,
        settable=False,
        bounds=DesktopRect(10, 20, 30, 40),
        actions=("AXPress",),
    )
    state = AccessibilityState(
        "state-1",
        "com.example.Editor",
        "Editor",
        DesktopRect(0, 0, 800, 600),
        (element,),
        False,
    )

    assert state.to_result() == {
        "state_id": "state-1",
        "app": {"id": "com.example.Editor", "name": "Editor"},
        "window": {"x": 0, "y": 0, "width": 800, "height": 600},
        "elements": [
            {
                "index": 3,
                "depth": 2,
                "parent_index": 1,
                "role": "AXButton",
                "name": "Save",
                "enabled": True,
                "settable": False,
                "bounds": {"x": 10, "y": 20, "width": 30, "height": 40},
                "actions": ["AXPress"],
            },
        ],
        "truncated": False,
    }


def test_mac_tree_accepts_native_sequences_and_hides_secure_values() -> None:
    """PyObjC arrays are traversed while unsafe actions and secure values stay local."""
    backend, _, _ = _fake_mac_backend()

    state = backend.get_app_state("com.example.Editor")

    button = next(element for element in state.elements if element.name == "Save")
    password = next(element for element in state.elements if element.name == "Password")
    assert button.actions == ("AXPress",)
    assert password.role == "AXSecureTextField"
    assert password.value is None
    assert not password.settable

    with pytest.raises(AccessibilityError, match="Secure text fields"):
        backend.set_value(state.app_id, state.state_id, password.index, "secret")


def test_mac_semantic_action_rejects_disabled_element_before_invocation() -> None:
    """Disabled controls fail safely instead of producing an unknown action outcome."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"]["enabled"] = False
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")

    with pytest.raises(AccessibilityError, match="disabled"):
        backend.click_element(state.app_id, state.state_id, button.index)


def test_mac_semantic_action_rejects_changed_target_value() -> None:
    """Any changed semantic value invalidates the state before an action can run."""
    backend, services, _ = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")
    services.attributes["button"]["value"] = "published"

    with pytest.raises(AccessibilityError, match="state changed"):
        backend.click_element(state.app_id, state.state_id, button.index)


def test_mac_capture_rechecks_state_after_foregrounding_app() -> None:
    """Activation cannot silently change the UI between validation and pixel capture."""
    backend, services, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    workspace.applications[0].activation_hook = lambda: services.attributes["button"].update(value="published")

    with pytest.raises(AccessibilityError, match="state changed"):
        backend.prepare_capture(state.app_id, state.state_id)


def test_mac_element_scroll_rejects_bounds_outside_allowed_window() -> None:
    """Element-scoped pixel scrolling cannot land on a different visible application."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(
        position=SimpleNamespace(x=1500, y=800),
        size=SimpleNamespace(width=100, height=40),
    )
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")

    with pytest.raises(AccessibilityError, match="outside the allowed app window"):
        backend.element_for_action(state.app_id, state.state_id, button.index)


@pytest.mark.parametrize("replacement", ["process", "window", "structure"])
def test_mac_state_pins_exact_process_and_window(replacement: str) -> None:
    """A matching bundle ID cannot redirect an old state to another process or window."""
    backend, services, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    if replacement == "process":
        workspace.applications = [FakeRunningApplication(pid=99)]
    elif replacement == "window":
        services.window = "window-2"
    else:
        services.attributes["button"]["title"] = "Publish"

    with pytest.raises(AccessibilityError, match=r"process changed|window changed|state changed"):
        backend.prepare_capture(state.app_id, state.state_id)
    with pytest.raises(AccessibilityError, match="stale"):
        backend.prepare_capture(state.app_id, state.state_id)


def test_screenshot_only_backend_requires_explicit_primary_screen_allowlist() -> None:
    """Portable pixel mode cannot pretend to provide semantic access to an arbitrary app."""
    backend = ScreenshotOnlyAccessibilityBackend(
        frozenset({PRIMARY_SCREEN_APP_ID, "com.example.Editor"}),
        lambda: (1920, 1080),
    )

    assert [app.to_result() for app in backend.list_apps()] == [
        {"id": "com.example.Editor", "name": "com.example.Editor", "running": False},
        {"id": PRIMARY_SCREEN_APP_ID, "name": "Primary Screen", "running": True},
    ]
    with pytest.raises(AccessibilityError, match="only on macOS"):
        backend.get_app_state("com.example.Editor")
    with pytest.raises(AccessibilityError, match="allowlist"):
        backend.get_app_state("not-allowed")


def test_fallback_state_id_expires_when_replaced() -> None:
    """Each fresh observation invalidates all older pixel coordinates."""
    backend = ScreenshotOnlyAccessibilityBackend(
        frozenset({PRIMARY_SCREEN_APP_ID}),
        lambda: (1920, 1080),
    )
    old = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    current = backend.get_app_state(PRIMARY_SCREEN_APP_ID)

    with pytest.raises(AccessibilityError, match="stale"):
        backend.prepare_fallback(PRIMARY_SCREEN_APP_ID, old.state_id)
    assert backend.prepare_fallback(PRIMARY_SCREEN_APP_ID, current.state_id) == current


def test_fallback_state_expires_when_screen_geometry_changes() -> None:
    """Coordinates cannot be reused after monitor or resolution changes."""
    geometry = [(1920, 1080)]
    backend = ScreenshotOnlyAccessibilityBackend(
        frozenset({PRIMARY_SCREEN_APP_ID}),
        lambda: geometry[0],
    )
    state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    geometry[0] = (1280, 720)

    with pytest.raises(AccessibilityError, match="geometry changed"):
        backend.prepare_fallback(PRIMARY_SCREEN_APP_ID, state.state_id)


def test_screenshot_only_backend_rejects_semantic_actions() -> None:
    """The agent must consciously choose pixel fallback where elements are unavailable."""
    backend = ScreenshotOnlyAccessibilityBackend(
        frozenset({PRIMARY_SCREEN_APP_ID}),
        lambda: (1920, 1080),
    )
    state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)

    with pytest.raises(AccessibilityError, match="unavailable"):
        backend.click_element(PRIMARY_SCREEN_APP_ID, state.state_id, 0)
