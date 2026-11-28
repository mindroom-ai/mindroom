"""Tests for the fail-soft SkyLight and ApplicationServices ctypes bridge."""

from __future__ import annotations

import ctypes
import struct
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from mindroom.desktop import macos_skylight


@pytest.fixture(autouse=True)
def _reset_skylight_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macos_skylight, "_libs", {})
    monkeypatch.setattr(macos_skylight, "_symbols", {})


def _install_fake_libs(monkeypatch: pytest.MonkeyPatch, libs: dict[str, object]) -> None:
    def fake_load(path: str) -> object:
        return libs.get(path)

    monkeypatch.setattr(macos_skylight, "_load_library", fake_load)


def test_fail_soft_when_libraries_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every SkyLight helper returns False/None cleanly when macOS frameworks are unavailable."""
    _install_fake_libs(monkeypatch, {})
    assert macos_skylight.is_available() is False
    assert macos_skylight.ax_window_id(object()) is None
    assert macos_skylight.set_window_location(object(), 10.0, 20.0) is False
    assert macos_skylight.set_integer_field(object(), 40, 123) is False
    assert macos_skylight.post_to_pid(123, object(), attach_auth_message=True) is False
    assert macos_skylight.activate_without_raise(123, 456) is False
    assert macos_skylight.hw_capture_window(456) is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("14.7", False), ("15.4.1", False), ("26.0", False), ("27.0", True), ("28.1", True), ("", False), ("bad", False)],
)
def test_window_location_coordinate_mode_by_macos_version(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    expected: bool,
) -> None:
    """MacOS 27+ uses window-local coordinates; older versions use global screen coordinates."""
    monkeypatch.setattr(macos_skylight.platform, "mac_ver", lambda: (version, ("", "", ""), ""))
    assert macos_skylight.window_location_is_window_local() is expected


def test_ax_window_id_and_event_field_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_AXUIElementGetWindow, CGEventSetWindowLocation, and SLEventSetIntegerValueField pass raw pointers."""
    calls: list[tuple[str, object]] = []
    status_and_wid = [0, 777]

    def get_window(ptr: ctypes.c_void_p, out_ptr: object) -> int:
        calls.append(("ax_wid", ptr.value))
        ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = status_and_wid[1]
        return status_and_wid[0]

    def set_loc(ptr: ctypes.c_void_p, x: ctypes.c_double, y: ctypes.c_double) -> None:
        calls.append(("loc", (ptr.value, x.value, y.value)))

    def set_field(ptr: ctypes.c_void_p, field: ctypes.c_uint32, val: ctypes.c_int64) -> None:
        calls.append(("field", (ptr.value, field.value, val.value)))

    _install_fake_libs(
        monkeypatch,
        {
            macos_skylight._AS: SimpleNamespace(_AXUIElementGetWindow=get_window),
            macos_skylight._CG: SimpleNamespace(CGEventSetWindowLocation=set_loc),
            macos_skylight._SL: SimpleNamespace(
                SLEventSetIntegerValueField=set_field,
                SLEventPostToPid=lambda _pid, _ev: None,
            ),
        },
    )
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(pyobjc_id=lambda _obj: 0x1000))

    assert macos_skylight.is_available() is True
    assert macos_skylight.ax_window_id("ax-win") == 777
    assert macos_skylight.set_window_location("ev", 12.5, 34.5) is True
    assert macos_skylight.set_integer_field("ev", 51, 777) is True
    assert calls == [("ax_wid", 0x1000), ("loc", (0x1000, 12.5, 34.5)), ("field", (0x1000, 51, 777))]

    status_and_wid[:] = [1, 777]
    assert macos_skylight.ax_window_id("ax-win") is None
    status_and_wid[:] = [0, 0]
    assert macos_skylight.ax_window_id("ax-win") is None


@pytest.mark.parametrize("record_offset", [24, 32, 16])
def test_post_to_pid_attaches_auth_message_only_when_requested_and_probes_offsets(
    monkeypatch: pytest.MonkeyPatch,
    record_offset: int,
) -> None:
    """Keyboard posts probe embedded SLSEventRecord* at (24, 32, 16) via metaclass selector; mouse posts skip auth."""
    event_buf = (ctypes.c_uint8 * 64)()
    fake_record_addr = 0xDEAD_BEEF
    ctypes.c_void_p.from_address(ctypes.addressof(event_buf) + record_offset).value = fake_record_addr

    calls: list[tuple[str, object]] = []
    responds_result = [True]

    _install_fake_libs(
        monkeypatch,
        {
            macos_skylight._SL: SimpleNamespace(
                SLEventPostToPid=lambda pid, ev: calls.append(("post", (pid.value, ev.value))),
                SLEventSetAuthenticationMessage=lambda ev, msg: calls.append(("set_auth", (ev.value, msg.value))),
            ),
            macos_skylight._OBJC: SimpleNamespace(
                objc_getClass=lambda name: 0x2000 if name == b"SLSEventAuthenticationMessage" else 0,
                object_getClass=lambda cls: cls + 0x10,
                sel_registerName=lambda name: 0x3000 if name == b"messageWithEventRecord:pid:version:" else 0,
                class_respondsToSelector=lambda meta, sel: (
                    calls.append(("responds", (meta, sel))) or responds_result[0]
                ),
                objc_msgSend=lambda cls, sel, rec, pid, ver: (
                    calls.append(("msg_send", (cls, sel, rec.value, pid.value, ver.value))) or 0x4000
                ),
            ),
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "objc",
        SimpleNamespace(pyobjc_id=lambda _obj: ctypes.addressof(event_buf), autorelease_pool=nullcontext),
    )

    # Mouse/scroll path: attach_auth_message=False must NEVER create or attach an auth message.
    assert macos_skylight.post_to_pid(42, "mouse-ev", attach_auth_message=False) is True
    assert calls == [("post", (42, ctypes.addressof(event_buf)))]

    # Keyboard path: attaches auth message via metaclass check + probed record offset.
    calls.clear()
    assert macos_skylight.post_to_pid(42, "key-ev", attach_auth_message=True) is True
    assert calls == [
        ("responds", (0x2010, 0x3000)),
        ("msg_send", (0x2000, 0x3000, fake_record_addr, 42, 0)),
        ("set_auth", (ctypes.addressof(event_buf), 0x4000)),
        ("post", (42, ctypes.addressof(event_buf))),
    ]

    # macOS 14 fallback: selector absent on metaclass -> skips auth attachment and still posts.
    calls.clear()
    responds_result[0] = False
    assert macos_skylight.post_to_pid(42, "key-ev", attach_auth_message=True) is True
    assert calls == [("responds", (0x2010, 0x3000)), ("post", (42, ctypes.addressof(event_buf)))]


def test_activate_without_raise_builds_focus_record_and_falls_back_to_pid_psn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focus record encodes 0xF8 length, 0x0D type, LE window_id at 0x3C, 0x01 focus at 0x8A, and posts once."""
    posted: list[tuple[bytes, bytes]] = []
    owner_status = [0]

    def get_owner(cid: ctypes.c_uint32, wid: ctypes.c_uint32, out_owner: object) -> int:
        assert (cid.value, wid.value) == (11, 0x12345678)
        ctypes.cast(out_owner, ctypes.POINTER(ctypes.c_uint32))[0] = 22
        return owner_status[0]

    def get_psn(owner_cid: ctypes.c_uint32, psn_ptr: object) -> int:
        assert owner_cid.value == 22
        ctypes.memmove(psn_ptr, b"\x01\x02\x03\x04\x05\x06\x07\x08", 8)
        return 0

    def get_proc(pid: ctypes.c_int32, psn_ptr: object) -> int:
        assert pid.value == 42
        ctypes.memmove(psn_ptr, b"\xaa\xbb\xcc\xdd\x11\x22\x33\x44", 8)
        return 0

    def post_record(psn_ptr: object, rec_ptr: object) -> int:
        posted.append((ctypes.string_at(psn_ptr, 8), ctypes.string_at(rec_ptr, 0xF8)))
        return 0

    _install_fake_libs(
        monkeypatch,
        {
            macos_skylight._SL: SimpleNamespace(
                CGSMainConnectionID=lambda: 11,
                SLSGetWindowOwner=get_owner,
                SLSGetConnectionPSN=get_psn,
                SLPSPostEventRecordTo=post_record,
            ),
            macos_skylight._AS: SimpleNamespace(GetProcessForPID=get_proc),
        },
    )

    assert macos_skylight.activate_without_raise(42, 0x12345678) is True
    assert len(posted) == 1  # Never sends a second defocus record to the user's frontmost app.
    psn_bytes, rec_bytes = posted[0]
    assert psn_bytes == b"\x01\x02\x03\x04\x05\x06\x07\x08"
    assert len(rec_bytes) == 0xF8
    assert rec_bytes[0x04] == 0xF8
    assert rec_bytes[0x08] == 0x0D
    assert struct.unpack_from("<I", rec_bytes, 0x3C)[0] == 0x12345678
    assert rec_bytes[0x8A] == 0x01

    # Falls back to GetProcessForPID when SLSGetWindowOwner fails.
    posted.clear()
    owner_status[0] = -1
    assert macos_skylight.activate_without_raise(42, 0x12345678) is True
    assert posted[0][0] == b"\xaa\xbb\xcc\xdd\x11\x22\x33\x44"


def test_hw_capture_window_validates_type_wraps_before_release_and_always_releases_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SLSHWCaptureWindowList validates CGImage type, wraps while array retains it, and always CFReleases."""
    events: list[tuple[str, object]] = []
    img_type = [99]

    def hw_capture(cid: ctypes.c_uint32, wid_ptr: object, count: int, options: ctypes.c_uint32) -> int:
        wid = ctypes.cast(wid_ptr, ctypes.POINTER(ctypes.c_uint32))[0]
        events.append(("capture", (cid.value, wid, count, options.value)))
        return 0x5000

    _install_fake_libs(
        monkeypatch,
        {
            macos_skylight._SL: SimpleNamespace(CGSMainConnectionID=lambda: 7, SLSHWCaptureWindowList=hw_capture),
            macos_skylight._CF: SimpleNamespace(
                CFArrayGetCount=lambda arr: 1 if arr.value == 0x5000 else 0,
                CFArrayGetValueAtIndex=lambda _arr, idx: 0x6000 if idx == 0 else 0,
                CFGetTypeID=lambda ptr: img_type[0] if ptr.value == 0x6000 else 0,
                CFRelease=lambda ptr: events.append(("release", ptr.value)),
            ),
            macos_skylight._CG: SimpleNamespace(CGImageGetTypeID=lambda: 99),
        },
    )

    def wrap_objc(*, c_void_p: ctypes.c_void_p) -> str:
        events.append(("wrap", c_void_p.value))
        return "wrapped-cg-image"

    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(objc_object=wrap_objc))

    assert macos_skylight.hw_capture_window(321) == "wrapped-cg-image"
    assert events == [
        ("capture", (7, 321, 1, (1 << 11) | (1 << 8))),
        ("wrap", 0x6000),
        ("release", 0x5000),
    ]

    # Wrong CFTypeID returns None and still releases the CFArray.
    events.clear()
    img_type[0] = 42
    assert macos_skylight.hw_capture_window(321) is None
    assert events[-1] == ("release", 0x5000)
