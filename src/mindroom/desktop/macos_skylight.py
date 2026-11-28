"""Fail-soft ctypes bridge to macOS SkyLight and ApplicationServices private APIs."""

from __future__ import annotations

import ctypes
import platform
import struct
from contextlib import nullcontext, suppress
from ctypes import POINTER, c_bool, c_char_p, c_double, c_int, c_int32, c_int64, c_ssize_t, c_uint32, c_ulong, c_void_p
from typing import Any

# Private macOS C/Objective-C symbols are loaded dynamically via ctypes.
# ruff: noqa: ANN401

_SL = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"
_AS = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_OBJC = "/usr/lib/libobjc.A.dylib"

_HW_CAPTURE_OPTIONS = (1 << 11) | (1 << 8)  # IGNORE_GLOBAL_CLIP_SHAPE | BEST_RESOLUTION
_EVENT_RECORD_OFFSETS = (24, 32, 16)
_FOCUS_RECORD_SIZE = 0xF8
_MAX_UINT32 = 0xFFFFFFFF

_SPECS: dict[str, tuple[tuple[str, ...], list[Any], Any]] = {
    "_AXUIElementGetWindow": ((_AS,), [c_void_p, POINTER(c_uint32)], c_int32),
    "CGEventSetWindowLocation": ((_CG, _AS, _SL), [c_void_p, c_double, c_double], None),
    "SLEventSetIntegerValueField": ((_SL,), [c_void_p, c_uint32, c_int64], None),
    "SLEventPostToPid": ((_SL,), [c_int, c_void_p], None),
    "SLEventSetAuthenticationMessage": ((_SL,), [c_void_p, c_void_p], None),
    "SLPSPostEventRecordTo": ((_SL,), [c_void_p, c_void_p], c_int32),
    "CGSMainConnectionID": ((_SL, _CG, _AS), [], c_uint32),
    "SLSGetWindowOwner": ((_SL,), [c_uint32, c_uint32, POINTER(c_uint32)], c_int32),
    "SLSGetConnectionPSN": ((_SL,), [c_uint32, c_void_p], c_int32),
    "GetProcessForPID": ((_AS,), [c_int32, c_void_p], c_int32),
    "SLSHWCaptureWindowList": ((_SL,), [c_uint32, POINTER(c_uint32), c_int, c_uint32], c_void_p),
    "CFArrayGetCount": ((_CF,), [c_void_p], c_ssize_t),
    "CFArrayGetValueAtIndex": ((_CF,), [c_void_p, c_ssize_t], c_void_p),
    "CFGetTypeID": ((_CF,), [c_void_p], c_ulong),
    "CFRelease": ((_CF,), [c_void_p], None),
    "CGImageGetTypeID": ((_CG, _AS), [], c_ulong),
    "objc_getClass": ((_OBJC,), [c_char_p], c_void_p),
    "object_getClass": ((_OBJC,), [c_void_p], c_void_p),
    "sel_registerName": ((_OBJC,), [c_char_p], c_void_p),
    "class_respondsToSelector": ((_OBJC,), [c_void_p, c_void_p], c_bool),
    "objc_msgSend": ((_OBJC,), [c_void_p, c_void_p, c_void_p, c_int, c_uint32], c_void_p),
}

_libs: dict[str, Any] = {}
_symbols: dict[str, Any] = {}


def _load_library(path: str) -> Any:
    if path not in _libs:
        try:
            _libs[path] = ctypes.CDLL(path)
        except OSError:
            _libs[path] = None
    return _libs[path]


def _sym(name: str) -> Any:
    if name in _symbols:
        return _symbols[name]
    paths, argtypes, restype = _SPECS[name]
    for path in paths:
        lib = _load_library(path)
        fn = getattr(lib, name, None) if lib is not None else None
        if fn is None:
            continue
        with suppress(AttributeError):
            fn.argtypes, fn.restype = argtypes, restype
        _symbols[name] = fn
        return fn
    _symbols[name] = None
    return None


def _syms(*names: str) -> tuple[Any, ...] | None:
    resolved = tuple(_sym(name) for name in names)
    return resolved if all(resolved) else None


def _pyobjc_ptr(obj: object) -> int | None:
    if obj is None:
        return None
    try:
        import objc  # noqa: PLC0415

        return int(objc.pyobjc_id(obj)) or None
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _autorelease_pool() -> Any:
    try:
        import objc  # noqa: PLC0415

        return objc.autorelease_pool()
    except (ImportError, AttributeError):
        return nullcontext()


def _attach_auth_message(pid: int, event_ptr: int) -> None:
    fns = _syms(
        "SLEventSetAuthenticationMessage",
        "objc_getClass",
        "sel_registerName",
        "class_respondsToSelector",
        "objc_msgSend",
    )
    if fns is None:
        return
    set_auth, get_cls, sel_reg, responds, msg_send = fns
    cls, sel = get_cls(b"SLSEventAuthenticationMessage"), sel_reg(b"messageWithEventRecord:pid:version:")
    obj_get_cls = _sym("object_getClass")
    meta = obj_get_cls(cls) if cls and obj_get_cls else cls
    if not (cls and sel and meta and responds(meta, sel)):
        return
    record_ptr = next(
        (int(v) for off in _EVENT_RECORD_OFFSETS if (v := c_void_p.from_address(event_ptr + off).value)),
        0,
    )
    if record_ptr and (msg := msg_send(cls, sel, c_void_p(record_ptr), c_int(pid), c_uint32(0))):
        set_auth(c_void_p(event_ptr), c_void_p(msg))


def _resolve_psn(pid: int, window_id: int) -> Any:
    psn = (ctypes.c_uint8 * 8)()
    if (fns := _syms("CGSMainConnectionID", "SLSGetWindowOwner", "SLSGetConnectionPSN")) is not None:
        main_conn, get_owner, get_psn = fns
        cid, owner = int(main_conn()), c_uint32(0)
        if (
            cid
            and get_owner(c_uint32(cid), c_uint32(window_id), ctypes.byref(owner)) == 0
            and owner.value
            and get_psn(c_uint32(owner.value), ctypes.byref(psn)) == 0
        ):
            return psn
    get_process = _sym("GetProcessForPID")
    return psn if get_process and get_process(c_int32(pid), ctypes.byref(psn)) == 0 else None


def is_available() -> bool:
    """Return whether SkyLight PID-targeted event posting resolved."""
    return _sym("SLEventPostToPid") is not None


def ax_window_id(ax_window: object) -> int | None:
    """Resolve the Core Graphics window ID for an AXUIElement window reference."""
    get_window, ptr = _sym("_AXUIElementGetWindow"), _pyobjc_ptr(ax_window)
    if get_window is None or ptr is None:
        return None
    wid = c_uint32(0)
    return int(wid.value) if int(get_window(c_void_p(ptr), ctypes.byref(wid))) == 0 and wid.value > 0 else None


def window_location_is_window_local() -> bool:
    """Return True on macOS 27+, where CGEventSetWindowLocation expects window-local coordinates."""
    version = platform.mac_ver()[0]
    try:
        return bool(version) and int(version.split(".")[0]) >= 27
    except ValueError:
        return False


def set_window_location(event: object, x: float, y: float) -> bool:
    """Set the event's Core Graphics window location field."""
    fn, ptr = _sym("CGEventSetWindowLocation"), _pyobjc_ptr(event)
    if fn is None or ptr is None:
        return False
    fn(c_void_p(ptr), c_double(x), c_double(y))
    return True


def set_integer_field(event: object, field: int, value: int) -> bool:
    """Set one raw integer event field via SkyLight."""
    fn, ptr = _sym("SLEventSetIntegerValueField"), _pyobjc_ptr(event)
    if fn is None or ptr is None:
        return False
    fn(c_void_p(ptr), c_uint32(field), c_int64(value))
    return True


def post_to_pid(pid: int, event: object, *, attach_auth_message: bool = False) -> bool:
    """Post a CGEvent directly to one process via SkyLight, optionally attaching an auth envelope."""
    post_fn, event_ptr = _sym("SLEventPostToPid"), _pyobjc_ptr(event)
    if pid <= 0 or post_fn is None or event_ptr is None:
        return False
    with _autorelease_pool() if attach_auth_message else nullcontext():
        if attach_auth_message:
            _attach_auth_message(pid, event_ptr)
        post_fn(c_int(pid), c_void_p(event_ptr))
    return True


def activate_without_raise(pid: int, window_id: int) -> bool:
    """Deliver a SkyLight focus record to the target window's PSN without raising it or defocusing the user."""
    if pid <= 0 or not 0 < window_id <= _MAX_UINT32:
        return False
    post_record = _sym("SLPSPostEventRecordTo")
    psn = _resolve_psn(pid, window_id) if post_record is not None else None
    if post_record is None or psn is None:
        return False
    record = (ctypes.c_uint8 * _FOCUS_RECORD_SIZE)()
    record[0x04], record[0x08], record[0x8A] = _FOCUS_RECORD_SIZE, 0x0D, 0x01
    struct.pack_into("<I", record, 0x3C, window_id)
    return int(post_record(ctypes.byref(psn), ctypes.byref(record))) == 0


def hw_capture_window(window_id: int) -> object | None:
    """Capture one window via SLSHWCaptureWindowList without switching Spaces."""
    fns = _syms(
        "CGSMainConnectionID",
        "SLSHWCaptureWindowList",
        "CFArrayGetCount",
        "CFArrayGetValueAtIndex",
        "CFGetTypeID",
        "CFRelease",
        "CGImageGetTypeID",
    )
    if not 0 < window_id <= _MAX_UINT32 or fns is None:
        return None
    main_conn, hw_capture, cf_count, cf_value, cf_type_id, cf_release, cg_image_type_id = fns
    try:
        import objc  # noqa: PLC0415
    except ImportError:
        return None
    if not hasattr(objc, "objc_object") or not (cid := int(main_conn())):
        return None
    wid = c_uint32(window_id)
    arr_ptr = hw_capture(c_uint32(cid), ctypes.byref(wid), 1, c_uint32(_HW_CAPTURE_OPTIONS))
    if not arr_ptr:
        return None
    try:
        img_ptr = cf_value(c_void_p(arr_ptr), 0) if int(cf_count(c_void_p(arr_ptr))) >= 1 else None
        if not img_ptr or int(cf_type_id(c_void_p(img_ptr))) != int(cg_image_type_id()):
            return None
        # PyObjC retains the CFTypeRef before we release the owning CFArray in finally.
        return objc.objc_object(c_void_p=c_void_p(img_ptr))
    finally:
        cf_release(c_void_p(arr_ptr))


__all__ = [
    "activate_without_raise",
    "ax_window_id",
    "hw_capture_window",
    "is_available",
    "post_to_pid",
    "set_integer_field",
    "set_window_location",
    "window_location_is_window_local",
]
