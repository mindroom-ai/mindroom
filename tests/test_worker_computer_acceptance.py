"""Acceptance helpers preserve native results and wait for viewer readiness."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest


def _helper() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/testing/worker_computer_native.py"
    spec = importlib.util.spec_from_file_location("worker_computer_native", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_viewer_waits_for_async_module_before_connecting() -> None:
    """Page load can precede noVNC's top-level asynchronous import completion."""
    module = _helper()
    page = AsyncMock()
    await module.connect_viewer(page, {"session_id": "owned"})
    assert page.mock_calls[0].args == ("typeof window.connectComputer === 'function'",)
    assert page.mock_calls[0].kwargs == {"timeout": 30000}
    assert "connectComputer" in page.mock_calls[1].args[0]


def test_native_result_parser_uses_only_result_section() -> None:
    """Code, snapshots and braces outside the result cannot corrupt JSON readback."""
    module = _helper()
    text = '### Result\n{"value": "typed"}\n### Ran Playwright code\n```js\n() => {}\n```'
    assert module.native_json(text) == {"value": "typed"}


def test_native_tab_indices_remain_current() -> None:
    """The native server exposes mutable indices, not stable CDP targets."""
    module = _helper()
    text = "### Result\n- 0: (current) [Fixture](http://127.0.0.1:8767/)\n- 1: [New Tab](chrome://newtab/)"
    assert module.native_tabs(text) == [
        {"index": 0, "current": True, "title": "Fixture", "url": "http://127.0.0.1:8767/"},
        {"index": 1, "current": False, "title": "New Tab", "url": "chrome://newtab/"},
    ]
