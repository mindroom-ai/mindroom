"""Quartz pointer events preserve negative global logical coordinates."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from mindroom.desktop.macos_input import click, drag, scroll


@pytest.fixture
def quartz(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace only the external Quartz event emitter."""
    posted = []
    api = SimpleNamespace(
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGEventLeftMouseDragged=3,
        kCGEventRightMouseDown=4,
        kCGEventRightMouseUp=5,
        kCGEventOtherMouseDown=6,
        kCGEventOtherMouseUp=7,
        kCGEventMouseMoved=8,
        kCGMouseButtonLeft=0,
        kCGMouseButtonRight=1,
        kCGMouseButtonCenter=2,
        kCGMouseEventClickState=10,
        kCGHIDEventTap=0,
        kCGScrollEventUnitLine=1,
        CGEventCreateMouseEvent=lambda _source, kind, point, button: {"kind": kind, "point": point, "button": button},
        CGEventSetIntegerValueField=lambda event, field, value: event.update({field: value}),
        CGEventCreateScrollWheelEvent=lambda _source, _unit, _axes, vertical, horizontal: {
            "wheel": (vertical, horizontal),
        },
        CGEventPost=lambda _tap, event: posted.append(dict(event)),
        posted=posted,
    )
    monkeypatch.setitem(sys.modules, "Quartz", api)
    monkeypatch.setattr("mindroom.desktop.macos_input.time.sleep", lambda _delay: None)
    return api


def test_double_click_preserves_negative_points_and_click_count(quartz: SimpleNamespace) -> None:
    """A secondary-display click must not be clamped onto the primary display."""
    click(-1200, 300, button="left", count=2, check=lambda: None)
    assert [event["point"] for event in quartz.posted] == [(-1200, 300)] * 4
    assert [event[10] for event in quartz.posted] == [1, 1, 2, 2]


def test_drag_releases_button_after_emergency_stop(quartz: SimpleNamespace) -> None:
    """A fail-safe during motion must still post the matching mouse-up event."""
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            msg = "stop"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="stop"):
        drag((-1800, 100), (-1200, 400), duration=0.1, check=check)
    assert quartz.posted[0]["kind"] == quartz.kCGEventMouseMoved
    assert quartz.posted[1]["kind"] == quartz.kCGEventLeftMouseDown
    assert quartz.posted[-1]["kind"] == quartz.kCGEventLeftMouseUp
    assert all(event["point"][0] < 0 for event in quartz.posted)


def test_horizontal_wheel_uses_second_axis(quartz: SimpleNamespace) -> None:
    """Horizontal scroll reaches the target location and the correct wheel axis."""
    scroll(-500, 100, clicks=-6, horizontal=True, check=lambda: None)
    assert quartz.posted[0]["point"] == (-500, 100)
    assert quartz.posted[1] == {"wheel": (0, -6)}


def test_drag_revalidates_after_hover_before_press(quartz: SimpleNamespace) -> None:
    """Moving to the start can reveal a menu; reject changed state before pressing."""

    def reject() -> None:
        assert [event["kind"] for event in quartz.posted] == [quartz.kCGEventMouseMoved]
        msg = "target changed after hover"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="target changed"):
        drag((-1800, 100), (-1200, 400), duration=0.1, check=lambda: None, before_press=reject)
    assert len(quartz.posted) == 1
