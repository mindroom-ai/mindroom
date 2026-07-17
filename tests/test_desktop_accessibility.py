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
    AccessibilityActionOutcomeUnknownError,
    AccessibilityElement,
    AccessibilityError,
    AccessibilityState,
    DesktopRect,
    MacAccessibilityBackend,
    ScreenshotOnlyAccessibilityBackend,
)


class FakeRunningApplication:
    """Small NSRunningApplication-compatible test object."""

    def __init__(self, pid: int = 42, *, active: bool = True) -> None:
        self.pid = pid
        self.active = active
        self.activation_hook: Callable[[], None] | None = None
        self.activation_options: list[int] = []

    def bundleIdentifier(self) -> str:
        return "com.example.Editor"

    def localizedName(self) -> str:
        return "Editor"

    def processIdentifier(self) -> int:
        return self.pid

    def isActive(self) -> bool:
        return self.active

    def activateWithOptions_(self, options: int) -> bool:
        self.activation_options.append(options)
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
    kAXSubroleAttribute = "subrole"
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
        self.collection_count = 0
        self.collection_hook: Callable[[int], None] | None = None
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
                "role": "AXTextField",
                "subrole": "AXSecureTextField",
                "title": "Password",
                "value": "must-not-leave-machine",
                "enabled": True,
                "settable": True,
            },
        }
        self.actions = {"button": UserList(["AXPress", "Name:Unsafe\nTarget:0x0"])}
        self.apply_value_updates = True
        self.performed_actions: list[tuple[object, str]] = []

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
                self.collection_count += 1
                if self.collection_hook is not None:
                    self.collection_hook(self.collection_count)
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
        settable = self.attributes.get(reference, {}).get("settable") is True
        return 0, settable and attribute == self.kAXValueAttribute

    def AXUIElementPerformAction(self, reference: object, action: str) -> int:
        self.performed_actions.append((reference, action))
        return 0

    def AXUIElementSetAttributeValue(self, reference: object, attribute: str, value: object) -> int:
        if self.apply_value_updates:
            self.attributes[reference][attribute] = value
        return 0


def _fake_mac_backend() -> tuple[MacAccessibilityBackend, FakeMacServices, FakeWorkspace]:
    application = FakeRunningApplication()
    services = FakeMacServices()
    workspace = FakeWorkspace(application)
    backend = object.__new__(MacAccessibilityBackend)
    backend._allowed_app_ids = frozenset({"com.example.Editor"})
    backend._screen_size = lambda: (1920, 1080)
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
        subrole=None,
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
    assert password.role == "AXTextField"
    assert password.subrole == "AXSecureTextField"
    assert password.value is None
    assert not password.settable

    with pytest.raises(AccessibilityError, match="Secure text fields"):
        backend.set_value(state.app_id, state.state_id, password.index, "secret")


def test_mac_tree_skips_empty_table_wrappers_but_keeps_descendants() -> None:
    """Presentation-only Finder-style rows and cells cannot consume the bounded tree."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["window-1"]["children"] = UserList(["row"])
    services.attributes["row"] = {"role": "AXRow", "children": UserList(["cell"])}
    services.attributes["cell"] = {"role": "AXCell", "children": UserList(["button"])}
    services.actions["row"] = UserList(["AXShowAlternateUI", "AXShowDefaultUI"])

    state = backend.get_app_state("com.example.Editor")

    assert [element.role for element in state.elements] == ["AXWindow", "AXButton"]
    assert state.elements[1].name == "Save"
    assert state.elements[1].parent_index == 0


def test_mac_tree_uses_visible_rows_for_outline() -> None:
    """Off-screen Finder rows cannot crowd visible file controls out of bounded state."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["window-1"]["children"] = UserList(["outline"])
    services.attributes["outline"] = {
        "role": "AXOutline",
        "children": UserList(["offscreen-row"]),
        "AXVisibleRows": UserList(["visible-row"]),
    }
    services.attributes["offscreen-row"] = {"role": "AXRow", "title": "private.txt"}
    services.attributes["visible-row"] = {"role": "AXRow", "title": "test-note.txt"}

    state = backend.get_app_state("com.example.Editor")

    assert [element.name for element in state.elements if element.role == "AXRow"] == ["test-note.txt"]


def test_mac_truncated_state_skips_costly_stabilization() -> None:
    """A bounded partial tree remains usable without repeatedly walking a very large app."""
    backend, services, _ = _fake_mac_backend()
    children = UserList([f"button-{index}" for index in range(128)])
    services.attributes["window-1"]["children"] = children
    for child in children:
        services.attributes[child] = {"role": "AXButton", "title": child}

    state = backend.get_app_state("com.example.Editor")

    assert state.truncated
    assert len(state.elements) == 128
    assert services.collection_count == 1
    target = next(element for element in state.elements if element.name == "button-0")
    services.attributes["button-0"]["title"] = "changed"
    with pytest.raises(AccessibilityError, match="target element changed"):
        backend.click_element(state.app_id, state.state_id, target.index)


def test_mac_state_ignores_non_actionable_image_animation() -> None:
    """Decorative icon jitter cannot prevent an otherwise stable application state."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["window-1"]["children"] = UserList(["image", "button"])
    services.attributes["image"] = {
        "role": "AXImage",
        "title": "trash",
        "position": SimpleNamespace(x=10, y=10),
        "size": SimpleNamespace(width=16, height=16),
    }

    def move_decorative_image(collection_count: int) -> None:
        services.attributes["image"]["position"] = SimpleNamespace(x=10 + collection_count, y=10)

    services.collection_hook = move_decorative_image

    state = backend.get_app_state("com.example.Editor")

    assert next(element for element in state.elements if element.role == "AXImage").name == "trash"
    assert services.collection_count == 4


def test_mac_semantic_action_rejects_disabled_element_before_invocation() -> None:
    """Disabled controls fail safely instead of producing an unknown action outcome."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"]["enabled"] = False
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")

    with pytest.raises(AccessibilityError, match="disabled"):
        backend.click_element(state.app_id, state.state_id, button.index)


def test_mac_state_waits_for_stable_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-time asynchronous UI update settles before a state ID is exposed."""
    backend, services, _ = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)

    def update_after_initial_matching_observation(collection_count: int) -> None:
        if collection_count == 3:
            services.attributes["button"]["title"] = "Publish"

    services.collection_hook = update_after_initial_matching_observation

    state = backend.get_app_state("com.example.Editor")

    assert next(element for element in state.elements if element.role == "AXButton").name == "Publish"
    assert services.collection_count == 6


def test_mac_semantic_action_rejects_changed_target_value() -> None:
    """Any changed semantic value invalidates the state before an action can run."""
    backend, services, _ = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")
    services.attributes["button"]["value"] = "published"

    with pytest.raises(AccessibilityError, match="target element changed"):
        backend.click_element(state.app_id, state.state_id, button.index)


def test_mac_semantic_action_allows_unrelated_dynamic_content() -> None:
    """A stable target remains actionable while an unrelated status element updates."""
    backend, services, _ = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")
    services.attributes["window-1"]["title"] = "Editor — synced"

    backend.click_element(state.app_id, state.state_id, button.index)

    assert services.performed_actions == [("button", "AXPress")]


def test_mac_set_value_focuses_text_field_and_verifies_result() -> None:
    """Writable web-style text fields are focused and must expose the requested value."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(role="AXTextField", settable=True)
    state = backend.get_app_state("com.example.Editor")
    field = next(element for element in state.elements if element.name == "Save")

    backend.set_value(state.app_id, state.state_id, field.index, "published")

    assert services.performed_actions == [("button", "AXPress")]
    assert services.attributes["button"]["value"] == "published"


def test_mac_set_value_rejects_unconfirmed_os_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-op AX success becomes an unknown outcome instead of a false completion."""
    backend, services, _ = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    services.attributes["button"].update(role="AXTextField", settable=True)
    services.apply_value_updates = False
    state = backend.get_app_state("com.example.Editor")
    field = next(element for element in state.elements if element.name == "Save")

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="did not expose"):
        backend.set_value(state.app_id, state.state_id, field.index, "published")


def test_mac_capture_allows_dynamic_content_after_foregrounding_app() -> None:
    """An app window can be captured when activation updates unrelated dynamic content."""
    backend, services, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    workspace.applications[0].activation_hook = lambda: services.attributes["button"].update(value="published")

    captured = backend.prepare_capture(state.app_id, state.state_id)

    assert captured.state.window == state.window
    assert captured.process_id == 42
    assert workspace.applications[0].activation_options == [0]


def test_mac_fallback_rejects_dynamic_content_after_foregrounding_app() -> None:
    """Pixel input still requires exact visual state after app activation."""
    backend, services, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    workspace.applications[0].activation_hook = lambda: services.attributes["button"].update(value="published")

    with pytest.raises(AccessibilityError, match="state changed"):
        backend.prepare_fallback(state.app_id, state.state_id)


def test_mac_activation_must_reach_foreground_before_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OS-accepted but incomplete activation fails before any app input."""
    backend, services, workspace = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("mindroom.desktop.accessibility._request_application_activation", lambda _app_id: None)
    workspace.applications[0].active = False
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")

    with pytest.raises(AccessibilityError, match="did not become active"):
        backend.click_element(state.app_id, state.state_id, button.index)

    assert services.performed_actions == []


def test_mac_activation_uses_bounded_apple_event_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local bridge can foreground an allowed app when direct CLI activation is ignored."""
    backend, services, workspace = _fake_mac_backend()
    application = workspace.applications[0]
    application.active = False
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    calls: list[str] = []

    def activate_app(app_id: str) -> None:
        calls.append(app_id)
        application.active = True

    monkeypatch.setattr("mindroom.desktop.accessibility._request_application_activation", activate_app)
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")

    backend.click_element(state.app_id, state.state_id, button.index)

    assert calls == ["com.example.Editor"]
    assert services.performed_actions == [("button", "AXPress")]


def test_mac_launches_only_the_exact_allowlisted_bundle_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped allowlisted app can be launched without exposing arbitrary application names."""
    backend, _, workspace = _fake_mac_backend()
    workspace.applications = []
    launched = FakeRunningApplication()
    calls: list[str] = []

    def launch(app_id: str) -> None:
        calls.append(app_id)
        workspace.applications.append(launched)

    monkeypatch.setattr("mindroom.desktop.accessibility._request_application_activation", launch)

    backend.launch_app("com.example.Editor")

    assert calls == ["com.example.Editor"]
    assert launched.activation_options == [0]


def test_mac_launch_timeout_has_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted launch request that never appears cannot be presented as safe to repeat."""
    backend, _, workspace = _fake_mac_backend()
    workspace.applications = []
    monkeypatch.setattr("mindroom.desktop.accessibility._request_application_activation", lambda _app_id: None)
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome is unknown"):
        backend.launch_app("com.example.Editor")


@pytest.mark.parametrize("already_running", [False, True])
def test_mac_launch_activation_timeout_has_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
    already_running: bool,
) -> None:
    """A failed foreground wait stays unknown after either launch or direct activation was requested."""
    backend, _, workspace = _fake_mac_backend()
    application = FakeRunningApplication(active=False)
    workspace.applications = [application] if already_running else []

    def request_activation(_app_id: str) -> None:
        if not already_running:
            workspace.applications.append(application)

    monkeypatch.setattr("mindroom.desktop.accessibility._request_application_activation", request_activation)
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome is unknown"):
        backend.launch_app("com.example.Editor")


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


@pytest.mark.parametrize("replacement", ["process", "window"])
def test_mac_state_pins_exact_process_and_window(replacement: str) -> None:
    """A matching bundle ID cannot redirect an old state to another process or window."""
    backend, services, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    if replacement == "process":
        workspace.applications = [FakeRunningApplication(pid=99)]
    elif replacement == "window":
        services.window = "window-2"
    with pytest.raises(AccessibilityError, match=r"process changed|window changed"):
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
    with pytest.raises(AccessibilityError, match="only on macOS"):
        backend.launch_app("com.example.Editor")
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
