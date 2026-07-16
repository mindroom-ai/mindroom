"""App-scoped accessibility state for the local desktop bridge."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable


class _RunningApplication(Protocol):
    def bundleIdentifier(self) -> str | None: ...  # noqa: N802

    def localizedName(self) -> str | None: ...  # noqa: N802

    def processIdentifier(self) -> int: ...  # noqa: N802

    def isActive(self) -> bool: ...  # noqa: N802

    def activateWithOptions_(self, options: int) -> bool: ...  # noqa: N802


class _Workspace(Protocol):
    def runningApplications(self) -> list[_RunningApplication]: ...  # noqa: N802


PRIMARY_SCREEN_APP_ID = "primary-screen"
MAX_ACCESSIBILITY_ELEMENTS = 128
_MAX_ACCESSIBILITY_DEPTH = 12
_MAX_VISITED_ELEMENTS = 2048
_MAX_TEXT_LENGTH = 160
_CONTAINER_ROLES = frozenset(
    {
        "AXApplication",
        "AXGroup",
        "AXLayoutArea",
        "AXList",
        "AXOutline",
        "AXScrollArea",
        "AXSplitGroup",
        "AXTable",
        "AXUnknown",
        "AXWebArea",
        "AXWindow",
    },
)


class AccessibilityError(RuntimeError):
    """An accessibility observation or validation failed safely."""


class AccessibilityActionOutcomeUnknownError(AccessibilityError):
    """An accessibility action failed after it may have changed local state."""


@dataclass(frozen=True, slots=True)
class DesktopRect:
    """One rectangle in logical desktop coordinates."""

    x: int
    y: int
    width: int
    height: int

    def to_result(self) -> dict[str, int]:
        """Return a JSON-safe rectangle."""
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class AccessibilityElement:
    """One state-scoped semantic UI element exposed to the cloud agent."""

    index: int
    depth: int
    parent_index: int | None
    role: str
    name: str | None
    value: str | int | float | bool | None
    enabled: bool | None
    settable: bool
    bounds: DesktopRect | None
    actions: tuple[str, ...]

    def to_result(self) -> dict[str, object]:
        """Return the bounded semantic fields sent through Matrix."""
        result: dict[str, object] = {
            "index": self.index,
            "depth": self.depth,
            "role": self.role,
            "settable": self.settable,
            "actions": list(self.actions),
        }
        if self.parent_index is not None:
            result["parent_index"] = self.parent_index
        if self.name is not None:
            result["name"] = self.name
        if self.value is not None:
            result["value"] = self.value
        if self.enabled is not None:
            result["enabled"] = self.enabled
        if self.bounds is not None:
            result["bounds"] = self.bounds.to_result()
        return result


@dataclass(frozen=True, slots=True)
class AccessibilityState:
    """One fresh app state whose element indexes expire on the next state change."""

    state_id: str
    app_id: str
    app_name: str
    window: DesktopRect
    elements: tuple[AccessibilityElement, ...]
    truncated: bool

    def to_result(self) -> dict[str, object]:
        """Return the app state sent to the model."""
        return {
            "state_id": self.state_id,
            "app": {"id": self.app_id, "name": self.app_name},
            "window": self.window.to_result(),
            "elements": [element.to_result() for element in self.elements],
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class DesktopApp:
    """One locally allowed application and its current running state."""

    app_id: str
    name: str
    running: bool

    def to_result(self) -> dict[str, object]:
        """Return app metadata without exposing disallowed applications."""
        return {"id": self.app_id, "name": self.name, "running": self.running}


class AccessibilityBackend(Protocol):
    """Platform accessibility operations used by the hybrid provider."""

    def availability(self) -> dict[str, object]:
        """Describe the active semantic backend."""
        ...

    def list_apps(self) -> list[DesktopApp]:
        """List only applications named in the local allowlist."""
        ...

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Capture and cache one fresh semantic state."""
        ...

    def prepare_capture(self, app_id: str, state_id: str) -> AccessibilityState:
        """Revalidate state and foreground its app immediately before capture."""
        ...

    def prepare_fallback(self, app_id: str, state_id: str) -> AccessibilityState:
        """Validate a fresh state and focus its app before coordinate or keyboard fallback."""
        ...

    def element_for_action(
        self,
        app_id: str,
        state_id: str,
        element_index: int,
    ) -> AccessibilityElement:
        """Validate a fresh state and return one indexed element."""
        ...

    def click_element(self, app_id: str, state_id: str, element_index: int) -> None:
        """Invoke the semantic press action on one element."""
        ...

    def set_value(self, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Set one accessibility value after verifying it is writable."""
        ...

    def perform_action(self, app_id: str, state_id: str, element_index: int, action: str) -> None:
        """Invoke one action explicitly advertised by the element."""
        ...


@dataclass(slots=True)
class _StoredMacState:
    public: AccessibilityState
    fingerprint: str
    references: tuple[object, ...]
    application: _RunningApplication | None
    window_reference: object | None


class MacAccessibilityBackend:
    """macOS AXUIElement backend with exact app allowlisting and stale-state checks."""

    def __init__(self, allowed_app_ids: frozenset[str], screen_size: Callable[[], tuple[int, int]]) -> None:
        import AppKit  # noqa: PLC0415
        import ApplicationServices  # noqa: PLC0415

        self._allowed_app_ids = allowed_app_ids
        self._screen_size = screen_size
        self._appkit: Any = AppKit
        self._services: Any = ApplicationServices
        self._workspace: _Workspace = AppKit.NSWorkspace.sharedWorkspace()  # ty: ignore[unresolved-attribute]
        self._states: dict[str, _StoredMacState] = {}

    def availability(self) -> dict[str, object]:
        """Report whether macOS granted the process Accessibility permission."""
        return {
            "available": bool(self._services.AXIsProcessTrusted()),
            "backend": "macos_ax",
        }

    def list_apps(self) -> list[DesktopApp]:
        """List configured bundle identifiers without revealing other running apps."""
        running = {
            str(application.bundleIdentifier()): str(application.localizedName() or application.bundleIdentifier())
            for application in self._workspace.runningApplications()
            if application.bundleIdentifier() is not None
        }
        apps: list[DesktopApp] = []
        for app_id in sorted(self._allowed_app_ids):
            if app_id == PRIMARY_SCREEN_APP_ID:
                apps.append(DesktopApp(app_id, "Primary Screen", True))
                continue
            apps.append(DesktopApp(app_id, running.get(app_id, app_id), app_id in running))
        return apps

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Capture one allowed app and invalidate its previous element indexes."""
        self._require_allowed(app_id)
        if app_id == PRIMARY_SCREEN_APP_ID:
            state = _primary_screen_state(self._screen_size(), state_id=uuid4().hex)
            self._states[app_id] = _StoredMacState(state, _state_fingerprint(state), (), None, None)
            return state
        stored = self._collect_app_state(app_id, state_id=uuid4().hex)
        self._states[app_id] = stored
        return stored.public

    def prepare_fallback(self, app_id: str, state_id: str) -> AccessibilityState:
        """Revalidate exact state before focusing an allowed target."""
        return self.prepare_capture(app_id, state_id)

    def prepare_capture(self, app_id: str, state_id: str) -> AccessibilityState:
        """Revalidate exact state before and after foregrounding the allowed target."""
        stored = self._fresh_state(app_id, state_id)
        self._activate(stored.application)
        if stored.application is None:
            return stored.public
        return self._fresh_state(app_id, state_id).public

    def element_for_action(
        self,
        app_id: str,
        state_id: str,
        element_index: int,
    ) -> AccessibilityElement:
        """Return one current element after structural revalidation."""
        current = self._current_action_state(app_id, state_id, element_index)
        self._require_element_enabled(current, element_index)
        self._activate(current.application)
        current = self._current_action_state(app_id, state_id, element_index)
        self._require_element_enabled(current, element_index)
        element = current.public.elements[element_index]
        if element.bounds is not None and not _rect_center_inside(element.bounds, current.public.window):
            msg = f"Accessibility element {element_index} is outside the allowed app window."
            raise AccessibilityError(msg)
        return element

    def click_element(self, app_id: str, state_id: str, element_index: int) -> None:
        """Perform AXPress only when the fresh element advertises it."""
        self.perform_action(app_id, state_id, element_index, self._services.kAXPressAction)

    def set_value(self, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Set a writable AXValue after refreshing the element reference."""
        current = self._current_action_state(app_id, state_id, element_index)
        self._writable_value_target(current, element_index)
        self._activate(current.application)
        current = self._current_action_state(app_id, state_id, element_index)
        reference = self._writable_value_target(current, element_index)
        error = self._services.AXUIElementSetAttributeValue(
            reference,
            self._services.kAXValueAttribute,
            value,
        )
        if error != self._services.kAXErrorSuccess:
            msg = f"macOS accessibility value action returned error {error}; the outcome is unknown."
            raise AccessibilityActionOutcomeUnknownError(msg)

    def perform_action(self, app_id: str, state_id: str, element_index: int, action: str) -> None:
        """Perform only an action present in the fresh element's advertised action list."""
        current = self._current_action_state(app_id, state_id, element_index)
        self._advertised_action_target(current, element_index, action)
        self._activate(current.application)
        current = self._current_action_state(app_id, state_id, element_index)
        reference = self._advertised_action_target(current, element_index, action)
        error = self._services.AXUIElementPerformAction(reference, action)
        if error != self._services.kAXErrorSuccess:
            msg = f"macOS accessibility action returned error {error}; the outcome is unknown."
            raise AccessibilityActionOutcomeUnknownError(msg)

    def _fresh_state(self, app_id: str, state_id: str) -> _StoredMacState:
        self._require_allowed(app_id)
        stored = self._states.get(app_id)
        if stored is None or stored.public.state_id != state_id:
            msg = "Accessibility state is stale; request get_app_state again before acting."
            raise AccessibilityError(msg)
        if app_id == PRIMARY_SCREEN_APP_ID:
            current = _primary_screen_state(self._screen_size(), state_id=state_id)
            if _state_fingerprint(current) != stored.fingerprint:
                self._states.pop(app_id, None)
                msg = "Accessibility state changed; request get_app_state again before acting."
                raise AccessibilityError(msg)
            return stored
        application = stored.application
        if application is None or stored.window_reference is None:
            self._states.pop(app_id, None)
            msg = "Accessibility target is stale; request get_app_state again before acting."
            raise AccessibilityError(msg)
        try:
            current = self._collect_app_state(
                app_id,
                state_id=state_id,
                expected_pid=application.processIdentifier(),
                expected_window=stored.window_reference,
            )
        except AccessibilityError:
            self._states.pop(app_id, None)
            raise
        if current.fingerprint != stored.fingerprint:
            self._states.pop(app_id, None)
            msg = "Accessibility state changed; request get_app_state again before acting."
            raise AccessibilityError(msg)
        return current

    def _current_action_state(
        self,
        app_id: str,
        state_id: str,
        element_index: int,
    ) -> _StoredMacState:
        observed = self._states.get(app_id)
        if observed is None or observed.public.state_id != state_id:
            msg = "Accessibility state is stale; request get_app_state again before acting."
            raise AccessibilityError(msg)
        current = self._fresh_state(app_id, state_id)
        self._validate_element_index(observed, element_index)
        self._validate_element_index(current, element_index)
        if observed.public.elements[element_index] != current.public.elements[element_index]:
            self._states.pop(app_id, None)
            msg = "Accessibility target element changed; request get_app_state again before acting."
            raise AccessibilityError(msg)
        return current

    def _collect_app_state(
        self,
        app_id: str,
        *,
        state_id: str,
        expected_pid: int | None = None,
        expected_window: object | None = None,
    ) -> _StoredMacState:
        if not self._services.AXIsProcessTrusted():
            msg = "macOS Accessibility permission is required for semantic desktop state."
            raise AccessibilityError(msg)
        application = self._running_application(app_id, expected_pid=expected_pid)
        app_element = self._services.AXUIElementCreateApplication(application.processIdentifier())
        window_element = self._window_element(app_element)
        if expected_window is not None and window_element != expected_window:
            msg = "Accessibility target window changed; request get_app_state again before acting."
            raise AccessibilityError(msg)
        window = self._element_rect(window_element)
        if window is None:
            msg = f"Allowed application {app_id!r} has no readable window bounds."
            raise AccessibilityError(msg)
        elements, references, truncated = self._flatten_tree(window_element)
        state = AccessibilityState(
            state_id=state_id,
            app_id=app_id,
            app_name=str(application.localizedName() or app_id),
            window=window,
            elements=elements,
            truncated=truncated,
        )
        return _StoredMacState(state, _state_fingerprint(state), references, application, window_element)

    def _flatten_tree(
        self,
        root: object,
    ) -> tuple[tuple[AccessibilityElement, ...], tuple[object, ...], bool]:
        queue: deque[tuple[object, int, int | None]] = deque([(root, 0, None)])
        seen: set[object] = set()
        elements: list[AccessibilityElement] = []
        references: list[object] = []
        visited = 0
        truncated = False
        while queue and visited < _MAX_VISITED_ELEMENTS:
            reference, depth, parent_index = queue.popleft()
            if reference in seen:
                continue
            seen.add(reference)
            visited += 1
            role = _bounded_text(self._copy_attribute(reference, self._services.kAXRoleAttribute)) or "AXUnknown"
            actions = self._action_names(reference)
            name = self._element_name(reference)
            secure = role == "AXSecureTextField"
            value = (
                None if secure else _bounded_value(self._copy_attribute(reference, self._services.kAXValueAttribute))
            )
            include = (
                depth == 0 or role not in _CONTAINER_ROLES or name is not None or value is not None or bool(actions)
            )
            current_parent = parent_index
            if include:
                if len(elements) >= MAX_ACCESSIBILITY_ELEMENTS:
                    truncated = True
                    break
                index = len(elements)
                settable = not secure and self._attribute_settable(reference, self._services.kAXValueAttribute)
                enabled_value = self._copy_attribute(reference, self._services.kAXEnabledAttribute)
                enabled = enabled_value if isinstance(enabled_value, bool) else None
                elements.append(
                    AccessibilityElement(
                        index=index,
                        depth=depth,
                        parent_index=parent_index,
                        role=role,
                        name=name,
                        value=value,
                        enabled=enabled,
                        settable=settable,
                        bounds=self._element_rect(reference),
                        actions=actions,
                    ),
                )
                references.append(reference)
                current_parent = index
            if depth >= _MAX_ACCESSIBILITY_DEPTH:
                if self._children(reference):
                    truncated = True
                continue
            for child in self._children(reference):
                queue.append((child, depth + 1, current_parent))
        if queue or visited >= _MAX_VISITED_ELEMENTS:
            truncated = True
        return tuple(elements), tuple(references), truncated

    def _running_application(self, app_id: str, *, expected_pid: int | None = None) -> _RunningApplication:
        matches = [
            application
            for application in self._workspace.runningApplications()
            if application.bundleIdentifier() == app_id
        ]
        if not matches:
            msg = f"Allowed application {app_id!r} is not running."
            raise AccessibilityError(msg)
        if expected_pid is not None:
            exact = [application for application in matches if application.processIdentifier() == expected_pid]
            if not exact:
                msg = "Accessibility target app process changed; request get_app_state again before acting."
                raise AccessibilityError(msg)
            return exact[0]
        active = [application for application in matches if application.isActive()]
        return active[0] if active else matches[0]

    def _window_element(self, app_element: object) -> object:
        focused = self._copy_attribute(app_element, self._services.kAXFocusedWindowAttribute)
        if focused is not None:
            window = focused
        else:
            windows = self._copy_attribute(app_element, self._services.kAXWindowsAttribute)
            if not isinstance(windows, Sequence) or isinstance(windows, str) or not windows:
                msg = "Allowed application has no accessible window."
                raise AccessibilityError(msg)
            window = windows[0]
        if self._copy_attribute(window, self._services.kAXMinimizedAttribute) is True:
            msg = "Allowed application window is minimized; restore it locally before requesting state."
            raise AccessibilityError(msg)
        return window

    def _element_name(self, reference: object) -> str | None:
        for attribute in (
            self._services.kAXTitleAttribute,
            self._services.kAXDescriptionAttribute,
            self._services.kAXHelpAttribute,
            self._services.kAXIdentifierAttribute,
        ):
            value = _bounded_text(self._copy_attribute(reference, attribute))
            if value is not None:
                return value
        return None

    def _element_rect(self, reference: object) -> DesktopRect | None:
        position = self._copy_attribute(reference, self._services.kAXPositionAttribute)
        size = self._copy_attribute(reference, self._services.kAXSizeAttribute)
        if position is None or size is None:
            return None
        position_ok, point = self._services.AXValueGetValue(
            position,
            self._services.kAXValueCGPointType,
            None,
        )
        size_ok, dimensions = self._services.AXValueGetValue(
            size,
            self._services.kAXValueCGSizeType,
            None,
        )
        if not position_ok or not size_ok or point is None or dimensions is None:
            return None
        width = round(dimensions.width)
        height = round(dimensions.height)
        if width <= 0 or height <= 0:
            return None
        return DesktopRect(round(point.x), round(point.y), width, height)

    def _children(self, reference: object) -> tuple[object, ...]:
        children = self._copy_attribute(reference, self._services.kAXChildrenAttribute)
        if children is None:
            children = self._copy_attribute(reference, "AXChildrenInNavigationOrder")
        if not isinstance(children, Sequence) or isinstance(children, str):
            return ()
        return tuple(children)

    def _action_names(self, reference: object) -> tuple[str, ...]:
        error, actions = self._services.AXUIElementCopyActionNames(reference, None)
        if error != self._services.kAXErrorSuccess or not isinstance(actions, Sequence) or isinstance(actions, str):
            return ()
        names = [
            name
            for action in actions
            if (name := _bounded_text(action)) is not None and name.startswith("AX") and name.isalnum()
        ]
        return tuple(sorted(names))[:8]

    def _attribute_settable(self, reference: object, attribute: str) -> bool:
        error, settable = self._services.AXUIElementIsAttributeSettable(reference, attribute, None)
        return error == self._services.kAXErrorSuccess and settable is True

    def _copy_attribute(self, reference: object, attribute: str) -> object | None:
        error, value = self._services.AXUIElementCopyAttributeValue(reference, attribute, None)
        return value if error == self._services.kAXErrorSuccess else None

    def _activate(self, application: _RunningApplication | None) -> None:
        if application is None:
            return
        activated = application.activateWithOptions_(self._appkit.NSApplicationActivateIgnoringOtherApps)
        if not activated:
            msg = "Allowed application could not be focused before input."
            raise AccessibilityError(msg)

    @staticmethod
    def _validate_element_index(stored: _StoredMacState, element_index: int) -> object:
        if not 0 <= element_index < len(stored.references):
            msg = f"Accessibility element index {element_index} is outside the current state."
            raise AccessibilityError(msg)
        return stored.references[element_index]

    @staticmethod
    def _require_element_enabled(stored: _StoredMacState, element_index: int) -> None:
        MacAccessibilityBackend._validate_element_index(stored, element_index)
        if stored.public.elements[element_index].enabled is False:
            msg = f"Accessibility element {element_index} is disabled."
            raise AccessibilityError(msg)

    def _writable_value_target(self, stored: _StoredMacState, element_index: int) -> object:
        reference = self._validate_element_index(stored, element_index)
        self._require_element_enabled(stored, element_index)
        element = stored.public.elements[element_index]
        if element.role == "AXSecureTextField":
            msg = "Secure text fields cannot be read or changed through the desktop bridge."
            raise AccessibilityError(msg)
        if not element.settable:
            msg = f"Accessibility element {element_index} does not expose a writable value."
            raise AccessibilityError(msg)
        return reference

    def _advertised_action_target(
        self,
        stored: _StoredMacState,
        element_index: int,
        action: str,
    ) -> object:
        reference = self._validate_element_index(stored, element_index)
        self._require_element_enabled(stored, element_index)
        if action not in stored.public.elements[element_index].actions:
            msg = f"Accessibility element {element_index} does not advertise action {action!r}."
            raise AccessibilityError(msg)
        return reference

    def _require_allowed(self, app_id: str) -> None:
        if app_id not in self._allowed_app_ids:
            msg = f"Application {app_id!r} is not in the local desktop allowlist."
            raise AccessibilityError(msg)


class ScreenshotOnlyAccessibilityBackend:
    """Portable state binding for explicit primary-screen coordinate fallback."""

    def __init__(self, allowed_app_ids: frozenset[str], screen_size: Callable[[], tuple[int, int]]) -> None:
        self._allowed_app_ids = allowed_app_ids
        self._screen_size = screen_size
        self._state: AccessibilityState | None = None

    def availability(self) -> dict[str, object]:
        """Report that semantic elements are unavailable on this platform."""
        return {"available": False, "backend": "screenshot_only"}

    def list_apps(self) -> list[DesktopApp]:
        """Expose only the explicit primary-screen fallback target."""
        return [
            DesktopApp(
                app_id,
                "Primary Screen" if app_id == PRIMARY_SCREEN_APP_ID else app_id,
                app_id == PRIMARY_SCREEN_APP_ID,
            )
            for app_id in sorted(self._allowed_app_ids)
        ]

    def get_app_state(self, app_id: str) -> AccessibilityState:
        """Create an empty state bound to the current primary-screen geometry."""
        self._require_primary(app_id)
        self._state = _primary_screen_state(self._screen_size(), state_id=uuid4().hex)
        return self._state

    def prepare_fallback(self, app_id: str, state_id: str) -> AccessibilityState:
        """Validate that coordinate fallback still targets the latest screen geometry."""
        return self.prepare_capture(app_id, state_id)

    def prepare_capture(self, app_id: str, state_id: str) -> AccessibilityState:
        """Validate that capture still targets the latest screen geometry."""
        self._require_primary(app_id)
        if self._state is None or self._state.state_id != state_id:
            msg = "Desktop state is stale; request get_app_state again before acting."
            raise AccessibilityError(msg)
        current = _primary_screen_state(self._screen_size(), state_id=state_id)
        if _state_fingerprint(current) != _state_fingerprint(self._state):
            self._state = None
            msg = "Desktop geometry changed; request get_app_state again before acting."
            raise AccessibilityError(msg)
        return self._state

    def element_for_action(
        self,
        app_id: str,
        state_id: str,
        element_index: int,
    ) -> AccessibilityElement:
        """Reject semantic element use when only pixels are available."""
        self.prepare_fallback(app_id, state_id)
        msg = f"Accessibility elements are unavailable; element index {element_index} cannot be used."
        raise AccessibilityError(msg)

    def click_element(self, app_id: str, state_id: str, element_index: int) -> None:
        """Reject semantic clicking on a screenshot-only backend."""
        self.element_for_action(app_id, state_id, element_index)

    def set_value(self, app_id: str, state_id: str, element_index: int, value: str) -> None:
        """Reject semantic value changes on a screenshot-only backend."""
        del value
        self.element_for_action(app_id, state_id, element_index)

    def perform_action(self, app_id: str, state_id: str, element_index: int, action: str) -> None:
        """Reject semantic actions on a screenshot-only backend."""
        del action
        self.element_for_action(app_id, state_id, element_index)

    def _require_primary(self, app_id: str) -> None:
        if app_id not in self._allowed_app_ids:
            msg = f"Application {app_id!r} is not in the local desktop allowlist."
            raise AccessibilityError(msg)
        if app_id != PRIMARY_SCREEN_APP_ID:
            msg = "Semantic application accessibility is currently available only on macOS."
            raise AccessibilityError(msg)


def create_accessibility_backend(
    allowed_app_ids: frozenset[str],
    screen_size: Callable[[], tuple[int, int]],
) -> AccessibilityBackend:
    """Create the native semantic backend or a state-bound pixel fallback."""
    if sys.platform == "darwin":
        try:
            return MacAccessibilityBackend(allowed_app_ids, screen_size)
        except ImportError:
            return ScreenshotOnlyAccessibilityBackend(allowed_app_ids, screen_size)
    return ScreenshotOnlyAccessibilityBackend(allowed_app_ids, screen_size)


def _primary_screen_state(screen_size: tuple[int, int], *, state_id: str) -> AccessibilityState:
    width, height = screen_size
    return AccessibilityState(
        state_id=state_id,
        app_id=PRIMARY_SCREEN_APP_ID,
        app_name="Primary Screen",
        window=DesktopRect(0, 0, width, height),
        elements=(),
        truncated=False,
    )


def _state_fingerprint(state: AccessibilityState) -> str:
    structural = {
        "app_id": state.app_id,
        "window": state.window.to_result(),
        "elements": [
            {
                "depth": element.depth,
                "parent_index": element.parent_index,
                "role": element.role,
                "name": element.name,
                "value": element.value,
                "enabled": element.enabled,
                "settable": element.settable,
                "bounds": element.bounds.to_result() if element.bounds is not None else None,
                "actions": element.actions,
            }
            for element in state.elements
        ],
        "truncated": state.truncated,
    }
    encoded = json.dumps(structural, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _bounded_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:_MAX_TEXT_LENGTH]


def _bounded_value(value: object) -> str | int | float | bool | None:
    if isinstance(value, str):
        return value[:_MAX_TEXT_LENGTH]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _rect_center_inside(rect: DesktopRect, container: DesktopRect) -> bool:
    center_x = rect.x + rect.width // 2
    center_y = rect.y + rect.height // 2
    return (
        container.x <= center_x < container.x + container.width
        and container.y <= center_y < container.y + container.height
    )


__all__ = [
    "MAX_ACCESSIBILITY_ELEMENTS",
    "PRIMARY_SCREEN_APP_ID",
    "AccessibilityActionOutcomeUnknownError",
    "AccessibilityBackend",
    "AccessibilityElement",
    "AccessibilityError",
    "AccessibilityState",
    "DesktopApp",
    "DesktopRect",
    "MacAccessibilityBackend",
    "ScreenshotOnlyAccessibilityBackend",
    "create_accessibility_backend",
]
