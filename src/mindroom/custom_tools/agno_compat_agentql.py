"""Scoped AgentQL request credentials and lossless custom query results."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

import requests
from agentql import AgentQLServerError, AgentQLServerTimeoutError, APIKeyError, QueryParser
from agentql._core._api_constants import (
    DEFAULT_REQUEST_ORIGIN,
    GET_AGENTQL_DATA_ENDPOINT,
    GET_AGENTQL_ELEMENT_ENDPOINT,
    SERVICE_URL,
)
from agentql._core._errors import API_KEY_NOT_SET_MESSAGE
from agentql._core._utils import raise_401_error
from agentql.ext.playwright.sync_api._utils_sync import get_accessibility_tree
from agentql.ext.playwright.sync_api.playwright_smart_locator import Page as AgentQLPage
from agno.tools.agentql import AgentQLTools as AgnoAgentQLTools
from playwright.sync_api import sync_playwright

from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url
from mindroom.worker_computer.browser_proxy import (
    PROXIED_WEBRTC_ONLY_ARG,
    RELAY_ONLY_PROXY_BYPASS,
    BrowserDestinationProxy,
    browser_egress,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any, Self

    from agentql._core._syntax.node import ContainerNode
    from agentql._core._typing import ResponseMode
    from playwright.sync_api import Page as PlaywrightPage

    from mindroom.constants import RuntimePaths
    from mindroom.worker_computer.browser_proxy import BrowserEgress

logger = get_logger(__name__)

# AGNO_COMPAT: AgentQLTools does not pass its resolved key to SDK queries.
# Reason: Agno 3.0.9 stores api_key but AgentQL 1.18.1 resolves auth from shared
# SDK, environment, or CLI-file state. Its Page has no per-call credential hook.
# Keep the private page dispatch and HTTP boundary local to one toolkit key;
# retain SDK query parsing, readiness, parameters, and typed transport failures.
# Deliberately reject redirects and preserve parsing errors from malformed
# successful responses instead of inheriting the SDK's generic server error.
# Upstream issue: Tracking gap; a resolved-key handoff and a per-request or
# per-page SDK authentication extension point are needed. No issue identified.
# Upstream PR: None identified for this credential handoff.
# Remove when: Agno forwards its resolved key through an isolated AgentQL SDK
# request API; retain scoped-key precedence, redirect rejection, parse diagnostics,
# and concurrent-call isolation.
# Coverage: tests/test_agentql_tools.py::test_agentql_resolved_key_reaches_request_without_shared_auth_mutation;
# tests/test_agentql_tools.py::test_agentql_concurrent_scoped_calls_leave_unrelated_sdk_auth_untouched;
# tests/test_agentql_tools.py::test_agentql_retains_sdk_query_errors_and_browser_cleanup;
# tests/test_agentql_tools.py::test_agentql_redirect_does_not_forward_scoped_request;
# tests/test_agentql_tools.py::test_agentql_malformed_success_retains_parse_error.


def _query_agentql_server(
    *,
    api_key: str | None,
    query: str,
    accessibility_tree: dict[str, Any],
    timeout: int,
    page_url: str,
    mode: ResponseMode,
    query_data: bool,
    experimental_query_elements_enabled: bool,
    **kwargs: object,
) -> dict[str, Any]:
    """Send a scoped request, rejecting redirects and preserving JSON parsing failures."""
    if not api_key:
        raise APIKeyError(API_KEY_NOT_SET_MESSAGE)

    try:
        request_data: dict[str, Any] = {
            "query": query,
            "accessibility_tree": accessibility_tree,
            "metadata": {
                "url": page_url,
                "experimental_query_elements_enabled": experimental_query_elements_enabled,
            },
            "params": {"mode": mode},
            "request_origin": kwargs.get("request_origin", DEFAULT_REQUEST_ORIGIN),
        }
        if "metadata" in kwargs:
            request_data["metadata"] |= cast("dict[str, Any]", kwargs["metadata"])
        endpoint = GET_AGENTQL_DATA_ENDPOINT if query_data else GET_AGENTQL_ELEMENT_ENDPOINT
        response = requests.post(
            os.getenv("AGENTQL_API_HOST", SERVICE_URL) + endpoint,
            json=request_data,
            headers={"X-API-Key": api_key},
            timeout=timeout,
            allow_redirects=False,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        request_id = error.response.headers.get("X_REQUEST_ID") if error.response is not None else None
        if isinstance(error, requests.exceptions.ReadTimeout):
            raise AgentQLServerTimeoutError from error
        if isinstance(error, requests.exceptions.HTTPError) and error.response.status_code == 401:
            raise_401_error(error, request_id)
        error_code = error.response.status_code if error.response is not None else None
        server_error = error.response.content.decode("utf-8") if error.response is not None else None
        if server_error:
            try:
                server_error_json = json.loads(server_error)
                if isinstance(server_error_json, dict):
                    server_error = server_error_json.get("detail")
            except ValueError:
                raise AgentQLServerError(server_error, error_code, request_id) from error
        raise AgentQLServerError(server_error, error_code, request_id) from error

    if 300 <= response.status_code < 400:
        message = "AgentQL query endpoint returned an unexpected redirect."
        raise AgentQLServerError(message, response.status_code, response.headers.get("X_REQUEST_ID"))
    payload = response.json()
    logger.debug("AgentQL query completed", request_id=payload["request_id"])
    return payload["response"]


class _CredentialedPage(AgentQLPage):
    """Retain SDK page behavior while binding request authentication to this page."""

    _api_key: str | None

    @classmethod
    def with_api_key(cls, page: PlaywrightPage, api_key: str | None) -> Self:
        """Initialize the SDK's page monitor and attach the toolkit's resolved key."""
        wrapped = cls.create(page)
        wrapped._api_key = api_key
        return wrapped

    def _execute_query(
        self,
        query: str,
        timeout: int,
        wait_for_network_idle: bool,
        include_hidden: bool,
        mode: ResponseMode,
        is_data_query: bool,
        accessibility_tree: dict[str, Any] | None = None,
        experimental_query_elements_enabled: bool = False,
        **kwargs: object,
    ) -> tuple[dict[str, Any], ContainerNode]:
        query_tree = QueryParser(query).parse()
        self.wait_for_page_ready_state(wait_for_network_idle=wait_for_network_idle)
        if not accessibility_tree:
            accessibility_tree = get_accessibility_tree(self._page, include_hidden=include_hidden)
        response = _query_agentql_server(
            api_key=self._api_key,
            query=query,
            accessibility_tree=accessibility_tree,
            timeout=timeout,
            page_url=self._page.url,
            mode=mode,
            query_data=is_data_query,
            experimental_query_elements_enabled=experimental_query_elements_enabled,
            **kwargs,
        )
        self._set_debug_info(last_query=query, last_response=response, last_accessibility_tree=accessibility_tree)
        return response, query_tree


@contextmanager
def _destination_relay(egress: BrowserEgress) -> Iterator[str]:
    """Run a browser destination relay on its own event loop for one synchronous browser session."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="mindroom-agentql-relay", daemon=True)
    thread.start()
    relay = BrowserDestinationProxy(egress=egress)
    try:
        asyncio.run_coroutine_threadsafe(relay.start(), loop).result()
        yield relay.endpoint
    finally:
        asyncio.run_coroutine_threadsafe(relay.close(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


class MindRoomAgentQLTools(AgnoAgentQLTools):
    """Preserve Agno registration with scoped requests and complete custom results."""

    def __init__(self, runtime_paths: RuntimePaths, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(**kwargs)
        self._runtime_paths = runtime_paths

    def scrape_website(self, url: str) -> str:
        """Scrape all text content from a website using AgentQL.

        Args:
            url: The URL of the website to scrape.

        Returns:
            Extracted text content or an error message.

        """
        if not url:
            return "No URL provided"
        return self._scrape(url, "{ text_content[] }", custom=False)

    # AGNO_COMPAT: AgentQLTools custom scraping discards extracted values.
    # Reason: Agno 3.0.9 iterates a response dict and joins only its keys; nested
    # values, lists, and even the requested page title disappear from the result.
    # Upstream issue: Tracking gap; no matching custom-result formatting issue identified.
    # Upstream PR: None identified for lossless custom query results.
    # Remove when: Agno preserves every custom response value and nested collection;
    # retain JSON results and the independent scoped-credential request behavior.
    # Coverage: tests/test_agentql_tools.py::test_agentql_custom_scrape_preserves_extracted_values.
    def custom_scrape_website(self, url: str) -> str:
        """Scrape a website using a custom AgentQL query.

        Args:
            url: The URL of the website to scrape.

        Returns:
            Extracted data as JSON or an error message.

        """
        if not url:
            return "No URL provided"
        if self.agentql_query == "":
            return "Custom AgentQL query not provided. Please provide a custom AgentQL query."
        return self._scrape(url, self.agentql_query, custom=True)

    # AGNO_COMPAT: AgentQLTools launches its browser without a destination policy.
    # Reason: Agno 3.0.9 scrape_website and custom_scrape_website launch Chromium with no
    # proxy and navigate to the model-chosen URL, so file:// paths, redirects, and subresources
    # reached local files and loopback, private, and metadata targets from the MindRoom process.
    # Upstream issue: Tracking gap; no matching issue identified on October 1, 2026.
    # Upstream PR: None identified for a browser launch or navigation hook.
    # Remove when: AgentQLTools accepts caller-supplied launch options or a page factory;
    # retain URL validation before launch and dial-time validation of every connection.
    # Coverage: tests/test_agentql_tools.py::test_agentql_rejects_unsafe_targets_before_starting_browser;
    # tests/test_agentql_tools.py::test_agentql_browser_connects_only_through_destination_relay.
    def _scrape(self, url: str, query: str, *, custom: bool) -> str:
        try:
            # The relay below resolves and validates every address Chromium dials.
            validate_server_fetch_url(url, resolve_hostnames=False)
        except ServerFetchUrlError as error:
            return f"Error: {error}"
        try:
            egress = browser_egress(
                self._runtime_paths.process_env,
                os.environ,
                egress_control=self._runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]),
            )
            # Page routes see neither redirects nor the addresses Chromium resolves itself, so the relay is
            # Chromium's only proxy and validates every connection, redirects and subresources included, at dial time.
            with (
                _destination_relay(egress) as relay_endpoint,
                sync_playwright() as playwright,
                playwright.chromium.launch(
                    headless=False,
                    proxy={"server": relay_endpoint, "bypass": RELAY_ONLY_PROXY_BYPASS},
                    args=[PROXIED_WEBRTC_ONLY_ARG],
                ) as browser,
            ):
                page = _CredentialedPage.with_api_key(browser.new_page(), self.api_key)
                page.goto(url)
                try:
                    response = page.query_data(query)
                    if isinstance(response, dict):
                        if custom:
                            return json.dumps(response, ensure_ascii=False)
                        if "text_content" in response:
                            text_items = [item for item in response["text_content"] if item and item.strip()]
                            return " ".join(set(text_items))
                except Exception as error:
                    return f"Error extracting text: {error}"
        except Exception as error:
            return f"Error launching browser: {error}"
        return "No text content found"
