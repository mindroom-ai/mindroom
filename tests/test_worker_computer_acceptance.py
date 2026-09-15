"""Acceptance helpers preserve native results and wait for viewer readiness."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agno.tools.function import ToolResult

from mindroom.worker_computer.mcp_results import encode_browser_mcp_result


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


def test_native_result_parser_accepts_void_page_actions() -> None:
    """Native focus and storage mutations report JavaScript undefined explicitly."""
    assert _helper().native_json("### Result\nundefined\n### Ran Playwright code\n...") is None


def test_shell_readback_removes_only_known_cwd_header() -> None:
    """Native absolute upload paths use command stdout, not the display header."""
    assert _helper().shell_stdout("[cwd: /workspace]\n/workspace\n") == "/workspace\n"


def test_native_tab_indices_remain_current() -> None:
    """The native server exposes mutable indices, not stable CDP targets."""
    module = _helper()
    text = "### Result\n- 0: (current) [Fixture](http://127.0.0.1:8767/)\n- 1: [New Tab](chrome://newtab/)"
    assert module.native_tabs(text) == [
        {"index": 0, "current": True, "title": "Fixture", "url": "http://127.0.0.1:8767/"},
        {"index": 1, "current": False, "title": "New Tab", "url": "chrome://newtab/"},
    ]


def test_docker_fixture_preserves_explicit_daemon_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """An isolated runtime environment must still reach its owned Docker daemon."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.setenv("UNRELATED_SECRET", "not-forwarded")
    assert _helper().docker_environment() == {"DOCKER_HOST": "tcp://127.0.0.1:2375"}


@pytest.mark.asyncio
async def test_native_call_accepts_native_function_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The evaluate schema's function argument must not collide with helper routing."""
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("computer_acceptance_driver", scripts / "test-worker-computer.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = object.__new__(module.Fixture)
    fixture.args = SimpleNamespace(output=tmp_path)
    fixture.execute = AsyncMock(
        return_value={"ok": True, "result": encode_browser_mcp_result(ToolResult(content="### Result\n1"))},
    )
    result = await fixture.native("browser_evaluate", function="()=>1")
    fixture.execute.assert_awaited_once_with("browser_mcp", "browser_evaluate", {"function": "()=>1"})
    assert result.content == "### Result\n1"
