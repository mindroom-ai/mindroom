"""Bounded, in-memory ScreenCaptureKit screenshots on macOS 14 and later."""

from __future__ import annotations

import math
import platform
import threading
import time
from typing import TYPE_CHECKING, Any

from mindroom.desktop.accessibility import DesktopRect

# Objective-C framework and callback types are synthesized by PyObjC at runtime.
# ruff: noqa: ANN401

if TYPE_CHECKING:
    from collections.abc import Callable

_CAPTURE_TIMEOUT_SECONDS = 5.0


class MacOSCaptureError(RuntimeError):
    """A screenshot could not preserve its authorized content binding."""


def _frameworks() -> tuple[Any, Any]:
    version = platform.mac_ver()[0]
    if not version or int(version.split(".")[0]) < 14:
        msg = "Desktop screenshots require macOS 14 or later."
        raise MacOSCaptureError(msg)
    try:
        import objc  # noqa: PLC0415
        import ScreenCaptureKit  # noqa: PLC0415
    except ImportError as exc:
        msg = "ScreenCaptureKit is missing; reinstall MindRoom with the 'desktop' extra."
        raise MacOSCaptureError(msg) from exc
    return ScreenCaptureKit, objc


def _await_result(start: Callable[[Callable[..., None]], None], deadline: float) -> Any:
    done = threading.Event()
    result: list[tuple[Any, Any]] = []

    def complete(value: Any, error: Any) -> None:
        result.append((value, error))
        done.set()

    start(complete)
    if not done.wait(max(0.0, deadline - time.monotonic())):
        msg = "macOS screenshot timed out; check Screen Recording permission and retry."
        raise MacOSCaptureError(msg)
    value, error = result[0]
    if error is not None or value is None:
        msg = "macOS screenshot failed; check Screen Recording permission and request fresh app state."
        raise MacOSCaptureError(msg)
    return value


def _content(framework: Any, deadline: float) -> Any:
    return _await_result(
        lambda complete: (
            framework.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                True,
                True,
                complete,
            )
        ),
        deadline,
    )


def _rect(frame: Any) -> DesktopRect:
    return DesktopRect(
        round(frame.origin.x),
        round(frame.origin.y),
        round(frame.size.width),
        round(frame.size.height),
    )


def _image(framework: Any, content_filter: Any, deadline: float, *, window: bool) -> object:
    import Quartz  # noqa: PLC0415

    scale = float(content_filter.pointPixelScale())
    rect = content_filter.contentRect()
    if not math.isfinite(scale) or not 0 < scale <= 4:
        msg = "ScreenCaptureKit returned an unsupported pixel scale."
        raise MacOSCaptureError(msg)
    width, height = round(rect.size.width * scale), round(rect.size.height * scale)
    if not 0 < width <= 32768 or not 0 < height <= 32768 or width * height > 100_000_000:
        msg = "ScreenCaptureKit returned unsupported capture dimensions."
        raise MacOSCaptureError(msg)
    configuration = framework.SCStreamConfiguration.alloc().init()
    configuration.setWidth_(width)
    configuration.setHeight_(height)
    configuration.setShowsCursor_(False)
    if window:
        configuration.setIgnoreShadowsSingleWindow_(True)
        # This selector was added in 14.2; earlier single-window captures do
        # not opt into child windows.
        if hasattr(configuration, "setIncludeChildWindows_"):
            configuration.setIncludeChildWindows_(False)
    image = _await_result(
        lambda complete: framework.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter,
            configuration,
            complete,
        ),
        deadline,
    )
    if (
        int(Quartz.CGImageGetWidth(image)) != width  # ty: ignore[unresolved-attribute]
        or int(Quartz.CGImageGetHeight(image)) != height  # ty: ignore[unresolved-attribute]
    ):
        msg = "ScreenCaptureKit returned unexpected pixel dimensions; request fresh app state."
        raise MacOSCaptureError(msg)
    return image


def capture_window(window_id: int, process_id: int, region: DesktopRect) -> object:
    """Capture only the SCWindow matching the already validated CG window."""
    framework, objc = _frameworks()
    deadline = time.monotonic() + _CAPTURE_TIMEOUT_SECONDS
    with objc.autorelease_pool():
        content = _content(framework, deadline)
        matching = [
            window
            for window in content.windows()
            if int(window.windowID()) == window_id
            and window.owningApplication() is not None
            and int(window.owningApplication().processID()) == process_id
            and window.isOnScreen()
            and _rect(window.frame()) == region
        ]
        if len(matching) != 1:
            msg = "ScreenCaptureKit could not bind one exact on-screen application window; request fresh app state."
            raise MacOSCaptureError(msg)
        content_filter = framework.SCContentFilter.alloc().initWithDesktopIndependentWindow_(matching[0])
        if _rect(content_filter.contentRect()).width != region.width or (
            _rect(content_filter.contentRect()).height != region.height
        ):
            msg = "ScreenCaptureKit window bounds changed; request fresh app state."
            raise MacOSCaptureError(msg)
        return _image(framework, content_filter, deadline, window=True)


def capture_display(display_id: int) -> object:
    """Capture one explicit display; callers own policy and logical cropping."""
    framework, objc = _frameworks()
    deadline = time.monotonic() + _CAPTURE_TIMEOUT_SECONDS
    with objc.autorelease_pool():
        content = _content(framework, deadline)
        matching = [display for display in content.displays() if int(display.displayID()) == display_id]
        if len(matching) != 1:
            msg = "ScreenCaptureKit could not bind one exact display."
            raise MacOSCaptureError(msg)
        content_filter = framework.SCContentFilter.alloc().initWithDisplay_excludingWindows_(matching[0], [])
        return _image(framework, content_filter, deadline, window=False)
