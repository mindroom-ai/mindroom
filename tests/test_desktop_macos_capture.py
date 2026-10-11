"""ScreenCaptureKit captures only the bound content and terminates stalled callbacks."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mindroom.desktop import macos_capture
from mindroom.desktop.accessibility import DesktopRect

REGION = DesktopRect(-1800, 100, 800, 600)


def _frame(region: DesktopRect) -> SimpleNamespace:
    return SimpleNamespace(
        origin=SimpleNamespace(x=region.x, y=region.y),
        size=SimpleNamespace(width=region.width, height=region.height),
    )


@pytest.fixture
def framework(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace external Objective-C APIs while preserving their callback signatures."""
    window = SimpleNamespace(
        windowID=lambda: 3,
        owningApplication=lambda: SimpleNamespace(processID=lambda: 42),
        frame=lambda: _frame(REGION),
        isOnScreen=lambda: True,
    )
    display = SimpleNamespace(displayID=lambda: 7)
    content = SimpleNamespace(windows=lambda: [window], displays=lambda: [display])
    config = SimpleNamespace(
        setWidth_=Mock(),
        setHeight_=Mock(),
        setShowsCursor_=Mock(),
        setIgnoreShadowsSingleWindow_=Mock(),
        setIncludeChildWindows_=Mock(),
    )
    filter_object = SimpleNamespace(
        pointPixelScale=lambda: 2.0,
        contentRect=lambda: _frame(REGION),
    )
    filters = SimpleNamespace(
        initWithDesktopIndependentWindow_=Mock(return_value=filter_object),
        initWithDisplay_excludingWindows_=Mock(return_value=filter_object),
    )
    screenshot = Mock(side_effect=lambda _filter, _config, callback: callback("image", None))
    shared = Mock(side_effect=lambda _desktop, _on_screen, callback: callback(content, None))
    api = SimpleNamespace(
        SCShareableContent=SimpleNamespace(
            getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=shared,
        ),
        SCContentFilter=SimpleNamespace(alloc=lambda: filters),
        SCStreamConfiguration=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: config)),
        SCScreenshotManager=SimpleNamespace(
            captureImageWithFilter_configuration_completionHandler_=screenshot,
        ),
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", api)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(autorelease_pool=nullcontext))
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        SimpleNamespace(
            CGImageGetWidth=lambda _image: 1600,
            CGImageGetHeight=lambda _image: 1200,
        ),
    )
    monkeypatch.setattr(macos_capture.platform, "mac_ver", lambda: ("14.0", ("", "", ""), ""))
    return SimpleNamespace(
        api=api,
        config=config,
        filters=filters,
        filter_object=filter_object,
        screenshot=screenshot,
        shared=shared,
        content=content,
        window=window,
        display=display,
    )


def test_window_capture_uses_exact_content_and_retina_size(framework: SimpleNamespace) -> None:
    """Another window cannot be captured by broadening the filter."""
    assert macos_capture.capture_window(3, 42, REGION) == "image"
    framework.filters.initWithDesktopIndependentWindow_.assert_called_once_with(framework.window)
    framework.filters.initWithDisplay_excludingWindows_.assert_not_called()
    framework.config.setWidth_.assert_called_once_with(1600)
    framework.config.setHeight_.assert_called_once_with(1200)
    framework.config.setShowsCursor_.assert_called_once_with(False)
    framework.config.setIgnoreShadowsSingleWindow_.assert_called_once_with(True)
    framework.config.setIncludeChildWindows_.assert_called_once_with(False)


@pytest.mark.parametrize("change", ["pid", "id", "frame", "hidden", "duplicate"])
def test_changed_window_identity_never_starts_capture(framework: SimpleNamespace, change: str) -> None:
    """The SCWindow must still identify the process and bounds from CG inventory."""
    if change == "pid":
        framework.window.owningApplication = lambda: SimpleNamespace(processID=lambda: 99)
    elif change == "id":
        framework.window.windowID = lambda: 4
    elif change == "frame":
        framework.window.frame = lambda: _frame(DesktopRect(0, 0, 800, 600))
    elif change == "hidden":
        framework.window.isOnScreen = lambda: False
    else:
        framework.content.windows = lambda: [framework.window, framework.window]
    with pytest.raises(macos_capture.MacOSCaptureError, match="window"):
        macos_capture.capture_window(3, 42, REGION)
    framework.screenshot.assert_not_called()


@pytest.mark.parametrize("stage", ["shared", "screenshot"])
def test_callback_error_fails_without_fallback(framework: SimpleNamespace, stage: str) -> None:
    """A denied or failed operation cannot produce a successful screenshot."""
    getattr(framework, stage).side_effect = lambda *args: args[-1](None, "permission denied")
    with pytest.raises(macos_capture.MacOSCaptureError, match="Screen Recording"):
        macos_capture.capture_window(3, 42, REGION)


@pytest.mark.parametrize("stage", ["shared", "screenshot"])
def test_missing_callback_has_bounded_wait(
    framework: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """An OS callback that never arrives cannot strand the bridge worker."""
    monkeypatch.setattr(macos_capture, "_CAPTURE_TIMEOUT_SECONDS", 0.01)
    getattr(framework, stage).side_effect = lambda *_args: None
    with pytest.raises(macos_capture.MacOSCaptureError, match="timed out"):
        macos_capture.capture_window(3, 42, REGION)


def test_primary_display_is_selected_by_id(framework: SimpleNamespace) -> None:
    """Primary display selection cannot fall through to an arbitrary screen."""
    assert macos_capture.capture_display(7) == "image"
    framework.filters.initWithDisplay_excludingWindows_.assert_called_once_with(framework.display, [])
    with pytest.raises(macos_capture.MacOSCaptureError, match="display"):
        macos_capture.capture_display(8)


def test_old_macos_is_rejected_before_framework_calls(
    framework: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing screenshot selectors on older macOS have an actionable error."""
    monkeypatch.setattr(macos_capture.platform, "mac_ver", lambda: ("13.6", ("", "", ""), ""))
    with pytest.raises(macos_capture.MacOSCaptureError, match="macOS 14"):
        macos_capture.capture_window(3, 42, REGION)
    framework.shared.assert_not_called()


@pytest.mark.usefixtures("framework")
def test_missing_framework_has_install_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete desktop installations fail before accessing content."""
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", None)
    with pytest.raises(macos_capture.MacOSCaptureError, match="desktop"):
        macos_capture.capture_window(3, 42, REGION)


@pytest.mark.parametrize("size", [(0, 1200), (800, 600), (1600, 1199)])
@pytest.mark.usefixtures("framework")
def test_unexpected_pixel_dimensions_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    """A scaled or clipped result cannot inherit incorrect logical geometry."""
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        SimpleNamespace(
            CGImageGetWidth=lambda _image: size[0],
            CGImageGetHeight=lambda _image: size[1],
        ),
    )
    with pytest.raises(macos_capture.MacOSCaptureError, match="dimensions"):
        macos_capture.capture_window(3, 42, REGION)


def test_macos_14_without_child_window_selector(framework: SimpleNamespace) -> None:
    """The macOS 14.0 single-window API works before the 14.2 child-window option."""
    del framework.config.setIncludeChildWindows_
    assert macos_capture.capture_window(3, 42, REGION) == "image"


@pytest.mark.parametrize("scale", [0, float("nan"), float("inf"), 8])
def test_uncertain_pixel_scale_never_starts_capture(framework: SimpleNamespace, scale: float) -> None:
    """Unavailable or unsupported Retina geometry cannot authorize a pixel mapping."""
    framework.filter_object.pointPixelScale = lambda: scale
    with pytest.raises(macos_capture.MacOSCaptureError, match="scale"):
        macos_capture.capture_window(3, 42, REGION)
    framework.screenshot.assert_not_called()


def test_filter_size_change_never_starts_capture(framework: SimpleNamespace) -> None:
    """A window resized between enumeration and filter creation needs a fresh observation."""
    framework.filter_object.contentRect = lambda: _frame(DesktopRect(-1800, 100, 640, 480))
    with pytest.raises(macos_capture.MacOSCaptureError, match="bounds changed"):
        macos_capture.capture_window(3, 42, REGION)
    framework.screenshot.assert_not_called()
