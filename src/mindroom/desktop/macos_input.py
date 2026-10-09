"""Bounded Quartz pointer input in global logical display coordinates."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _quartz() -> Any:  # noqa: ANN401 - Optional PyObjC exposes a dynamic framework module.
    import Quartz  # noqa: PLC0415

    return Quartz


def _mouse(kind: int, point: tuple[int, int], button: int, *, count: int = 1) -> None:
    api = _quartz()
    event = api.CGEventCreateMouseEvent(None, kind, point, button)
    if event is None:
        msg = "macOS could not create a pointer event; the action outcome may be unknown."
        raise RuntimeError(msg)
    api.CGEventSetIntegerValueField(event, api.kCGMouseEventClickState, count)
    api.CGEventPost(api.kCGHIDEventTap, event)


def window_owner_at(point: tuple[int, int]) -> int | None:
    """Return the process owning the frontmost visible window at one global logical point."""
    api = _quartz()
    windows = api.CGWindowListCopyWindowInfo(api.kCGWindowListOptionOnScreenOnly, api.kCGNullWindowID) or ()
    # Quartz lists on-screen windows front to back; negative layers are the desktop and its icons.
    for window in windows:
        bounds = window.get(api.kCGWindowBounds)
        if window.get(api.kCGWindowLayer, 0) < 0 or window.get(api.kCGWindowAlpha, 1) <= 0 or bounds is None:
            continue
        if (
            bounds["X"] <= point[0] < bounds["X"] + bounds["Width"]
            and bounds["Y"] <= point[1] < bounds["Y"] + bounds["Height"]
        ):
            return int(window[api.kCGWindowOwnerPID])
    return None


def move(x: int, y: int, *, check: Callable[[tuple[int, int]], None]) -> None:
    """Move to a verified global logical point without primary-screen clamping."""
    api = _quartz()
    check((x, y))
    _mouse(api.kCGEventMouseMoved, (x, y), api.kCGMouseButtonLeft)


def click(x: int, y: int, *, button: str, count: int, check: Callable[[tuple[int, int]], None]) -> None:
    """Post matching down/up pairs and explicit double-click event counts, checking the point before each."""
    api = _quartz()
    buttons = {
        "left": (api.kCGEventLeftMouseDown, api.kCGEventLeftMouseUp, api.kCGMouseButtonLeft),
        "right": (api.kCGEventRightMouseDown, api.kCGEventRightMouseUp, api.kCGMouseButtonRight),
        "middle": (api.kCGEventOtherMouseDown, api.kCGEventOtherMouseUp, api.kCGMouseButtonCenter),
    }
    down, up, mouse_button = buttons[button]
    for click_count in range(1, count + 1):
        check((x, y))
        try:
            _mouse(down, (x, y), mouse_button, count=click_count)
            check((x, y))
        finally:
            _mouse(up, (x, y), mouse_button, count=click_count)
        if click_count < count:
            time.sleep(0.1)


def drag(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    duration: float,
    check: Callable[[tuple[int, int]], None],
    before_press: Callable[[], object] | None = None,
) -> None:
    """Keep the left button paired through bounded motion and interruption, checking each point before it posts."""
    api = _quartz()
    move(*start, check=check)
    if before_press is not None:
        before_press()
    point = start
    check(start)
    try:
        _mouse(api.kCGEventLeftMouseDown, start, api.kCGMouseButtonLeft)
        for step in range(1, 21):
            target = (
                round(start[0] + (end[0] - start[0]) * step / 20),
                round(start[1] + (end[1] - start[1]) * step / 20),
            )
            check(target)
            point = target
            _mouse(api.kCGEventLeftMouseDragged, point, api.kCGMouseButtonLeft)
            time.sleep(duration / 20)
        check(point)
    finally:
        _mouse(api.kCGEventLeftMouseUp, point, api.kCGMouseButtonLeft)


def scroll(x: int, y: int, *, clicks: int, horizontal: bool, check: Callable[[tuple[int, int]], None]) -> None:
    """Post a line-based wheel event at an explicitly verified app point."""
    api = _quartz()
    move(x, y, check=check)
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
    api.CGEventPost(api.kCGHIDEventTap, event)


__all__ = ["click", "drag", "move", "scroll", "window_owner_at"]
