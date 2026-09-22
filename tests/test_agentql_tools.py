"""AgentQL scoped credentials and extraction results at the real SDK/HTTP boundary."""

from __future__ import annotations

import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, Self, cast

import pytest
import requests
import requests.adapters
from playwright.sync_api._context_manager import PlaywrightContextManager

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager, save_scoped_credentials
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from mindroom.tools.agentql import agentql_tools

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.agentql import AgentQLTools

_CUSTOM_QUERY = "{ title links[] { href label } metadata { count enabled missing } }"
_URL = "https://example.org/source"


class _SDKConfig(Protocol):
    api_key: str | None


@dataclass
class _AgentQLBoundary:
    sdk_config: _SDKConfig
    config_path: Path
    requests: list[requests.PreparedRequest] = field(default_factory=list)
    observed_auth: list[tuple[str | None, str | None]] = field(default_factory=list)
    browsers: list[_Browser] = field(default_factory=list)
    barrier: Barrier | None = None
    custom_data: object = field(
        default_factory=lambda: {"title": "An extracted title", "links": [{"href": "https://example.org/value"}]},
    )
    status_code: int = 200
    timeout_error: bool = False

    def respond(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Retain requests' header/body building and the SDK's response/error handling."""
        self.requests.append(request)
        self.observed_auth.append((os.environ.get("AGENTQL_API_KEY"), self.sdk_config.api_key))
        assert request.method == "POST"
        assert request.url == "https://api.agentql.test/api/v2/query-data"
        assert kwargs["timeout"] == 900
        assert isinstance(request.body, (str, bytes))
        body = json.loads(request.body)
        assert body["params"] == {"mode": "fast"}
        assert body["accessibility_tree"]["role"] == "document"
        assert body["request_origin"] == "sdk-playwright-python"
        if self.timeout_error:
            raise requests.exceptions.ReadTimeout
        data = (
            {"text_content": ["Alpha", "Alpha", " ", None, "Beta"]}
            if "text_content" in body["query"]
            else self.custom_data
        )
        response = requests.Response()
        response.status_code = self.status_code
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response.headers["X-Request-ID"] = "test-request"
        response.encoding = "utf-8"
        payload = (
            {"response": data, "request_id": "test-request"}
            if self.status_code == 200
            else {"detail": "Synthetic service failure"}
        )
        response._content = json.dumps(payload).encode()
        return response


class _BrowserPage:
    """Minimal browser operations while the real SDK parses queries and builds its tree."""

    def __init__(self, boundary: _AgentQLBoundary) -> None:
        self.boundary = boundary
        self.url = ""
        self._impl_obj = SimpleNamespace()
        self.main_frame = SimpleNamespace(child_frames=[])

    def on(self, _event: str, _callback: object) -> None:
        pass

    def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url
        if self.boundary.barrier is not None:
            self.boundary.barrier.wait(timeout=5)

    def evaluate(self, _script: str, arg: object = None) -> str | None:
        if arg is not None:
            return json.dumps({"tree": {"role": "document", "name": "Fixture page", "children": []}, "lastUsedId": 1})
        return None


class _Browser:
    def __init__(self, boundary: _AgentQLBoundary) -> None:
        self.boundary = boundary
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def new_page(self) -> _BrowserPage:
        return _BrowserPage(self.boundary)


@pytest.fixture
def agentql_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _AgentQLBoundary:
    """Isolate SDK/file credentials, browser activity, and HTTP without replacing query dispatch."""
    agentql_tools()  # Use the real optional-import/Playwright-stealth compatibility boundary.
    sdk_config = importlib.import_module("agentql._core._config").config
    sdk_utils = importlib.import_module("agentql._core._utils")
    sdk_page = importlib.import_module("agentql.ext.playwright.sync_api.playwright_smart_locator").Page
    monkeypatch.setattr(sdk_config, "api_key", None)
    monkeypatch.delenv("AGENTQL_API_KEY", raising=False)
    monkeypatch.setenv("AGENTQL_API_HOST", "https://api.agentql.test")
    config_path = tmp_path / "sdk-config.ini"
    monkeypatch.setattr(sdk_utils, "CONFIG_FILE_PATH", config_path)
    monkeypatch.setattr(sdk_utils, "API_KEY_FILE_PATH_BEFORE_0_5_0", tmp_path / "absent-legacy.ini")
    boundary = _AgentQLBoundary(cast("_SDKConfig", sdk_config), config_path)

    def launch(*, headless: bool) -> _Browser:
        assert headless is False
        browser = _Browser(boundary)
        boundary.browsers.append(browser)
        return browser

    def enter(_context: object) -> SimpleNamespace:
        return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    def exit_context(_context: object, *_args: object) -> None:
        pass

    def page_ready(_page: object, **_kwargs: object) -> None:
        pass

    def send(_adapter: object, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        return boundary.respond(request, **kwargs)

    monkeypatch.setattr(PlaywrightContextManager, "__enter__", enter)
    monkeypatch.setattr(PlaywrightContextManager, "__exit__", exit_context)
    monkeypatch.setattr(sdk_page, "wait_for_page_ready_state", page_ready)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return boundary


def _tool(
    tmp_path: Path,
    stored_key: str | None = None,
    *,
    requester: str = "@alice:example.org",
    query: str = _CUSTOM_QUERY,
) -> AgentQLTools:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )
    credentials = CredentialsManager(tmp_path / "credentials")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="extractor",
        requester_id=requester,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user", "extractor", identity)
    if stored_key is not None:
        save_scoped_credentials(
            "agentql",
            {"api_key": stored_key},
            credentials_manager=credentials,
            worker_target=worker_target,
        )
    return cast(
        "AgentQLTools",
        get_tool_by_name(
            "agentql",
            runtime_paths,
            credentials_manager=credentials,
            tool_config_overrides={"agentql_query": query},
            worker_target=worker_target,
            disable_sandbox_proxy=True,
        ),
    )


@pytest.mark.parametrize("method", ["scrape_website", "custom_scrape_website"])
@pytest.mark.parametrize(
    ("stored_key", "env_key", "sdk_key", "file_key", "expected_key"),
    [
        pytest.param("scoped-key", None, None, None, "scoped-key", id="stored-only"),
        pytest.param(None, "env-key", None, None, "env-key", id="environment-control"),
        pytest.param("scoped-key", "env-key", "sdk-key", "file-key", "scoped-key", id="stored-wins-ambient"),
        pytest.param(None, "env-key", "sdk-key", "file-key", "env-key", id="environment-wins-sdk-config"),
        pytest.param("scoped-key", None, None, "file-key", "scoped-key", id="stored-wins-cli-file"),
    ],
)
def test_agentql_resolved_key_reaches_request_without_shared_auth_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
    method: str,
    stored_key: str | None,
    env_key: str | None,
    sdk_key: str | None,
    file_key: str | None,
    expected_key: str,
) -> None:
    """Both tools send the resolved scoped key, even when unrelated SDK credentials exist."""
    if env_key is not None:
        monkeypatch.setenv("AGENTQL_API_KEY", env_key)
    monkeypatch.setattr(agentql_boundary.sdk_config, "api_key", sdk_key)
    file_text = f"[DEFAULT]\nagentql_api_key = {file_key}\n" if file_key is not None else None
    if file_text is not None:
        agentql_boundary.config_path.write_text(file_text)
    tool = _tool(tmp_path, stored_key)

    result = getattr(tool, method)(_URL)

    assert not result.startswith("Error")
    assert len(agentql_boundary.requests) == 1
    assert agentql_boundary.requests[0].headers["X-API-Key"] == expected_key
    assert agentql_boundary.observed_auth == [(env_key, sdk_key)]
    assert os.environ.get("AGENTQL_API_KEY") == env_key
    assert agentql_boundary.sdk_config.api_key == sdk_key
    if file_text is not None:
        assert agentql_boundary.config_path.read_text() == file_text
    else:
        assert not agentql_boundary.config_path.exists()
    assert all(browser.closed for browser in agentql_boundary.browsers)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            {
                "title": "Unique extracted café title",
                "links": [{"href": "https://example.org/value", "label": "Visit here"}],
                "metadata": {"count": 0, "enabled": False, "missing": None},
            },
            id="nested-values",
        ),
        pytest.param(
            {"title": "", "links": [["first", "first"], [], {"nested": {"values": [1, 2, 1]}}]},
            id="nested-collections-preserve-order-and-duplicates",
        ),
        pytest.param({}, id="empty-object"),
    ],
)
def test_agentql_custom_scrape_preserves_extracted_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
    data: dict[str, object],
) -> None:
    """Custom extraction returns structured values, including nested and empty data."""
    monkeypatch.setenv("AGENTQL_API_KEY", "matching-key")
    agentql_boundary.custom_data = data

    result = _tool(tmp_path, "matching-key").custom_scrape_website(_URL)

    assert json.loads(result) == data
    assert len(agentql_boundary.requests) == 1
    assert all(browser.closed for browser in agentql_boundary.browsers)


def test_agentql_independent_scoped_toolkits_keep_their_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
) -> None:
    """Repeated calls across two requester stores cannot reuse the other store's key."""
    monkeypatch.setenv("AGENTQL_API_KEY", "ambient-env")
    monkeypatch.setattr(agentql_boundary.sdk_config, "api_key", "ambient-sdk")
    first = _tool(tmp_path, "alice-key", requester="@alice:example.org")
    second = _tool(tmp_path, "bob-key", requester="@bob:example.org")

    for tool in (first, second, first):
        assert not tool.scrape_website(_URL).startswith("Error")

    assert [request.headers["X-API-Key"] for request in agentql_boundary.requests] == [
        "alice-key",
        "bob-key",
        "alice-key",
    ]
    assert agentql_boundary.observed_auth == [("ambient-env", "ambient-sdk")] * 3


@pytest.mark.parametrize("method", ["scrape_website", "custom_scrape_website"])
def test_agentql_concurrent_scoped_calls_leave_unrelated_sdk_auth_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
    method: str,
) -> None:
    """Overlapping tool and unrelated SDK queries retain three distinct auth owners."""
    monkeypatch.setenv("AGENTQL_API_KEY", "ambient-env")
    monkeypatch.setattr(agentql_boundary.sdk_config, "api_key", "ambient-sdk")
    first = _tool(tmp_path, "alice-key", requester="@alice:example.org")
    second = _tool(tmp_path, "bob-key", requester="@bob:example.org")
    agentql_boundary.barrier = Barrier(3)
    sdk = importlib.import_module("agentql")

    def unrelated_sdk_query() -> object:
        page = sdk.wrap(_BrowserPage(agentql_boundary))
        page.goto("https://example.org/unrelated")
        return page.query_data("{ text_content[] }")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(getattr(first, method), "https://example.org/alice"),
            pool.submit(getattr(second, method), "https://example.org/bob"),
            pool.submit(unrelated_sdk_query),
        ]
        results = [future.result(timeout=10) for future in futures]

    assert all(not str(result).startswith("Error") for result in results)
    observed = {
        json.loads(cast("str | bytes", request.body))["metadata"]["url"]: request.headers["X-API-Key"]
        for request in agentql_boundary.requests
    }
    assert observed == {
        "https://example.org/alice": "alice-key",
        "https://example.org/bob": "bob-key",
        "https://example.org/unrelated": "ambient-sdk",
    }
    assert agentql_boundary.observed_auth == [("ambient-env", "ambient-sdk")] * 3
    assert os.environ["AGENTQL_API_KEY"] == "ambient-env"
    assert agentql_boundary.sdk_config.api_key == "ambient-sdk"
    assert all(browser.closed for browser in agentql_boundary.browsers)


@pytest.mark.parametrize("method", ["scrape_website", "custom_scrape_website"])
@pytest.mark.parametrize(
    ("status_code", "timeout_error", "error_name"),
    [(401, False, "APIKeyError"), (500, False, "AgentQLServerError"), (200, True, "AgentQLServerTimeoutError")],
)
def test_agentql_retains_sdk_query_errors_and_browser_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
    method: str,
    status_code: int,
    timeout_error: bool,
    error_name: str,
) -> None:
    """Provider rejection and timeout remain SDK-classified extraction errors."""
    monkeypatch.setenv("AGENTQL_API_KEY", "matching-key")
    agentql_boundary.status_code = status_code
    agentql_boundary.timeout_error = timeout_error

    result = getattr(_tool(tmp_path, "matching-key"), method)(_URL)

    assert result.startswith("Error extracting text:")
    assert error_name in result
    assert len(agentql_boundary.requests) == 1
    assert all(browser.closed for browser in agentql_boundary.browsers)


def test_agentql_normal_scrape_retains_text_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
) -> None:
    """The standard text query keeps its existing filtering and deduplication behavior."""
    monkeypatch.setenv("AGENTQL_API_KEY", "matching-key")

    result = _tool(tmp_path, "matching-key").scrape_website(_URL)

    assert set(result.split()) == {"Alpha", "Beta"}
    assert len(result.split()) == 2
    assert len(agentql_boundary.requests) == 1


@pytest.mark.parametrize("method", ["scrape_website", "custom_scrape_website"])
def test_agentql_empty_url_does_not_start_browser(
    tmp_path: Path,
    agentql_boundary: _AgentQLBoundary,
    method: str,
) -> None:
    """The existing URL guard avoids browser and provider work."""
    result = getattr(_tool(tmp_path, "scoped-key"), method)("")

    assert result == "No URL provided"
    assert agentql_boundary.browsers == []
    assert agentql_boundary.requests == []


def test_agentql_missing_custom_query_does_not_start_browser(
    tmp_path: Path,
    agentql_boundary: _AgentQLBoundary,
) -> None:
    """The custom-query guard still runs before browser startup."""
    result = _tool(tmp_path, "scoped-key", query="").custom_scrape_website(_URL)

    assert result.startswith("Custom AgentQL query not provided.")
    assert agentql_boundary.browsers == []
    assert agentql_boundary.requests == []


def test_agentql_sdk_credentials_do_not_replace_missing_toolkit_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agentql_boundary: _AgentQLBoundary,
) -> None:
    """SDK-global or CLI keys alone must not bypass the toolkit constructor's key guard."""
    monkeypatch.setattr(agentql_boundary.sdk_config, "api_key", "sdk-key")
    agentql_boundary.config_path.write_text("[DEFAULT]\nagentql_api_key = file-key\n")

    with pytest.raises(ValueError, match="AGENTQL_API_KEY not set"):
        _tool(tmp_path)

    assert agentql_boundary.browsers == []
    assert agentql_boundary.requests == []
