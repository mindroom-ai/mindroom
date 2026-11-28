"""Quartz global HID and SkyLight background PID-routed input invariants."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from mindroom.desktop.accessibility import DesktopRect
from mindroom.desktop.macos_input import (
    click,
    click_background,
    drag,
    drag_background,
    keypress_background,
    move_background,
    scroll,
    scroll_background,
    type_text_background,
)

WINDOW_RECT = DesktopRect(100, 200, 800, 600)


@pytest.fixture
def quartz(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace only the external Quartz event emitter."""
    posted: list[dict[str, object]] = []
    pid_posted: list[tuple[int, dict[str, object]]] = []

    def set_field(event: dict[str, object], field: int, value: int) -> None:
        event.setdefault("fields", {})[field] = value  # type: ignore[union-attr]
        event[field] = value

    def set_flags(event: dict[str, object], flags: int) -> None:
        event["flags"] = flags

    def set_location(event: dict[str, object], point: tuple[int, int]) -> None:
        event["point"] = point

    def set_unicode(event: dict[str, object], length: int, text: str) -> None:
        event["unicode"] = (length, text)

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
        kCGEventFlagMaskCommand=0x00100000,
        kCGEventFlagMaskControl=0x00040000,
        kCGEventFlagMaskShift=0x00020000,
        CGEventCreateMouseEvent=lambda _source, kind, point, button: {"kind": kind, "point": point, "button": button},
        CGEventCreateKeyboardEvent=lambda _source, keycode, down: {"keycode": keycode, "down": down},
        CGEventSetIntegerValueField=set_field,
        CGEventSetFlags=set_flags,
        CGEventSetLocation=set_location,
        CGEventKeyboardSetUnicodeString=set_unicode,
        CGEventCreateScrollWheelEvent=lambda _source, _unit, _axes, vertical, horizontal: {
            "wheel": (vertical, horizontal),
        },
        CGEventPost=lambda _tap, event: posted.append(dict(event)),
        CGEventPostToPid=lambda pid, event: pid_posted.append((pid, dict(event))),
        posted=posted,
        pid_posted=pid_posted,
    )
    monkeypatch.setitem(sys.modules, "Quartz", api)
    monkeypatch.setattr("mindroom.desktop.macos_input.time.sleep", lambda _delay: None)
    return api


@pytest.fixture
def skylight(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Capture SkyLight background routing calls cleanly without private frameworks."""
    state = SimpleNamespace(
        window_local=False,
        post_returns=True,
        activations=[],
        posts=[],
    )

    def set_int(event: dict[str, object], field: int, value: int) -> bool:
        event.setdefault("sl_fields", {})[field] = value  # type: ignore[union-attr]
        return True

    def set_loc(event: dict[str, object], x: float, y: float) -> bool:
        event["win_loc"] = (x, y)
        return True

    def post(pid: int, event: dict[str, object], *, attach_auth_message: bool = False) -> bool:
        if not state.post_returns:
            return False
        state.posts.append((pid, dict(event), attach_auth_message))
        return True

    monkeypatch.setattr(
        "mindroom.desktop.macos_input.macos_skylight.window_location_is_window_local",
        lambda: state.window_local,
    )
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.set_integer_field", set_int)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.set_window_location", set_loc)
    monkeypatch.setattr("mindroom.desktop.macos_input.macos_skylight.post_to_pid", post)
    monkeypatch.setattr(
        "mindroom.desktop.macos_input.macos_skylight.activate_without_raise",
        lambda pid, wid: state.activations.append((pid, wid)) or True,
    )
    return state


def test_double_click_preserves_negative_points_and_click_count(quartz: SimpleNamespace) -> None:
    """A secondary-display click must not be clamped onto the primary display."""
    click(-1200, 300, button="left", count=2, check=lambda _point: None)
    assert [event["point"] for event in quartz.posted] == [(-1200, 300)] * 4
    assert [event[10] for event in quartz.posted] == [1, 1, 2, 2]


def test_drag_releases_button_after_failed_pointer_check(quartz: SimpleNamespace) -> None:
    """A pointer check failing during motion must still post the matching mouse-up event."""
    checks = 0

    def check(_point: tuple[int, int]) -> None:
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
    scroll(-500, 100, clicks=-6, horizontal=True, check=lambda _point: None)
    assert quartz.posted[0]["point"] == (-500, 100)
    assert quartz.posted[1] == {"wheel": (0, -6)}


def test_drag_revalidates_after_hover_before_press(quartz: SimpleNamespace) -> None:
    """Moving to the start can reveal a menu; reject changed state before pressing."""

    def reject() -> None:
        assert [event["kind"] for event in quartz.posted] == [quartz.kCGEventMouseMoved]
        msg = "target changed after hover"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="target changed"):
        drag((-1800, 100), (-1200, 400), duration=0.1, check=lambda _point: None, before_press=reject)
    assert len(quartz.posted) == 1


def test_drag_start_covered_before_press_posts_no_unpaired_release(quartz: SimpleNamespace) -> None:
    """A start point that becomes covered after the hover gets neither a press nor a release."""
    checked: list[tuple[int, int]] = []

    def check(point: tuple[int, int]) -> None:
        checked.append(point)
        if len(checked) == 2:
            msg = "covered"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="covered"):
        drag((-1800, 100), (-1200, 400), duration=0.1, check=check)
    assert checked == [(-1800, 100), (-1800, 100)]
    assert [event["kind"] for event in quartz.posted] == [quartz.kCGEventMouseMoved]


@pytest.mark.parametrize("window_local", [False, True])
def test_click_background_stamps_fields_primes_left_and_handles_macos27_coords(
    quartz: SimpleNamespace,
    skylight: SimpleNamespace,
    window_local: bool,
) -> None:
    """Background left double-click activates without raise, primes at (-1, -1), zeroes flags, and skips auth."""
    skylight.window_local = window_local
    click_background(42, 250, 350, button="left", count=2, window_id=99, window_rect=WINDOW_RECT, check=lambda _p: None)

    assert quartz.posted == []  # Never touches global HID tap.
    assert skylight.activations == [(42, 99)]
    assert [auth for _, _, auth in skylight.posts] == [False] * 7
    events = [ev for _, ev, _ in skylight.posts]
    assert [ev["kind"] for ev in events] == [
        quartz.kCGEventMouseMoved,
        quartz.kCGEventLeftMouseDown,
        quartz.kCGEventLeftMouseUp,
        quartz.kCGEventLeftMouseDown,
        quartz.kCGEventLeftMouseUp,
        quartz.kCGEventLeftMouseDown,
        quartz.kCGEventLeftMouseUp,
    ]
    # Off-screen primer stays (-1.0, -1.0) on BOTH macOS < 27 and macOS >= 27.
    assert events[1]["win_loc"] == (-1.0, -1.0)
    assert events[2]["win_loc"] == (-1.0, -1.0)
    expected_loc = (150.0, 150.0) if window_local else (250.0, 350.0)
    assert events[0]["win_loc"] == expected_loc
    assert events[3]["win_loc"] == expected_loc
    # Every event zeroes modifier flags and stamps PID + window IDs + phase/click_state/subtype.
    assert all(ev["flags"] == 0 for ev in events)
    assert all(
        ev["sl_fields"][40] == 42
        and ev["sl_fields"][51] == 99
        and ev["sl_fields"][91] == 99
        and ev["sl_fields"][92] == 99
        for ev in events
    )
    assert [ev["sl_fields"][0] for ev in events] == [2, 1, 2, 3, 3, 3, 3]
    assert [ev["sl_fields"][1] for ev in events] == [0, 1, 1, 1, 1, 2, 2]
    assert all(ev["sl_fields"][7] == 3 for ev in events)
    assert len({ev["sl_fields"][58] for ev in events}) == 1


def test_right_click_hover_scroll_and_cg_post_to_pid_fallback(
    quartz: SimpleNamespace,
    skylight: SimpleNamespace,
) -> None:
    """Right-click omits left primer; hover/scroll never touch HID; unavailable SkyLight falls back to CGEventPostToPid."""
    click_background(
        42,
        250,
        350,
        button="right",
        count=1,
        window_id=99,
        window_rect=WINDOW_RECT,
        check=lambda _p: None,
    )
    assert [ev["kind"] for _, ev, _ in skylight.posts] == [
        quartz.kCGEventMouseMoved,
        quartz.kCGEventRightMouseDown,
        quartz.kCGEventRightMouseUp,
    ]
    assert skylight.posts[1][1]["sl_fields"][3] == 1

    skylight.posts.clear()
    skylight.activations.clear()
    move_background(42, 300, 400, window_id=99, window_rect=WINDOW_RECT, check=lambda _p: None)
    assert skylight.activations == []
    assert len(skylight.posts) == 1
    assert skylight.posts[0][1]["kind"] == quartz.kCGEventMouseMoved

    # SkyLight unavailable -> falls back to CGEventPostToPid for scroll too.
    skylight.post_returns = False
    scroll_background(
        42,
        300,
        400,
        clicks=-6,
        horizontal=True,
        window_id=99,
        window_rect=WINDOW_RECT,
        check=lambda _p: None,
    )
    assert quartz.posted == []
    assert len(quartz.pid_posted) == 2
    wheel_pid, wheel_ev = quartz.pid_posted[1]
    assert wheel_pid == 42
    assert wheel_ev["wheel"] == (0, -6)
    assert wheel_ev["point"] == (300, 400)
    assert wheel_ev["flags"] == 0
    assert wheel_ev["sl_fields"][40] == 42
    assert wheel_ev["sl_fields"][51] == 99


def test_drag_background_uses_subtype_zero_and_releases_on_mid_drag_failure(
    quartz: SimpleNamespace,
    skylight: SimpleNamespace,
) -> None:
    """Background drag revalidates before primer/press, uses subtype=0, and releases mouse-up on interruption."""
    checks = 0

    def check(_point: tuple[int, int]) -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            msg = "window moved"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="window moved"):
        drag_background(
            42,
            (150, 250),
            (550, 450),
            duration=0.1,
            window_id=99,
            window_rect=WINDOW_RECT,
            check=check,
        )
    events = [ev for _, ev, _ in skylight.posts]
    # Hover + primer down/up + down + 1 dragged + finally up.
    assert [ev["kind"] for ev in events[:4]] == [
        quartz.kCGEventMouseMoved,
        quartz.kCGEventLeftMouseDown,
        quartz.kCGEventLeftMouseUp,
        quartz.kCGEventLeftMouseDown,
    ]
    assert events[-1]["kind"] == quartz.kCGEventLeftMouseUp
    assert all(ev["sl_fields"][7] == 0 for ev in events[3:])
    assert all(auth is False for _, _, auth in skylight.posts)


@pytest.mark.usefixtures("quartz")
def test_type_text_background_chunks_utf16_as_unicode_only_and_guards_before_activate(
    skylight: SimpleNamespace,
) -> None:
    """Typing runs before_chunk first, activates once, keeps CR/LF/Tab as Unicode on keycode 0, and attaches auth."""
    with pytest.raises(RuntimeError, match="refused"):
        type_text_background(42, 99, "abc", before_chunk=lambda: (_ for _ in ()).throw(RuntimeError("refused")))
    assert skylight.activations == []
    assert skylight.posts == []

    # 15 ASCII chars + emoji (2 UTF-16 units) splits before the emoji; CR/LF/Tab stay Unicode (never Return/Tab).
    text = ("a" * 15) + "🙂\r\nx\ty" + ("b" * 12)
    checks = 0

    def before_chunk() -> None:
        nonlocal checks
        checks += 1

    type_text_background(42, 99, text, before_chunk=before_chunk)
    assert skylight.activations == [(42, 99)]  # Activated once before first chunk.
    assert checks == 3
    assert all(auth is True for _, _, auth in skylight.posts)
    downs = [ev for _, ev, _ in skylight.posts if ev["down"]]
    assert [(ev["keycode"], ev.get("unicode")) for ev in downs] == [
        (0, (15, "a" * 15)),
        (0, (16, "🙂\r\nx\ty" + ("b" * 9))),
        (0, (3, "bbb")),
    ]
    assert all(ev["keycode"] == 0 for _, ev, _ in skylight.posts)
    assert all(ev["flags"] == 0 for _, ev, _ in skylight.posts)


def test_keypress_background_maps_keycodes_clears_command_on_up_and_falls_back(
    quartz: SimpleNamespace,
    skylight: SimpleNamespace,
) -> None:
    """Background chords attach auth, clear Command on key-up (Catalyst safety), and fall back to CGEventPostToPid."""
    keypress_background(42, 99, ("command", "shift", "z"))
    assert skylight.activations == [(42, 99)]
    assert [auth for _, _, auth in skylight.posts] == [True, True]
    down, up = skylight.posts[0][1], skylight.posts[1][1]
    assert (down["keycode"], up["keycode"]) == (6, 6)
    assert down["flags"] == quartz.kCGEventFlagMaskCommand | quartz.kCGEventFlagMaskShift
    assert up["flags"] == quartz.kCGEventFlagMaskShift  # Command cleared on key-up!

    # Fallback to CGEventPostToPid when SkyLight is unavailable.
    skylight.post_returns = False
    keypress_background(42, None, ("enter",))
    assert [(pid, ev["keycode"], ev["down"]) for pid, ev in quartz.pid_posted] == [(42, 36, True), (42, 36, False)]
