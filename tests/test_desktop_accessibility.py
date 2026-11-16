"""Tests for portable accessibility state and stale-state safety."""

# ruff: noqa: D102, N802, N815

from __future__ import annotations

import sys
from collections import OrderedDict, UserList
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from mindroom.desktop.accessibility import (
    PRIMARY_SCREEN_APP_ID,
    AccessibilityActionOutcomeUnknownError,
    AccessibilityCapture,
    AccessibilityElement,
    AccessibilityError,
    AccessibilityState,
    DesktopRect,
    MacAccessibilityBackend,
    ScreenshotOnlyAccessibilityBackend,
)


class FakeRunningApplication:
    """Small NSRunningApplication-compatible test object."""

    def __init__(
        self,
        pid: int = 42,
        *,
        active: bool = True,
        bundle_id: str = "com.example.Editor",
        name: str = "Editor",
    ) -> None:
        self.pid = pid
        self.active = active
        self.bundle_id = bundle_id
        self.name = name
        self.activation_hook: Callable[[], None] | None = None
        self.activation_options: list[int] = []

    def bundleIdentifier(self) -> str:
        return self.bundle_id

    def localizedName(self) -> str:
        return self.name

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
        self.frontmost: FakeRunningApplication | None = application

    def runningApplications(self) -> list[FakeRunningApplication]:
        return self.applications

    def frontmostApplication(self) -> FakeRunningApplication | None:
        return self.frontmost


class FakeMacServices:
    """Small AXUIElement surface for deterministic tree and pinning tests."""

    kAXErrorSuccess = 0
    kAXFocusedWindowAttribute = "focused_window"
    kAXFocusedAttribute = "focused"
    kAXFocusedUIElementAttribute = "focused_element"
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
        self.focused_element = None
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
            if attribute == self.kAXFocusedUIElementAttribute:
                return 0, self.focused_element
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
        return 0, settable and attribute in {self.kAXValueAttribute, self.kAXFocusedAttribute}

    def AXUIElementPerformAction(self, reference: object, action: str) -> int:
        self.performed_actions.append((reference, action))
        return 0

    def AXUIElementSetAttributeValue(self, reference: object, attribute: str, value: object) -> int:
        if self.apply_value_updates:
            self.attributes[reference][attribute] = value
            if attribute == self.kAXFocusedAttribute and value is True:
                self.focused_element = reference
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
    backend._states = OrderedDict()
    backend._before_input = lambda: None
    backend._collect_deadline = None
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

    result = state.to_result()
    element_ref = result["elements"][0].pop("ref")
    assert isinstance(element_ref, str)
    assert element_ref
    assert element_ref == state.to_result()["elements"][0]["ref"]
    assert element_ref != replace(state, state_id="state-2").to_result()["elements"][0]["ref"]
    assert result == {
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
        "stability": "stable",
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
    assert state.to_result()["stability"] == "truncated"
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


def test_mac_launch_rejects_invalid_bundle_id_before_request() -> None:
    """Invalid allowlist entries fail definitively before any launch side effect."""
    backend, _, workspace = _fake_mac_backend()
    backend._allowed_app_ids = frozenset({"invalid_app"})
    workspace.applications = []

    with pytest.raises(AccessibilityError, match="invalid bundle identifier") as exc_info:
        backend.launch_app("invalid_app")

    assert type(exc_info.value) is AccessibilityError


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


@pytest.mark.parametrize("operation", ["click", "capture", "fallback"])
def test_mac_retains_interleaved_observations(operation: str) -> None:
    """A second read cannot revoke an unchanged first observation."""
    backend, services, _ = _fake_mac_backend()
    first = backend.get_app_state("com.example.Editor")
    backend.get_app_state(first.app_id)

    if operation == "click":
        backend.click_element(first.app_id, first.state_id, 1)
        assert services.performed_actions == [("button", "AXPress")]
    elif operation == "capture":
        assert backend.prepare_capture(first.app_id, first.state_id).process_id == 42
    else:
        fallback = backend.prepare_fallback(first.app_id, first.state_id)
        assert (fallback.state.state_id, fallback.process_id) == (first.state_id, 42)


@pytest.mark.parametrize("operation", ["click", "capture", "fallback"])
def test_mac_retained_observation_expires(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Every action path rejects observations after the monotonic lifetime."""
    backend, services, _ = _fake_mac_backend()
    clock = [10.0]
    monkeypatch.setattr("mindroom.desktop.accessibility.time.monotonic", lambda: clock[0])
    state = backend.get_app_state("com.example.Editor")
    clock[0] = 130.0

    if operation == "click":
        with pytest.raises(AccessibilityError, match="stale"):
            backend.click_element(state.app_id, state.state_id, 1)
    elif operation == "capture":
        with pytest.raises(AccessibilityError, match="stale"):
            backend.prepare_capture(state.app_id, state.state_id)
    else:
        with pytest.raises(AccessibilityError, match="stale"):
            backend.prepare_fallback(state.app_id, state.state_id)
    assert services.performed_actions == []


def test_mac_retained_observations_evict_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache pressure revokes the oldest snapshot while preserving newer ones."""
    backend, services, _ = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    first = backend.get_app_state("com.example.Editor")
    second = backend.get_app_state(first.app_id)
    for _ in range(127):
        backend.get_app_state(first.app_id)

    with pytest.raises(AccessibilityError, match="stale"):
        backend.click_element(first.app_id, first.state_id, 1)
    backend.click_element(second.app_id, second.state_id, 1)
    assert services.performed_actions == [("button", "AXPress")]


@pytest.mark.parametrize("role", ["AXStaticText", "AXProgressIndicator"])
def test_mac_passive_updates_allow_observation_and_fallback(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """Continuously changing passive text must not starve observations or pixel input."""
    backend, services, _ = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    services.attributes["window-1"]["children"] = UserList(["button", "status"])
    services.attributes["status"] = {"role": role}

    def update_status(count: int) -> None:
        services.attributes["status"].update(title=f"Progress {count}", value=count)

    services.collection_hook = update_status
    state = backend.get_app_state("com.example.Editor")

    assert state.to_result()["stability"] == "stable"
    assert backend.prepare_fallback(state.app_id, state.state_id).state.state_id == state.state_id


def test_mac_unstable_observation_rejects_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changing actionable label remains readable but cannot authorize pixel input."""
    backend, services, _ = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    services.collection_hook = lambda count: services.attributes["button"].update(title=f"Save {count}")

    state = backend.get_app_state("com.example.Editor")

    assert state.to_result()["stability"] == "unstable"
    services.collection_hook = None
    with pytest.raises(AccessibilityError, match="stable"):
        backend.prepare_fallback(state.app_id, state.state_id)


@pytest.mark.parametrize("during_activation", [False, True])
def test_mac_semantic_action_rejects_replaced_target(during_activation: bool) -> None:
    """Identical visible fields cannot authorize a different OS element."""
    backend, services, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")

    def replace_target() -> None:
        services.attributes["replacement"] = dict(services.attributes["button"])
        services.actions["replacement"] = services.actions["button"]
        services.attributes["window-1"]["children"] = UserList(["replacement", "password"])

    if during_activation:
        workspace.applications[0].activation_hook = replace_target
    else:
        replace_target()
    with pytest.raises(AccessibilityError, match="target element changed"):
        backend.click_element(state.app_id, state.state_id, 1)
    assert services.performed_actions == []


@pytest.mark.parametrize("operation", ["click", "value", "element"])
@pytest.mark.parametrize("during_activation", [False, True])
def test_mac_semantic_target_survives_unrelated_insertion(operation: str, during_activation: bool) -> None:
    """Index remapping must still select the original OS target on both activation checks."""
    backend, services, workspace = _fake_mac_backend()
    services.attributes["button"].update(settable=True)
    state = backend.get_app_state("com.example.Editor")

    def insert_status() -> None:
        services.attributes["status"] = {"role": "AXStaticText", "title": "Synced"}
        services.attributes["window-1"]["children"] = UserList(["status", "button", "password"])

    if during_activation:
        workspace.applications[0].activation_hook = insert_status
    else:
        insert_status()
    if operation == "click":
        backend.click_element(state.app_id, state.state_id, 1)
        assert services.performed_actions == [("button", "AXPress")]
    elif operation == "value":
        backend.set_value(state.app_id, state.state_id, 1, "published")
        assert services.attributes["button"]["value"] == "published"
    else:
        element, process_id = backend.element_for_action(state.app_id, state.state_id, 1)
        assert (element.name, process_id) == ("Save", 42)


@pytest.mark.parametrize("change", ["title", "value", "geometry", "parent"])
@pytest.mark.parametrize("during_activation", [False, True])
def test_mac_semantic_target_rejects_changed_identity_fields(change: str, during_activation: bool) -> None:
    """Clipped text and omitted wrappers must not hide a change to the exact target."""
    backend, services, workspace = _fake_mac_backend()
    services.attributes["button"].update(title="x" * 160 + "old", value="y" * 160 + "old")
    services.attributes["window-1"]["children"] = UserList(["wrapper"])
    services.attributes["wrapper"] = {"role": "AXGroup", "children": UserList(["button"])}
    state = backend.get_app_state("com.example.Editor")

    def change_target() -> None:
        if change == "title":
            services.attributes["button"]["title"] = "x" * 160 + "new"
        elif change == "value":
            services.attributes["button"]["value"] = "y" * 160 + "new"
        elif change == "geometry":
            services.attributes["window-1"]["position"] = SimpleNamespace(x=20, y=30)
        else:
            services.attributes["replacement-wrapper"] = dict(services.attributes["wrapper"])
            services.attributes["window-1"]["children"] = UserList(["replacement-wrapper"])

    if during_activation:
        workspace.applications[0].activation_hook = change_target
    else:
        change_target()
    with pytest.raises(AccessibilityError, match="target element changed"):
        backend.click_element(state.app_id, state.state_id, 1)
    assert services.performed_actions == []


def test_mac_launch_invalidates_all_retained_app_observations() -> None:
    """Launching or foregrounding an app revokes every snapshot for that app."""
    backend, services, _ = _fake_mac_backend()
    first = backend.get_app_state("com.example.Editor")
    second = backend.get_app_state(first.app_id)

    backend.launch_app(first.app_id)

    for state in (first, second):
        with pytest.raises(AccessibilityError, match="stale"):
            backend.click_element(state.app_id, state.state_id, 1)
    assert services.performed_actions == []


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
    assert backend.prepare_fallback(PRIMARY_SCREEN_APP_ID, current.state_id) == AccessibilityCapture(current, None)


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


def test_element_typing_guard_tracks_exact_focus_after_each_chunk() -> None:
    """Typing can continue after its own value edit, but never after focus moves."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(role="AXTextField", settable=True)
    state = backend.get_app_state("com.example.Editor")
    guard = backend.prepare_typing(state.app_id, state.state_id, 1)
    assert services.focused_element == "button"
    services.attributes["button"]["value"] = "typed chunk"
    guard()
    services.focused_element = "password"
    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="focus"):
        guard()


def test_element_typing_rejects_secure_target_before_focus() -> None:
    """Secure field input is denied before focus or keyboard emission."""
    backend, services, _ = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    with pytest.raises(AccessibilityError, match="Secure"):
        backend.prepare_typing(state.app_id, state.state_id, 2)
    assert services.focused_element is None


def test_element_typing_revalidates_target_after_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """An AX focus operation cannot replace the originally selected field."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(role="AXTextField", settable=True)
    state = backend.get_app_state("com.example.Editor")
    original = services.AXUIElementSetAttributeValue

    def focus_replaces_target(reference: object, attribute: str, value: object) -> int:
        result = original(reference, attribute, value)
        services.attributes["replacement"] = dict(services.attributes["button"])
        services.attributes["window-1"]["children"] = ["replacement", "password"]
        return result

    monkeypatch.setattr(services, "AXUIElementSetAttributeValue", focus_replaces_target)
    with pytest.raises(AccessibilityActionOutcomeUnknownError):
        backend.prepare_typing(state.app_id, state.state_id, 1)


@pytest.mark.parametrize("during_activation", [False, True])
@pytest.mark.parametrize("change", ["identifier", "title", "value", "reference", "ancestor", "actions"])
def test_fallback_rejects_hidden_actionable_identity_changes(change: str, during_activation: bool) -> None:
    """Coordinate/key fallback must reject target changes hidden by the public tree."""
    backend, services, workspace = _fake_mac_backend()
    services.attributes["button"].update(identifier="record-A", title="x" * 160 + "old", value="y" * 160 + "old")
    services.attributes["wrapper"] = {"role": "AXGroup", "children": ["button"]}
    services.attributes["window-1"]["children"] = ["wrapper"]
    services.actions["button"] = [*(f"AXAction{index}" for index in range(8)), "AXZOld"]
    state = backend.get_app_state("com.example.Editor")

    def change_identity() -> None:
        if change == "identifier":
            services.attributes["button"]["identifier"] = "record-B"
        elif change == "title":
            services.attributes["button"]["title"] = "x" * 160 + "new"
        elif change == "value":
            services.attributes["button"]["value"] = "y" * 160 + "new"
        elif change == "reference":
            services.attributes["replacement"] = dict(services.attributes["button"])
            services.actions["replacement"] = services.actions["button"]
            services.attributes["wrapper"]["children"] = ["replacement"]
        elif change == "ancestor":
            services.attributes["replacement-wrapper"] = dict(services.attributes["wrapper"])
            services.attributes["window-1"]["children"] = ["replacement-wrapper"]
        else:
            services.actions["button"][-1] = "AXZNew"

    if during_activation:
        workspace.applications[0].activation_hook = change_identity
    else:
        change_identity()
    with pytest.raises(AccessibilityError, match="state changed"):
        backend.prepare_fallback(state.app_id, state.state_id)


def test_untargeted_keyboard_guard_requires_the_validated_app_to_stay_frontmost() -> None:
    """Keys without an exact element still go only to the allowlisted app that was focused and checked."""
    backend, _, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    guard = backend.prepare_keyboard(state.app_id, state.state_id)
    assert workspace.applications[0].activation_options == [0]
    guard()
    workspace.applications[0].active = False
    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="lost keyboard focus"):
        guard()


def test_untargeted_keyboard_guard_reports_nothing_typed_when_focus_is_lost_before_any_input() -> None:
    """The first check runs before any key is posted, so its failure is a definite refusal, not an unknown outcome."""
    backend, _, workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    guard = backend.prepare_keyboard(state.app_id, state.state_id)
    workspace.applications[0].active = False
    with pytest.raises(AccessibilityError, match="nothing was typed") as refused:
        guard()
    assert type(refused.value) is AccessibilityError


def test_primary_screen_keyboard_input_needs_no_app_focus_guard() -> None:
    """The primary-screen target already covers whichever app has keyboard focus."""
    backend = ScreenshotOnlyAccessibilityBackend(frozenset({PRIMARY_SCREEN_APP_ID}), lambda: (1920, 1080))
    state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, state.state_id)()
    with pytest.raises(AccessibilityError, match="stale"):
        backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, "old-state")


def test_primary_screen_keyboard_guard_refuses_a_frontmost_mindroom() -> None:
    """Whole-screen keys go to whichever app is frontmost, so they stop whenever that is MindRoom."""
    backend, _, workspace = _fake_mac_backend()
    backend._allowed_app_ids = frozenset({PRIMARY_SCREEN_APP_ID})
    state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    guard = backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, state.state_id)
    guard()
    workspace.frontmost = FakeRunningApplication(pid=45, bundle_id="chat.mindroom.menubar", name="MindRoom")
    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="MindRoom became the active application"):
        guard()
    for mindroom_id in ("chat.mindroom.menubar", "chat.mindroom.desktophelper"):
        workspace.frontmost = FakeRunningApplication(pid=45, bundle_id=mindroom_id, name="MindRoom")
        with pytest.raises(AccessibilityError, match="nothing was typed") as refused:
            backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, state.state_id)()
        assert type(refused.value) is AccessibilityError
    workspace.frontmost = None
    backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, state.state_id)()


def _refuse_input() -> None:
    msg = "revoked"
    raise RuntimeError(msg)


@pytest.mark.parametrize("action", ["click_element", "perform_action", "set_value", "prepare_typing"])
def test_closed_input_gate_blocks_each_accessibility_effect_after_revalidation(action: str) -> None:
    """The gate runs after every revalidation and immediately before the press, value write, or focus write."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(role="AXTextField", settable=True)
    state = backend.get_app_state("com.example.Editor")
    field = next(element for element in state.elements if element.name == "Save")
    backend._before_input = _refuse_input
    effects: dict[str, Callable[[], object]] = {
        "click_element": lambda: backend.click_element(state.app_id, state.state_id, field.index),
        "perform_action": lambda: backend.perform_action(state.app_id, state.state_id, field.index, "AXPress"),
        "set_value": lambda: backend.set_value(state.app_id, state.state_id, field.index, "published"),
        "prepare_typing": lambda: backend.prepare_typing(state.app_id, state.state_id, field.index),
    }

    with pytest.raises(RuntimeError, match="revoked"):
        effects[action]()

    assert services.performed_actions == []
    assert services.attributes["button"]["value"] == "draft"
    assert services.focused_element is None


def test_revoke_between_the_focus_press_and_the_value_write_keeps_the_value() -> None:
    """Each accessibility effect is gated on its own, so a revoke after the focus press stops the write."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(role="AXTextField", settable=True)
    state = backend.get_app_state("com.example.Editor")
    field = next(element for element in state.elements if element.name == "Save")
    gate_checks = 0

    def gate() -> None:
        nonlocal gate_checks
        gate_checks += 1
        if gate_checks > 1:
            _refuse_input()

    backend._before_input = gate

    with pytest.raises(RuntimeError, match="revoked"):
        backend.set_value(state.app_id, state.state_id, field.index, "published")

    assert services.performed_actions == [("button", "AXPress")]
    assert services.attributes["button"]["value"] == "draft"


def test_unresponsive_app_fails_within_the_collection_deadline_and_the_screen_still_observes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree read past its deadline fails instead of holding the lane, and the whole screen stays observable."""
    backend, services, _ = _fake_mac_backend()
    backend._allowed_app_ids = frozenset({"com.example.Editor", PRIMARY_SCREEN_APP_ID})
    now = [100.0]
    monkeypatch.setattr("mindroom.desktop.accessibility.time.monotonic", lambda: now[0])
    services.collection_hook = lambda _count: now.__setitem__(0, now[0] + 6.0)

    with pytest.raises(AccessibilityError, match="did not answer accessibility requests in time") as caught:
        backend.get_app_state("com.example.Editor")

    assert str(caught.value) == (
        "Application 'com.example.Editor' did not answer accessibility requests in time; "
        "it may be busy or not responding."
    )
    assert backend._states == OrderedDict()
    assert backend._collect_deadline is None
    assert backend.get_app_state(PRIMARY_SCREEN_APP_ID).app_id == PRIMARY_SCREEN_APP_ID


def test_hung_app_holds_a_collection_for_its_deadline_plus_at_most_one_message_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is checked before each message, so a tree read can overrun it by one message timeout."""
    backend, services, _ = _fake_mac_backend()
    now = [100.0]
    monkeypatch.setattr("mindroom.desktop.accessibility.time.monotonic", lambda: now[0])
    answer_attribute = services.AXUIElementCopyAttributeValue
    answer_actions = services.AXUIElementCopyActionNames
    hung = {"button", "password"}

    # The window answers, but every message to an element inside it waits out the 2-second messaging timeout.
    def copy_attribute(reference: object, attribute: str, unused: object) -> tuple[int, object | None]:
        if reference in hung:
            now[0] += 2.0
            return -25204, None
        return answer_attribute(reference, attribute, unused)

    def copy_actions(reference: object, unused: object) -> tuple[int, object | None]:
        if reference in hung:
            now[0] += 2.0
            return -25204, None
        return answer_actions(reference, unused)

    monkeypatch.setattr(services, "AXUIElementCopyAttributeValue", copy_attribute)
    monkeypatch.setattr(services, "AXUIElementCopyActionNames", copy_actions)

    with pytest.raises(AccessibilityError, match="did not answer"):
        backend.get_app_state("com.example.Editor")

    assert 5.0 < now[0] - 100.0 <= 5.0 + 2.0
    assert backend._collect_deadline is None


def test_an_action_on_an_app_that_stops_answering_drops_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revalidation past the deadline fails before input and the stale state cannot be used again."""
    backend, services, _ = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    button = next(element for element in state.elements if element.name == "Save")
    now = [100.0]
    monkeypatch.setattr("mindroom.desktop.accessibility.time.monotonic", lambda: now[0])
    services.collection_hook = lambda _count: now.__setitem__(0, now[0] + 6.0)

    with pytest.raises(AccessibilityError, match="did not answer"):
        backend.click_element(state.app_id, state.state_id, button.index)

    assert state.state_id not in backend._states
    assert backend._collect_deadline is None
    assert services.performed_actions == []


def test_state_stabilization_gives_up_after_its_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree that keeps changing is returned as unstable after the budget instead of after every attempt."""
    backend, services, _ = _fake_mac_backend()
    now = [100.0]
    monkeypatch.setattr("mindroom.desktop.accessibility.time.monotonic", lambda: now[0])
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _delay: now.__setitem__(0, now[0] + 3.0))

    def churn(count: int) -> None:
        services.attributes["button"]["value"] = f"draft-{count}"

    services.collection_hook = churn

    assert backend.get_app_state("com.example.Editor").stability == "unstable"
    assert services.collection_count == 4


def test_mac_backend_bounds_every_accessibility_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process-wide messaging timeout is set once, on the system-wide element, when the backend starts."""
    services = SimpleNamespace(AXUIElementCreateSystemWide=lambda: "system", AXUIElementSetMessagingTimeout=Mock())
    appkit = SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=object))
    monkeypatch.setitem(sys.modules, "ApplicationServices", services)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    MacAccessibilityBackend(frozenset({"com.example.Editor"}), lambda: (1, 1), lambda: None)

    services.AXUIElementSetMessagingTimeout.assert_called_once_with("system", 2.0)


def test_slow_app_holds_get_app_state_for_its_budget_plus_at_most_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is checked before each re-read, so a read that starts just inside it can overrun it by one read."""
    backend, services, _ = _fake_mac_backend()
    now = [100.0]
    monkeypatch.setattr("mindroom.desktop.accessibility.time.monotonic", lambda: now[0])
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _delay: None)

    def slow_read(count: int) -> None:
        # Each read finishes just inside its collection deadline, and the tree keeps changing.
        now[0] += 4.9
        services.attributes["button"]["value"] = f"draft-{count}"

    services.collection_hook = slow_read

    assert backend.get_app_state("com.example.Editor").stability == "unstable"
    assert services.collection_count == 2
    assert 8.0 < now[0] - 100.0 <= 8.0 + 7.0


@pytest.mark.parametrize("running", [False, True])
def test_closed_input_gate_stops_a_launch_before_it_is_requested_or_activated(running: bool) -> None:
    """Launching or foregrounding an app is control, so a revoke stops it before macOS is asked."""
    backend, _, workspace = _fake_mac_backend()
    requested: list[str] = []
    backend._before_input = _refuse_input
    if not running:
        workspace.applications = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("mindroom.desktop.accessibility._request_application_activation", requested.append)
        with pytest.raises(RuntimeError, match="revoked"):
            backend.launch_app("com.example.Editor")
    assert requested == []
    assert all(application.activation_options == [] for application in workspace.applications)
