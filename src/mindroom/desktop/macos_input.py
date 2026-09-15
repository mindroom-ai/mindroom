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


def move(x: int, y: int) -> None:
    """Move to a verified global logical point without primary-screen clamping."""
    api = _quartz()
    _mouse(api.kCGEventMouseMoved, (x, y), api.kCGMouseButtonLeft)


def click(x: int, y: int, *, button: str, count: int, check: Callable[[], None]) -> None:
    """Post matching down/up pairs and explicit double-click event counts."""
    api = _quartz()
    buttons = {
        "left": (api.kCGEventLeftMouseDown, api.kCGEventLeftMouseUp, api.kCGMouseButtonLeft),
        "right": (api.kCGEventRightMouseDown, api.kCGEventRightMouseUp, api.kCGMouseButtonRight),
        "middle": (api.kCGEventOtherMouseDown, api.kCGEventOtherMouseUp, api.kCGMouseButtonCenter),
    }
    down, up, mouse_button = buttons[button]
    for click_count in range(1, count + 1):
        check()
        try:
            _mouse(down, (x, y), mouse_button, count=click_count)
        finally:
            _mouse(up, (x, y), mouse_button, count=click_count)
        if click_count < count:
            time.sleep(0.1)


def drag(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    duration: float,
    check: Callable[[], None],
    before_press: Callable[[], object] | None = None,
) -> None:
    """Keep the left button paired through bounded motion and fail-safe interruption."""
    api = _quartz()
    check()
    move(*start)
    if before_press is not None:
        before_press()
    point = start
    try:
        _mouse(api.kCGEventLeftMouseDown, start, api.kCGMouseButtonLeft)
        for step in range(1, 21):
            check()
            point = (
                round(start[0] + (end[0] - start[0]) * step / 20),
                round(start[1] + (end[1] - start[1]) * step / 20),
            )
            _mouse(api.kCGEventLeftMouseDragged, point, api.kCGMouseButtonLeft)
            time.sleep(duration / 20)
    finally:
        _mouse(api.kCGEventLeftMouseUp, point, api.kCGMouseButtonLeft)


def scroll(x: int, y: int, *, clicks: int, horizontal: bool, check: Callable[[], None]) -> None:
    """Post a line-based wheel event at an explicitly verified app point."""
    api = _quartz()
    check()
    move(x, y)
    check()
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


__all__ = ["click", "drag", "move", "scroll"]
