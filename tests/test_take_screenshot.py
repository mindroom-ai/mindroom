"""Tests for the dashboard screenshot helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "frontend" / "take_screenshot.py"
_MODULE_SPEC = importlib.util.spec_from_file_location("mindroom_take_screenshot", _SCRIPT_PATH)
assert _MODULE_SPEC is not None and _MODULE_SPEC.loader is not None
take_screenshot = importlib.util.module_from_spec(_MODULE_SPEC)
_MODULE_SPEC.loader.exec_module(take_screenshot)


def test_resolve_demo_url_defaults_to_mindroom_port() -> None:
    """The helper should default to the bundled dashboard URL."""
    assert take_screenshot._resolve_demo_url(None) == "http://localhost:8765"


def test_resolve_demo_url_accepts_port_override() -> None:
    """Passing a port should resolve to the local dashboard URL."""
    assert take_screenshot._resolve_demo_url("3003") == "http://localhost:3003"


def test_resolve_demo_url_accepts_explicit_url() -> None:
    """Passing a full URL should preserve the requested target."""
    assert take_screenshot._resolve_demo_url("https://example.com/demo") == "https://example.com/demo"


def test_resolve_demo_url_rejects_invalid_target() -> None:
    """Invalid inputs should produce a clear usage error."""
    with pytest.raises(ValueError, match="not a valid port number or URL"):
        take_screenshot._resolve_demo_url("not-a-url")
