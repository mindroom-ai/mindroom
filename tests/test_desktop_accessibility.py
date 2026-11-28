"""Tests for portable accessibility state and stale-state safety."""

# ruff: noqa: D102, N802, N815

from __future__ import annotations

import sys
from collections import OrderedDict, UserList
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

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
    ElementActionTarget,
    KeyboardTarget,
    MacAccessibilityBackend,
    ScreenshotOnlyAccessibilityBackend,
)


class FakeOpenConfiguration:
    """Minimal NSWorkspaceOpenConfiguration test double."""

    def __init__(self) -> None:
        self.activates: bool | None = None
        self.hides: bool | None = None
        self.creates_new_instance: bool | None = None

    @classmethod
    def configuration(cls) -> FakeOpenConfiguration:
        return cls()

    def setActivates_(self, value: bool) -> None:
        self.activates = value

    def setHides_(self, value: bool) -> None:
        self.hides = value

    def setCreatesNewApplicationInstance_(self, value: bool) -> None:
        self.creates_new_instance = value


def _install_fake_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(NSWorkspaceOpenConfiguration=FakeOpenConfiguration),
    )


class FakeRunningApplication:
    """Small NSRunningApplication-compatible test object."""

    def __init__(
        self,
        pid: int = 42,
        *,
        active: bool = True,
        hidden: bool = False,
        bundle_url: object | None = "file:///Applications/Editor.app",
        launch_date: object = 1.0,
    ) -> None:
        self.pid = pid
        self.active = active
        self.hidden = hidden
        self.bundle_url = bundle_url
        self.launch_date = launch_date
        self.unhide_calls = 0
        self.activation_hook: Callable[[], None] | None = None
        self.activation_options: list[int] = []
        self.activate_returns = True

    def bundleIdentifier(self) -> str:
        return "com.example.Editor"

    def localizedName(self) -> str:
        return "Editor"

    def processIdentifier(self) -> int:
        return self.pid

    def launchDate(self) -> object:
        return self.launch_date

    def isActive(self) -> bool:
        return self.active

    def isHidden(self) -> bool:
        return self.hidden

    def unhide(self) -> bool:
        self.unhide_calls += 1
        self.hidden = False
        return True

    def bundleURL(self) -> object | None:
        return self.bundle_url

    def activationPolicy(self) -> int:
        return 0  # Regular: has a Dock icon.

    def activateWithOptions_(self, options: int) -> bool:
        self.activation_options.append(options)
        if self.activation_hook is not None:
            self.activation_hook()
        return self.activate_returns


class FakeWorkspace:
    """Mutable app list and NSWorkspace launch surface for deterministic activation tests."""

    def __init__(self, application: FakeRunningApplication) -> None:
        self.applications = [application]
        self.installed_urls: dict[str, object | None] = {"com.example.Editor": "file:///Applications/Editor.app"}
        self.url_queries: list[str] = []
        self.open_calls: list[tuple[object, FakeOpenConfiguration]] = []
        self.open_hook: Callable[[object, FakeOpenConfiguration, Callable[[object, object], None]], None] | None = None

    def runningApplications(self) -> list[FakeRunningApplication]:
        return self.applications

    def URLForApplicationWithBundleIdentifier_(self, bundle_identifier: str) -> object | None:
        self.url_queries.append(bundle_identifier)
        return self.installed_urls.get(bundle_identifier)

    def openApplicationAtURL_configuration_completionHandler_(
        self,
        url: object,
        configuration: FakeOpenConfiguration,
        completion_handler: Callable[[object, object], None],
    ) -> None:
        self.open_calls.append((url, configuration))
        if self.open_hook is not None:
            self.open_hook(url, configuration, completion_handler)
            return
        completion_handler(None, None)


class FakeMacServices:
    """Small AXUIElement surface for deterministic tree and pinning tests."""

    kAXErrorSuccess = 0
    kAXFocusedWindowAttribute = "focused_window"
    kAXFocusedAttribute = "focused"
    kAXFocusedUIElementAttribute = "focused_element"
    kAXFrontmostAttribute = "frontmost"
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
        self.app_attribute_calls: list[tuple[tuple[str, int], str, object]] = []
        self.app_attribute_errors: dict[str, int] = {}
        self.frontmost_calls: list[tuple[str, int]] = []
        self.frontmost_error = 0
        self.frontmost_hook: Callable[[], None] | None = None

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
        if isinstance(reference, tuple) and reference[0] == "app":
            self.app_attribute_calls.append((reference, attribute, value))
            if attribute == self.kAXFrontmostAttribute:
                self.frontmost_calls.append(reference)
                if self.frontmost_hook is not None:
                    self.frontmost_hook()
                return self.frontmost_error
            return self.app_attribute_errors.get(attribute, 0)
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
    backend._ax_enabled_pids = {}
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


def test_mac_capture_allows_dynamic_content_without_activating_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inactive app window can be captured with its AX window ID without foregrounding."""
    backend, services, workspace = _fake_mac_backend()
    workspace.applications[0].active = False
    monkeypatch.setattr("mindroom.desktop.accessibility.macos_skylight.ax_window_id", lambda _win: 77)
    state = backend.get_app_state("com.example.Editor")
    services.attributes["button"].update(value="published")

    captured = backend.prepare_capture(state.app_id, state.state_id)

    assert captured == AccessibilityCapture(state, 42, window_id=77)
    assert workspace.applications[0].activation_options == []
    assert services.frontmost_calls == []


def test_mac_fallback_rejects_dynamic_content_without_activating_app() -> None:
    """Pixel input still requires exact visual state without raising the app."""
    backend, services, workspace = _fake_mac_backend()
    workspace.applications[0].active = False
    state = backend.get_app_state("com.example.Editor")
    services.attributes["button"].update(value="published")

    with pytest.raises(AccessibilityError, match="state changed"):
        backend.prepare_fallback(state.app_id, state.state_id)
    assert workspace.applications[0].activation_options == []


def test_mac_routine_operations_never_activate_background_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """All capture, semantic, fallback, keyboard, typing, and launch operations stay background-only."""
    backend, services, workspace = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("mindroom.desktop.accessibility.macos_skylight.ax_window_id", lambda _win: 321)
    application = workspace.applications[0]
    application.active = False
    application.hidden = True
    services.attributes["button"].update(role="AXTextField", settable=True)
    state = backend.get_app_state("com.example.Editor")

    assert backend.prepare_capture(state.app_id, state.state_id) == AccessibilityCapture(state, 42, window_id=321)
    assert backend.prepare_fallback(state.app_id, state.state_id) == AccessibilityCapture(state, 42, window_id=321)
    kb = backend.prepare_keyboard(state.app_id, state.state_id)
    assert isinstance(kb, KeyboardTarget)
    assert (kb.process_id, kb.window_id, kb.window) == (42, 321, state.window)
    kb()
    typing = backend.prepare_typing(state.app_id, state.state_id, 1)
    assert (typing.process_id, typing.window_id, typing.window) == (42, 321, state.window)
    typing()
    action_target = backend.element_for_action(state.app_id, state.state_id, 1)
    assert action_target == ElementActionTarget(state.elements[1], 42, window_id=321, window=state.window)
    backend.click_element(state.app_id, state.state_id, 1)
    backend.perform_action(state.app_id, state.state_id, 1, "AXPress")
    backend.set_value(state.app_id, state.state_id, 1, "updated")
    backend.launch_app(state.app_id)

    assert application.unhide_calls == 1
    assert application.activation_options == []
    assert services.frontmost_calls == []
    assert workspace.open_calls == []


def test_mac_launches_only_the_exact_allowlisted_bundle_id_in_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped allowlisted app launches via NSWorkspace openApplication with activates=False."""
    backend, _, workspace = _fake_mac_backend()
    _install_fake_appkit(monkeypatch)
    workspace.applications = []
    launched = FakeRunningApplication(active=False)

    def launch(_url: object, _config: FakeOpenConfiguration, handler: Callable[[object, object], None]) -> None:
        workspace.applications.append(launched)
        handler(launched, None)

    workspace.open_hook = launch

    backend.launch_app("com.example.Editor")

    assert workspace.url_queries == ["com.example.Editor"]
    assert len(workspace.open_calls) == 1
    url, config = workspace.open_calls[0]
    assert url == "file:///Applications/Editor.app"
    assert (config.activates, config.hides, config.creates_new_instance) == (False, False, False)
    assert launched.activation_options == []


def test_mac_launch_rejects_invalid_and_uninstalled_bundle_id_before_request() -> None:
    """Invalid or uninstalled bundle IDs fail definitively before any NSWorkspace launch side effect."""
    backend, _, workspace = _fake_mac_backend()
    backend._allowed_app_ids = frozenset({"invalid_app", "com.example.Missing"})
    workspace.applications = []

    with pytest.raises(AccessibilityError, match="invalid bundle identifier") as exc_info:
        backend.launch_app("invalid_app")
    assert type(exc_info.value) is AccessibilityError

    with pytest.raises(AccessibilityError, match="not installed") as missing_exc:
        backend.launch_app("com.example.Missing")
    assert type(missing_exc.value) is AccessibilityError
    assert workspace.open_calls == []


def test_mac_launch_timeout_and_open_error_have_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted NSWorkspace launch that never finishes, errors, or never appears reports unknown outcome."""
    backend, _, workspace = _fake_mac_backend()
    _install_fake_appkit(monkeypatch)
    workspace.applications = []
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)

    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome is unknown"):
        backend.launch_app("com.example.Editor")

    monkeypatch.setattr("mindroom.desktop.accessibility._OPEN_APPLICATION_TIMEOUT_SECONDS", 0.01)
    workspace.open_hook = lambda _url, _cfg, _handler: None
    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome is unknown"):
        backend.launch_app("com.example.Editor")

    workspace.open_hook = lambda _url, _cfg, handler: handler(None, RuntimeError("launch services error"))
    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome is unknown"):
        backend.launch_app("com.example.Editor")


def test_mac_chromium_ax_enablement_caches_by_pid_and_launch_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """AXManualAccessibility caches per (pid, launchDate) and never enables AXEnhancedUserInterface."""
    backend, services, workspace = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    services.app_attribute_errors["AXManualAccessibility"] = -25205  # kAXErrorAttributeUnsupported

    backend.get_app_state("com.example.Editor")
    backend.get_app_state("com.example.Editor")
    assert services.app_attribute_calls == [(("app", 42), "AXManualAccessibility", True)]

    # Same PID reused with a newer launchDate -> cache misses and AX enablement runs again.
    services.app_attribute_calls.clear()
    services.app_attribute_errors.clear()
    workspace.applications = [FakeRunningApplication(pid=42, launch_date=2.0)]
    backend.get_app_state("com.example.Editor")
    assert services.app_attribute_calls == [(("app", 42), "AXManualAccessibility", True)]

    # Fake app lacking launchDate still caches safely per PID.
    services.app_attribute_calls.clear()
    app_no_date = SimpleNamespace(
        bundleIdentifier=lambda: "com.example.Editor",
        localizedName=lambda: "Editor",
        processIdentifier=lambda: 99,
        isActive=lambda: False,
        activationPolicy=lambda: 0,
    )
    workspace.applications = [app_no_date]  # type: ignore[list-item]
    backend.get_app_state("com.example.Editor")
    backend.get_app_state("com.example.Editor")
    assert services.app_attribute_calls == [(("app", 99), "AXManualAccessibility", True)]


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


def test_mac_semantic_action_rejects_replaced_target() -> None:
    """Identical visible fields cannot authorize a different OS element."""
    backend, services, _ = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")

    services.attributes["replacement"] = dict(services.attributes["button"])
    services.actions["replacement"] = services.actions["button"]
    services.attributes["window-1"]["children"] = UserList(["replacement", "password"])
    with pytest.raises(AccessibilityError, match="target element changed"):
        backend.click_element(state.app_id, state.state_id, 1)
    assert services.performed_actions == []


@pytest.mark.parametrize("operation", ["click", "value", "element"])
def test_mac_semantic_target_survives_unrelated_insertion(operation: str) -> None:
    """Index remapping must still select the original OS target without raising."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(settable=True)
    state = backend.get_app_state("com.example.Editor")

    services.attributes["status"] = {"role": "AXStaticText", "title": "Synced"}
    services.attributes["window-1"]["children"] = UserList(["status", "button", "password"])
    if operation == "click":
        backend.click_element(state.app_id, state.state_id, 1)
        assert services.performed_actions == [("button", "AXPress")]
    elif operation == "value":
        backend.set_value(state.app_id, state.state_id, 1, "published")
        assert services.attributes["button"]["value"] == "published"
    else:
        target = backend.element_for_action(state.app_id, state.state_id, 1)
        assert (target.element.name, target.process_id, target.window) == ("Save", 42, state.window)


@pytest.mark.parametrize("change", ["title", "value", "geometry", "parent"])
def test_mac_semantic_target_rejects_changed_identity_fields(change: str) -> None:
    """Clipped text and omitted wrappers must not hide a change to the exact target."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(title="x" * 160 + "old", value="y" * 160 + "old")
    services.attributes["window-1"]["children"] = UserList(["wrapper"])
    services.attributes["wrapper"] = {"role": "AXGroup", "children": UserList(["button"])}
    state = backend.get_app_state("com.example.Editor")

    if change == "title":
        services.attributes["button"]["title"] = "x" * 160 + "new"
    elif change == "value":
        services.attributes["button"]["value"] = "y" * 160 + "new"
    elif change == "geometry":
        services.attributes["window-1"]["position"] = SimpleNamespace(x=20, y=30)
    else:
        services.attributes["replacement-wrapper"] = dict(services.attributes["wrapper"])
        services.attributes["window-1"]["children"] = UserList(["replacement-wrapper"])

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


class NamedRunningApplication(FakeRunningApplication):
    """A running app with its own bundle identifier."""

    def __init__(self, bundle_id: str, *, policy: int = 0) -> None:
        super().__init__()
        self.bundle_id = bundle_id
        self.policy = policy

    def bundleIdentifier(self) -> str:
        return self.bundle_id

    def localizedName(self) -> str:
        return self.bundle_id

    def activationPolicy(self) -> int:
        return self.policy


def test_all_apps_backend_lists_and_admits_every_running_app_but_mindroom() -> None:
    """Computer scope reaches any app and the primary screen, never MindRoom's own windows, and ends with the flag."""
    backend, _, workspace = _fake_mac_backend()
    workspace.applications = [
        NamedRunningApplication("com.example.Terminal"),
        NamedRunningApplication("chat.mindroom.menubar"),
        NamedRunningApplication("com.example.BackgroundAgent", policy=2),  # Reachable by id, not listed.
    ]

    assert [app.app_id for app in backend.list_apps()] == ["com.example.Editor"]  # Saved apps only.
    with pytest.raises(AccessibilityError, match="allowlist"):
        backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    backend.all_apps = True

    assert [app.to_result() for app in backend.list_apps()] == [
        {"id": "com.example.Editor", "name": "com.example.Editor", "running": False},
        {"id": "com.example.Terminal", "name": "com.example.Terminal", "running": True},
        {"id": PRIMARY_SCREEN_APP_ID, "name": "Primary Screen", "running": True},
    ]
    state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    backend._require_allowed("com.example.NotRunning")
    backend._require_allowed("com.example.BackgroundAgent")
    with pytest.raises(AccessibilityError, match="allowlist"):
        backend._require_allowed("chat.mindroom.menubar")
    backend.all_apps = False

    with pytest.raises(AccessibilityError, match="allowlist"):
        backend.prepare_capture(PRIMARY_SCREEN_APP_ID, state.state_id)


def test_screenshot_only_backend_reaches_the_primary_screen_only_while_all_apps_is_set() -> None:
    """Portable pixel mode gains the primary screen, and no semantic app, from computer scope."""
    backend = ScreenshotOnlyAccessibilityBackend(frozenset({"com.example.Editor"}), lambda: (1920, 1080))

    with pytest.raises(AccessibilityError, match="allowlist"):
        backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    backend.all_apps = True

    assert [app.app_id for app in backend.list_apps()] == ["com.example.Editor", PRIMARY_SCREEN_APP_ID]
    assert backend.get_app_state(PRIMARY_SCREEN_APP_ID).app_id == PRIMARY_SCREEN_APP_ID
    with pytest.raises(AccessibilityError, match="only on macOS"):
        backend.get_app_state("com.example.Terminal")


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


@pytest.mark.parametrize("change", ["identifier", "title", "value", "reference", "ancestor", "actions"])
def test_fallback_rejects_hidden_actionable_identity_changes(change: str) -> None:
    """Coordinate/key fallback must reject target changes hidden by the public tree."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["button"].update(identifier="record-A", title="x" * 160 + "old", value="y" * 160 + "old")
    services.attributes["wrapper"] = {"role": "AXGroup", "children": ["button"]}
    services.attributes["window-1"]["children"] = ["wrapper"]
    services.actions["button"] = [*(f"AXAction{index}" for index in range(8)), "AXZOld"]
    state = backend.get_app_state("com.example.Editor")

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

    with pytest.raises(AccessibilityError, match="state changed"):
        backend.prepare_fallback(state.app_id, state.state_id)


def test_untargeted_keyboard_guard_works_in_background_and_tracks_window_validity() -> None:
    """Background keyboard guard does not require foreground activation, but stops if the window drifts mid-typing."""
    backend, services, workspace = _fake_mac_backend()
    workspace.applications[0].active = False
    state = backend.get_app_state("com.example.Editor")
    target = backend.prepare_keyboard(state.app_id, state.state_id)
    assert workspace.applications[0].activation_options == []
    assert target.process_id == 42
    target()
    services.attributes["window-1"]["size"] = SimpleNamespace(width=900, height=600)
    with pytest.raises(AccessibilityActionOutcomeUnknownError, match="outcome may be partial"):
        target()


def test_untargeted_keyboard_guard_reports_nothing_typed_when_window_changes_before_any_input() -> None:
    """The first check runs before any key is posted, so its failure is a definite refusal, not an unknown outcome."""
    backend, services, workspace = _fake_mac_backend()
    workspace.applications[0].active = False
    state = backend.get_app_state("com.example.Editor")
    target = backend.prepare_keyboard(state.app_id, state.state_id)
    services.attributes["window-1"]["minimized"] = True
    with pytest.raises(AccessibilityError, match="nothing was typed") as refused:
        target()
    assert type(refused.value) is AccessibilityError


def test_prepare_keyboard_rejects_app_state_without_window_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """An app-backed snapshot missing window_reference must fail rather than falling back to primary-screen."""
    backend, _services, _workspace = _fake_mac_backend()
    state = backend.get_app_state("com.example.Editor")
    broken = replace(backend._states[state.state_id], window_reference=None)
    backend._states[state.state_id] = broken
    with pytest.raises(AccessibilityError, match="stale"):
        backend.prepare_keyboard(state.app_id, state.state_id)
    monkeypatch.setattr(backend, "_fresh_state", lambda _app_id, _state_id: broken)
    with pytest.raises(AccessibilityError, match="target window is missing"):
        backend.prepare_keyboard(state.app_id, state.state_id)


def test_primary_screen_keyboard_input_needs_no_app_focus_guard() -> None:
    """The primary-screen target already covers whichever app has keyboard focus."""
    backend = ScreenshotOnlyAccessibilityBackend(frozenset({PRIMARY_SCREEN_APP_ID}), lambda: (1920, 1080))
    state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, state.state_id)()
    with pytest.raises(AccessibilityError, match="stale"):
        backend.prepare_keyboard(PRIMARY_SCREEN_APP_ID, "old-state")


def test_mac_flatten_skips_empty_string_containers_and_keeps_empty_textfields() -> None:
    """Blank Chromium groups and read-only cells are skipped while writable cells, text fields, and descendants stay."""
    backend, services, _ = _fake_mac_backend()
    services.attributes["window-1"]["children"] = UserList(
        ["empty-group", "space-group", "writable-cell", "readonly-cell", "text-field"],
    )
    services.attributes["empty-group"] = {
        "role": "AXGroup",
        "value": "",
        "settable": True,
        "children": UserList(["nested-button"]),
    }
    services.attributes["space-group"] = {
        "role": "AXGroup",
        "value": "   \t\n",
        "children": UserList([]),
    }
    services.attributes["writable-cell"] = {"role": "AXCell", "value": "", "settable": True}
    services.attributes["readonly-cell"] = {"role": "AXCell", "value": "", "settable": False}
    services.attributes["nested-button"] = {"role": "AXButton", "title": "Nested"}
    services.attributes["text-field"] = {"role": "AXTextField", "value": ""}

    settable_calls: dict[object, int] = {}
    orig_settable = services.AXUIElementIsAttributeSettable

    def counting_settable(reference: object, attribute: str, unused: object) -> tuple[int, bool]:
        settable_calls[reference] = settable_calls.get(reference, 0) + 1
        return orig_settable(reference, attribute, unused)

    services.AXUIElementIsAttributeSettable = counting_settable  # type: ignore[method-assign]

    stored = backend._collect_app_state("com.example.Editor", state_id="s1")

    roles_and_names = [(el.role, el.name, el.value, el.settable, el.parent_index) for el in stored.public.elements]
    assert roles_and_names == [
        ("AXWindow", "Editor", None, False, None),
        ("AXCell", None, "", True, 0),
        ("AXTextField", None, "", False, 0),
        ("AXButton", "Nested", None, False, 0),
    ]
    assert settable_calls["writable-cell"] == 1
    nested_idx = next(el.index for el in stored.public.elements if el.name == "Nested")
    assert stored.identities[nested_idx].ancestors == ("window-1", "empty-group")


def test_mac_flatten_avoids_duplicate_label_and_action_ipcs() -> None:
    """Flattening fetches each label attribute and action list at most once per node."""
    backend, services, _ = _fake_mac_backend()
    attr_calls: dict[tuple[object, str], int] = {}
    action_calls: dict[object, int] = {}
    orig_attr = services.AXUIElementCopyAttributeValue
    orig_actions = services.AXUIElementCopyActionNames

    def counting_attr(reference: object, attribute: str, unused: object) -> tuple[int, object | None]:
        attr_calls[(reference, attribute)] = attr_calls.get((reference, attribute), 0) + 1
        return orig_attr(reference, attribute, unused)

    def counting_actions(reference: object, unused: object) -> tuple[int, object]:
        action_calls[reference] = action_calls.get(reference, 0) + 1
        return orig_actions(reference, unused)

    services.AXUIElementCopyAttributeValue = counting_attr  # type: ignore[method-assign]
    services.AXUIElementCopyActionNames = counting_actions  # type: ignore[method-assign]

    stored = backend._collect_app_state("com.example.Editor", state_id="s1")

    for ref in ("window-1", "button"):
        assert action_calls[ref] == 1
        for attr in ("title", "description", "help", "identifier"):
            assert attr_calls[(ref, attr)] == 1
    button_idx = next(el.index for el in stored.public.elements if el.name == "Save")
    assert stored.identities[button_idx].content_fingerprint == backend._element_content_fingerprint("button", "draft")


def test_mac_flatten_caps_queued_children_bounds_app_name_and_formats_refs() -> None:
    """Excess children are capped to the remaining visit budget, app_name is bounded, and refs are 24 hex chars."""
    backend, services, workspace = _fake_mac_backend()
    workspace.applications[0].localizedName = lambda: "X" * 300  # type: ignore[method-assign]
    services.attributes["window-1"]["children"] = UserList([f"child-{i}" for i in range(600)])
    for i in range(600):
        services.attributes[f"child-{i}"] = {"role": "AXGroup", "value": ""}

    stored = backend._collect_app_state("com.example.Editor", state_id="s1")

    assert stored.public.truncated is True
    assert len(stored.public.app_name) == 160
    ref = stored.public.element_ref(0)
    assert len(ref) == 24
    assert all(ch in "0123456789abcdef" for ch in ref)
    without_elements = stored.public.to_result(include_elements=False)
    assert "elements" not in without_elements
    assert without_elements["state_id"] == "s1"


def test_mac_prepare_capture_revalidates_window_without_walking_ax_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_capture revalidates window identity and geometry without calling _flatten_tree."""
    backend, services, _ = _fake_mac_backend()
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)
    state = backend.get_app_state("com.example.Editor")

    flatten_calls = 0
    orig_flatten = backend._flatten_tree

    def spy_flatten(root: object) -> object:
        nonlocal flatten_calls
        flatten_calls += 1
        return orig_flatten(root)

    monkeypatch.setattr(backend, "_flatten_tree", spy_flatten)
    capture = backend.prepare_capture("com.example.Editor", state.state_id)
    assert capture == AccessibilityCapture(state, 42)
    assert flatten_calls == 0

    # Window geometry change is caught and invalidates the retained state.
    services.attributes["window-1"]["size"] = SimpleNamespace(width=900, height=600)
    with pytest.raises(AccessibilityError, match="window geometry changed"):
        backend.prepare_capture("com.example.Editor", state.state_id)
    assert state.state_id not in backend._states
    assert flatten_calls == 0


def test_mac_flatten_deduplicates_shared_table_cells_on_enqueue() -> None:
    """Shared cell references across table rows and columns do not consume the visit budget or truncate later controls."""
    backend, services, _ = _fake_mac_backend()
    rows = [f"row-{r}" for r in range(16)]
    cols = [f"col-{c}" for c in range(16)]
    services.attributes["window-1"]["children"] = UserList(["table", "footer"])
    services.attributes["table"] = {"role": "AXTable", "children": UserList([*rows, *cols])}
    services.attributes["footer"] = {"role": "AXGroup", "children": UserList(["footer-inner"])}
    services.attributes["footer-inner"] = {"role": "AXGroup", "children": UserList(["save-btn"])}
    services.attributes["save-btn"] = {"role": "AXButton", "title": "Save"}
    for r in range(16):
        services.attributes[f"row-{r}"] = {
            "role": "AXRow",
            "children": UserList([f"cell-{r}-{c}" for c in range(16)]),
        }
    for c in range(16):
        services.attributes[f"col-{c}"] = {
            "role": "AXColumn",
            "children": UserList([f"cell-{r}-{c}" for r in range(16)]),
        }
    for r in range(16):
        for c in range(16):
            services.attributes[f"cell-{r}-{c}"] = {"role": "AXCell", "value": ""}

    stored = backend._collect_app_state("com.example.Editor", state_id="s1")

    assert stored.public.truncated is False
    assert [el.name for el in stored.public.elements] == ["Editor", "Save"]


def test_mac_get_app_state_without_elements_skips_tree_walk_and_stabilization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_elements=False resolves only the running window without walking AX or sleeping."""
    backend, services, _ = _fake_mac_backend()
    sleeps: list[float] = []
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", sleeps.append)
    monkeypatch.setattr(
        backend,
        "_flatten_tree",
        lambda _root: pytest.fail("_flatten_tree must not run when include_elements=False"),
    )

    state = backend.get_app_state("com.example.Editor", include_elements=False)

    assert state.elements == ()
    assert state.truncated is False
    assert state.stability == "stable"
    assert state.window == DesktopRect(10, 20, 800, 600)
    assert services.collection_count == 1
    assert sleeps == []
    assert backend._states[state.state_id].tree_captured is False
    # Window-bound screenshot capture still works on that state_id.
    capture = backend.prepare_capture(state.app_id, state.state_id)
    assert (capture.state.state_id, capture.process_id) == (state.state_id, 42)


@pytest.mark.parametrize("kind", ["screenshot_only", "truncated", "unstable"])
def test_mac_window_fallback_revalidates_window_without_activating(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Screenshot-backed fallback uses window revalidation without activating and still rejects window drift."""
    backend, services, workspace = _fake_mac_backend()
    workspace.applications[0].active = False
    monkeypatch.setattr("mindroom.desktop.accessibility.time.sleep", lambda _seconds: None)

    if kind == "screenshot_only":
        state = backend.get_app_state("com.example.Editor", include_elements=False)
    elif kind == "truncated":
        children = UserList([f"button-{index}" for index in range(128)])
        services.attributes["window-1"]["children"] = children
        for child in children:
            services.attributes[child] = {"role": "AXButton", "title": child}
        state = backend.get_app_state("com.example.Editor")
        assert state.truncated is True
    else:
        services.collection_hook = lambda count: services.attributes["button"].update(title=f"Save {count}")
        state = backend.get_app_state("com.example.Editor")
        assert state.stability == "unstable"
        services.collection_hook = None

    # Without allow_window_fallback, fallback stays strictly rejected.
    with pytest.raises(AccessibilityError, match="stable complete observation"):
        backend.prepare_fallback(state.app_id, state.state_id)
    with pytest.raises(AccessibilityError, match="stable complete observation"):
        backend.prepare_keyboard(state.app_id, state.state_id)

    # With allow_window_fallback=True, dynamic AX content changes do not block fallback or walk the tree.
    services.attributes["button"].update(value="changed")
    monkeypatch.setattr(
        backend,
        "_flatten_tree",
        lambda _root: pytest.fail("window-level fallback must not walk AX"),
    )
    capture = backend.prepare_fallback(state.app_id, state.state_id, allow_window_fallback=True)
    assert (capture.state.state_id, capture.process_id) == (state.state_id, 42)
    guard = backend.prepare_keyboard(state.app_id, state.state_id, allow_window_fallback=True)
    guard()
    assert workspace.applications[0].activation_options == []

    # Window geometry change is caught without activating.
    services.attributes["window-1"]["size"] = SimpleNamespace(width=900, height=600)
    with pytest.raises(AccessibilityError, match="window geometry changed"):
        backend.prepare_fallback(state.app_id, state.state_id, allow_window_fallback=True)

    # Minimized window is rejected cleanly.
    services.attributes["window-1"]["size"] = SimpleNamespace(width=800, height=600)
    fresh = backend.get_app_state("com.example.Editor", include_elements=False)
    services.attributes["window-1"]["minimized"] = True
    with pytest.raises(AccessibilityError, match="minimized"):
        backend.prepare_fallback(fresh.app_id, fresh.state_id, allow_window_fallback=True)
    assert workspace.applications[0].activation_options == []

    # PRIMARY_SCREEN_APP_ID keeps strict screen-size fingerprint checks even with allow_window_fallback=True.
    backend._allowed_app_ids = frozenset({"com.example.Editor", PRIMARY_SCREEN_APP_ID})
    screen_state = backend.get_app_state(PRIMARY_SCREEN_APP_ID)
    backend._screen_size = lambda: (1280, 720)
    with pytest.raises(AccessibilityError, match="Accessibility state changed"):
        backend.prepare_fallback(PRIMARY_SCREEN_APP_ID, screen_state.state_id, allow_window_fallback=True)


def test_mac_flatten_discounts_unnamed_wrappers_up_to_raw_depth_cap() -> None:
    """Unnamed Chromium-style AXGroup wrappers do not burn semantic depth, while raw depth stays hard-capped."""
    backend, services, _ = _fake_mac_backend()

    # 20 unnamed AXGroup wrappers -> button still reached at semantic depth 1 without truncation.
    parent = "window-1"
    for index in range(20):
        node = f"wrapper-{index}"
        services.attributes[parent]["children"] = UserList([node])
        services.attributes[node] = {"role": "AXGroup"}
        parent = node
    services.attributes[parent]["children"] = UserList(["button"])

    state = backend.get_app_state("com.example.Editor")
    assert state.truncated is False
    assert [(el.role, el.depth, el.parent_index) for el in state.elements] == [
        ("AXWindow", 0, None),
        ("AXButton", 1, 0),
    ]

    # 45 unnamed wrappers exceed _MAX_RAW_ACCESSIBILITY_DEPTH (40) -> bounded and marked truncated.
    parent = "window-1"
    for index in range(45):
        node = f"deep-{index}"
        services.attributes[parent]["children"] = UserList([node])
        services.attributes[node] = {"role": "AXGroup"}
        parent = node
    services.attributes[parent]["children"] = UserList(["button"])

    deep_state = backend.get_app_state("com.example.Editor")
    assert deep_state.truncated is True
    assert [el.role for el in deep_state.elements] == ["AXWindow"]

    # Named semantic containers still cap at _MAX_ACCESSIBILITY_DEPTH (12).
    parent = "window-1"
    for index in range(13):
        node = f"named-{index}"
        services.attributes[parent]["children"] = UserList([node])
        services.attributes[node] = {"role": "AXGroup", "title": f"Level {index}"}
        parent = node
    services.attributes[parent]["children"] = UserList(["button"])

    semantic_state = backend.get_app_state("com.example.Editor")
    assert semantic_state.truncated is True
    assert max(el.depth for el in semantic_state.elements) == 12
