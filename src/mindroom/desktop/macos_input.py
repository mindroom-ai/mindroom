"""Bounded Quartz global HID and SkyLight background PID-routed input on macOS."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.desktop import macos_skylight

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.desktop.accessibility import DesktopRect

_CLICK_GROUP_IDS = itertools.count(1)
_MAX_BACKGROUND_UTF16_UNITS = 16
_CMD_FLAG_MASK = 0x00100000
_CTRL_FLAG_MASK = 0x00040000
_SHIFT_FLAG_MASK = 0x00020000

_MACOS_KEYCODES: dict[str, int] = {
    "a": 0,
    "f": 3,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "return": 36,
    "enter": 36,
    "tab": 48,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "home": 115,
    "pageup": 116,
    "delete": 117,
    "end": 119,
    "pagedown": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}


@dataclass(frozen=True, slots=True)
class _PidTarget:
    pid: int
    window_id: int | None
    window_rect: DesktopRect | None
    group: int


def _quartz() -> Any:  # noqa: ANN401 - Optional PyObjC exposes a dynamic framework module.
    import Quartz  # noqa: PLC0415

    return Quartz


def _button_spec(api: Any, button: str) -> tuple[int, int, int, int]:  # noqa: ANN401
    return {
        "left": (api.kCGEventLeftMouseDown, api.kCGEventLeftMouseUp, api.kCGMouseButtonLeft, 0),
        "right": (api.kCGEventRightMouseDown, api.kCGEventRightMouseUp, api.kCGMouseButtonRight, 1),
        "middle": (api.kCGEventOtherMouseDown, api.kCGEventOtherMouseUp, api.kCGMouseButtonCenter, 2),
    }[button]


def _create_mouse_event(api: Any, kind: int, point: tuple[int, int], button: int) -> object:  # noqa: ANN401
    event = api.CGEventCreateMouseEvent(None, kind, point, button)
    if event is None:
        msg = "macOS could not create a pointer event; the action outcome may be unknown."
        raise RuntimeError(msg)
    return event


def _mouse(kind: int, point: tuple[int, int], button: int, *, count: int = 1) -> None:
    api = _quartz()
    event = _create_mouse_event(api, kind, point, button)
    api.CGEventSetIntegerValueField(event, api.kCGMouseEventClickState, count)
    api.CGEventPost(api.kCGHIDEventTap, event)


def _run_check(check: Callable[[tuple[int, int]], None] | None, point: tuple[int, int]) -> None:
    if check is not None:
        check(point)


def _set_int_field(api: Any, event: object, field: int, value: int) -> None:  # noqa: ANN401
    if not macos_skylight.set_integer_field(event, field, value):
        api.CGEventSetIntegerValueField(event, field, value)


def _window_location(
    point: tuple[int, int],
    window_rect: DesktopRect | None,
    *,
    preserve_raw: bool = False,
) -> tuple[float, float]:
    if not preserve_raw and window_rect is not None and macos_skylight.window_location_is_window_local():
        return float(point[0] - window_rect.x), float(point[1] - window_rect.y)
    return float(point[0]), float(point[1])


def _stamp_window_fields(api: Any, event: object, pid: int, window_id: int | None) -> None:  # noqa: ANN401
    _set_int_field(api, event, 40, pid)
    if window_id is not None:
        for field in (51, 91, 92):
            _set_int_field(api, event, field, window_id)


def _deliver_pid_event(api: Any, pid: int, event: object, *, attach_auth_message: bool) -> None:  # noqa: ANN401
    if not macos_skylight.post_to_pid(pid, event, attach_auth_message=attach_auth_message):
        api.CGEventPostToPid(pid, event)


def _post_pid_mouse(
    target: _PidTarget,
    kind: int,
    point: tuple[int, int],
    button: int,
    *,
    phase: int = 3,
    click_state: int = 1,
    button_number: int = 0,
    subtype: int = 3,
    preserve_raw: bool = False,
) -> None:
    api = _quartz()
    event = _create_mouse_event(api, kind, point, button)
    for field, value in ((0, phase), (1, click_state), (3, button_number), (7, subtype), (58, target.group)):
        _set_int_field(api, event, field, value)
    _stamp_window_fields(api, event, target.pid, target.window_id)
    loc_x, loc_y = _window_location(point, target.window_rect, preserve_raw=preserve_raw)
    macos_skylight.set_window_location(event, loc_x, loc_y)
    api.CGEventSetFlags(event, 0)
    _deliver_pid_event(api, target.pid, event, attach_auth_message=False)


def _activate_window(pid: int, window_id: int | None) -> None:
    if window_id is not None:
        macos_skylight.activate_without_raise(pid, window_id)
        time.sleep(0.05)


def _post_pid_move(target: _PidTarget, point: tuple[int, int]) -> None:
    api = _quartz()
    _post_pid_mouse(target, api.kCGEventMouseMoved, point, api.kCGMouseButtonLeft, phase=2, click_state=0)


def _post_left_primer(target: _PidTarget) -> None:
    api = _quartz()
    for kind, phase, delay in ((api.kCGEventLeftMouseDown, 1, 0.001), (api.kCGEventLeftMouseUp, 2, 0.1)):
        _post_pid_mouse(target, kind, (-1, -1), api.kCGMouseButtonLeft, phase=phase, preserve_raw=True)
        time.sleep(delay)


def _post_pid_key(pid: int, keycode: int, *, flags: int = 0, unicode_chunk: str | None = None) -> None:
    api = _quartz()
    cmd_mask = int(getattr(api, "kCGEventFlagMaskCommand", _CMD_FLAG_MASK))
    key_down = api.CGEventCreateKeyboardEvent(None, keycode, True)
    key_up = api.CGEventCreateKeyboardEvent(None, keycode, False)
    if key_down is None or key_up is None:
        msg = "macOS could not create a keyboard event."
        raise RuntimeError(msg)
    api.CGEventSetFlags(key_down, flags)
    api.CGEventSetFlags(key_up, flags & ~cmd_mask)
    if unicode_chunk is not None:
        utf16_length = len(unicode_chunk.encode("utf-16-le")) // 2
        api.CGEventKeyboardSetUnicodeString(key_down, utf16_length, unicode_chunk)
        api.CGEventKeyboardSetUnicodeString(key_up, utf16_length, unicode_chunk)
    for index, event in enumerate((key_down, key_up)):
        if index:
            time.sleep(0.008)
        _deliver_pid_event(api, pid, event, attach_auth_message=True)


def _chunk_background_utf16(text: str) -> list[str]:
    chunks: list[str] = []
    chunk: list[str] = []
    utf16_units = 0
    for ch in text:
        units = 2 if ord(ch) > 0xFFFF else 1
        if chunk and utf16_units + units > _MAX_BACKGROUND_UTF16_UNITS:
            chunks.append("".join(chunk))
            chunk.clear()
            utf16_units = 0
        chunk.append(ch)
        utf16_units += units
    if chunk:
        chunks.append("".join(chunk))
    return chunks


def move(x: int, y: int, *, check: Callable[[tuple[int, int]], None] | None = None) -> None:
    """Move to a global logical point without primary-screen clamping."""
    api = _quartz()
    _run_check(check, (x, y))
    _mouse(api.kCGEventMouseMoved, (x, y), api.kCGMouseButtonLeft)


def click(
    x: int,
    y: int,
    *,
    button: str,
    count: int,
    check: Callable[[tuple[int, int]], None] | None = None,
) -> None:
    """Post matching down/up pairs and explicit double-click event counts on the global HID tap."""
    down, up, mouse_button, _ = _button_spec(_quartz(), button)
    for click_count in range(1, count + 1):
        _run_check(check, (x, y))
        try:
            _mouse(down, (x, y), mouse_button, count=click_count)
            _run_check(check, (x, y))
        finally:
            _mouse(up, (x, y), mouse_button, count=click_count)
        if click_count < count:
            time.sleep(0.1)


def drag(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    duration: float,
    check: Callable[[tuple[int, int]], None] | None = None,
    before_press: Callable[[], object] | None = None,
) -> None:
    """Keep the left button paired through bounded global-HID motion and interruption."""
    api = _quartz()
    move(*start, check=check)
    if before_press is not None:
        before_press()
    point = start
    _run_check(check, start)
    try:
        _mouse(api.kCGEventLeftMouseDown, start, api.kCGMouseButtonLeft)
        for step in range(1, 21):
            target = (
                round(start[0] + (end[0] - start[0]) * step / 20),
                round(start[1] + (end[1] - start[1]) * step / 20),
            )
            _run_check(check, target)
            point = target
            _mouse(api.kCGEventLeftMouseDragged, point, api.kCGMouseButtonLeft)
            time.sleep(duration / 20)
        _run_check(check, point)
    finally:
        _mouse(api.kCGEventLeftMouseUp, point, api.kCGMouseButtonLeft)


def scroll(
    x: int,
    y: int,
    *,
    clicks: int,
    horizontal: bool,
    check: Callable[[tuple[int, int]], None] | None = None,
) -> None:
    """Post a line-based wheel event on the global HID tap."""
    api = _quartz()
    move(x, y, check=check)
    _run_check(check, (x, y))
    event = api.CGEventCreateScrollWheelEvent(
        None,
        api.kCGScrollEventUnitLine,
        2,
        0 if horizontal else clicks,
        clicks if horizontal else 0,
    )
    if event is None:
        msg = "macOS could not create a scroll event."
        raise RuntimeError(msg)
    api.CGEventPost(api.kCGHIDEventTap, event)


def move_background(
    pid: int,
    x: int,
    y: int,
    *,
    window_id: int | None,
    window_rect: DesktopRect | None,
    check: Callable[[tuple[int, int]], None],
) -> None:
    """Deliver a background hover event directly to one app without moving the physical cursor."""
    check((x, y))
    _post_pid_move(_PidTarget(pid, window_id, window_rect, next(_CLICK_GROUP_IDS)), (x, y))


def click_background(
    pid: int,
    x: int,
    y: int,
    *,
    button: str,
    count: int,
    window_id: int | None,
    window_rect: DesktopRect | None,
    check: Callable[[tuple[int, int]], None],
) -> None:
    """Deliver background click/double-click events to one app without raising it or moving the cursor."""
    down_kind, up_kind, mouse_button, button_number = _button_spec(_quartz(), button)
    target = _PidTarget(pid, window_id, window_rect, next(_CLICK_GROUP_IDS))
    check((x, y))
    _activate_window(pid, window_id)
    _post_pid_move(target, (x, y))
    time.sleep(0.015)
    if button == "left":
        _post_left_primer(target)
    for click_count in range(1, count + 1):
        check((x, y))
        pressed = False
        try:
            _post_pid_mouse(
                target,
                down_kind,
                (x, y),
                mouse_button,
                click_state=click_count,
                button_number=button_number,
            )
            pressed = True
            time.sleep(0.001 if button == "left" else 0.025)
        finally:
            if pressed:
                _post_pid_mouse(
                    target,
                    up_kind,
                    (x, y),
                    mouse_button,
                    click_state=click_count,
                    button_number=button_number,
                )
        if click_count < count:
            time.sleep(0.08)


def drag_background(
    pid: int,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    duration: float,
    window_id: int | None,
    window_rect: DesktopRect | None,
    check: Callable[[tuple[int, int]], None],
    before_press: Callable[[], object] | None = None,
) -> None:
    """Deliver a background left-button drag directly to one app and always release on failure."""
    api = _quartz()
    target = _PidTarget(pid, window_id, window_rect, next(_CLICK_GROUP_IDS))
    check(start)
    _activate_window(pid, window_id)
    _post_pid_move(target, start)
    time.sleep(0.015)
    if before_press is not None:
        before_press()
    check(start)
    _post_left_primer(target)
    point, pressed = start, False
    try:
        _post_pid_mouse(target, api.kCGEventLeftMouseDown, start, api.kCGMouseButtonLeft, subtype=0)
        pressed = True
        for step in range(1, 21):
            next_point = (
                round(start[0] + (end[0] - start[0]) * step / 20),
                round(start[1] + (end[1] - start[1]) * step / 20),
            )
            check(next_point)
            point = next_point
            _post_pid_mouse(target, api.kCGEventLeftMouseDragged, point, api.kCGMouseButtonLeft, subtype=0)
            time.sleep(duration / 20)
        check(point)
    finally:
        if pressed:
            _post_pid_mouse(target, api.kCGEventLeftMouseUp, point, api.kCGMouseButtonLeft, subtype=0)


def scroll_background(
    pid: int,
    x: int,
    y: int,
    *,
    clicks: int,
    horizontal: bool,
    window_id: int | None,
    window_rect: DesktopRect | None,
    check: Callable[[tuple[int, int]], None],
) -> None:
    """Deliver a line-based wheel event directly to one app without moving the physical cursor."""
    api = _quartz()
    check((x, y))
    _post_pid_move(_PidTarget(pid, window_id, window_rect, next(_CLICK_GROUP_IDS)), (x, y))
    time.sleep(0.015)
    check((x, y))
    event = api.CGEventCreateScrollWheelEvent(
        None,
        api.kCGScrollEventUnitLine,
        2,
        0 if horizontal else clicks,
        clicks if horizontal else 0,
    )
    if event is None:
        msg = "macOS could not create a scroll event."
        raise RuntimeError(msg)
    api.CGEventSetLocation(event, (x, y))
    macos_skylight.set_window_location(event, *_window_location((x, y), window_rect))
    _stamp_window_fields(api, event, pid, window_id)
    api.CGEventSetFlags(event, 0)
    _deliver_pid_event(api, pid, event, attach_auth_message=False)


def type_text_background(
    pid: int,
    window_id: int | None,
    text: str,
    *,
    before_chunk: Callable[[], None],
) -> None:
    """Post bounded UTF-16 Unicode chunks directly to one background PID on keycode 0."""
    activated = False
    for chunk in _chunk_background_utf16(text):
        before_chunk()
        if not activated:
            _activate_window(pid, window_id)
            activated = True
        _post_pid_key(pid, 0, unicode_chunk=chunk)


def keypress_background(pid: int, window_id: int | None, keys: Sequence[str]) -> None:
    """Post one normalized macOS key chord directly to one background PID."""
    api = _quartz()
    modifier_flags = {
        "command": int(getattr(api, "kCGEventFlagMaskCommand", _CMD_FLAG_MASK)),
        "ctrl": int(getattr(api, "kCGEventFlagMaskControl", _CTRL_FLAG_MASK)),
        "shift": int(getattr(api, "kCGEventFlagMaskShift", _SHIFT_FLAG_MASK)),
    }
    if not keys or keys[-1] not in _MACOS_KEYCODES:
        msg = f"Unsupported macOS background key chord: {list(keys)!r}."
        raise ValueError(msg)
    flags = 0
    for modifier in keys[:-1]:
        if modifier not in modifier_flags:
            msg = f"Unsupported macOS background modifier: {modifier!r}."
            raise ValueError(msg)
        flags |= modifier_flags[modifier]
    _activate_window(pid, window_id)
    _post_pid_key(pid, _MACOS_KEYCODES[keys[-1]], flags=flags)


__all__ = [
    "click",
    "click_background",
    "drag",
    "drag_background",
    "keypress_background",
    "move",
    "move_background",
    "scroll",
    "scroll_background",
    "type_text_background",
]
