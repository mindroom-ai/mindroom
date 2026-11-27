"""AgentQL AI-powered web scraping toolkit with scoped credentials and destination proxy."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.worker_computer.browser_proxy import (
    PROXIED_WEBRTC_ONLY_ARG,
    RELAY_ONLY_PROXY_BYPASS,
    BrowserDestinationProxy,
    browser_egress,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

    from agentql._core._syntax.node import ContainerNode
    from agentql._core._typing import ResponseMode
    from playwright.async_api import Page as AsyncPage
    from playwright.sync_api import Page as SyncPage

    from mindroom.constants import RuntimePaths
    from mindroom.worker_computer.browser_proxy import BrowserEgress

logger = get_logger(__name__)

_credentialed_page_cls: type[Any] | None = None


def _ensure_agentql_playwright_stealth_compat() -> None:
    """Expose the legacy stealth helpers AgentQL imports when crawl4ai installs playwright-stealth 2.x."""
    playwright_stealth = importlib.import_module("playwright_stealth")
    if {"StealthConfig", "stealth_async", "stealth_sync"} <= vars(playwright_stealth).keys():
        return

    _install_agentql_playwright_stealth_compat(playwright_stealth)


def _install_agentql_playwright_stealth_compat(playwright_stealth: ModuleType) -> None:
    """Install the legacy playwright-stealth exports still imported by AgentQL."""
    from playwright_stealth.core import BrowserType, StealthConfig
    from playwright_stealth.properties import Properties

    def _combine_scripts(config: StealthConfig | None) -> tuple[Properties, str]:
        properties = Properties(browser_type=config.browser_type if config else BrowserType.CHROME)
        scripts = "\n".join((config or StealthConfig()).enabled_scripts(properties))
        return properties, scripts

    async def _stealth_async(page: AsyncPage, config: StealthConfig | None = None) -> None:
        properties, script = _combine_scripts(config)
        await page.set_extra_http_headers(properties.as_dict()["header"])
        await page.add_init_script(script)

    def _stealth_sync(page: SyncPage, config: StealthConfig | None = None) -> None:
        properties, script = _combine_scripts(config)
        page.set_extra_http_headers(properties.as_dict()["header"])
        page.add_init_script(script)

    namespace = vars(playwright_stealth)
    namespace["StealthConfig"] = StealthConfig
    namespace["stealth_async"] = _stealth_async
    namespace["stealth_sync"] = _stealth_sync


def _require_agentql() -> None:
    """Ensure ``agentql`` and ``playwright`` are installed and stealth helpers are compatible."""
    try:
        _ensure_agentql_playwright_stealth_compat()
        import agentql  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:
        msg = "`agentql` not installed. Please install using `pip install agentql`"
        raise ImportError(msg) from exc


def _agentql_api_host(runtime_paths: RuntimePaths) -> str:
    """Resolve the AgentQL host from the runtime environment and its ``.env``."""
    from agentql._core._api_constants import AGENTQL_SERVER_URL_ENV_VAR, DEFAULT_SERVER_URL

    return (
        runtime_paths.env_value("AGENTQL_API_HOST")
        or runtime_paths.env_value(AGENTQL_SERVER_URL_ENV_VAR)
        or DEFAULT_SERVER_URL
    )


def _query_agentql_server(
    *,
    api_host: str,
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
    """Send a scoped AgentQL request, rejecting redirects and preserving JSON parsing failures."""
    import requests
    from agentql import AgentQLServerError, AgentQLServerTimeoutError, APIKeyError
    from agentql._core._api_constants import (
        DEFAULT_REQUEST_ORIGIN,
        GET_AGENTQL_DATA_ENDPOINT,
        GET_AGENTQL_ELEMENT_ENDPOINT,
    )
    from agentql._core._errors import API_KEY_NOT_SET_MESSAGE
    from agentql._core._utils import raise_401_error

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
            api_host + endpoint,
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
        if (
            isinstance(error, requests.exceptions.HTTPError)
            and error.response is not None
            and error.response.status_code == 401
        ):
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


def _get_credentialed_page_cls() -> type[Any]:
    """Lazily define and return the AgentQL Page subclass that binds host and API key per page."""
    global _credentialed_page_cls
    if _credentialed_page_cls is not None:
        return _credentialed_page_cls

    _require_agentql()
    from agentql import QueryParser
    from agentql.ext.playwright.sync_api._utils_sync import get_accessibility_tree
    from agentql.ext.playwright.sync_api.playwright_smart_locator import Page as AgentQLPage

    class _CredentialedPage(AgentQLPage):
        """Retain SDK page behavior while binding the request host and authentication to this page."""

        _api_host: str
        _api_key: str | None

        @classmethod
        def for_service(cls, page: SyncPage, *, api_host: str, api_key: str | None) -> _CredentialedPage:
            """Initialize the SDK's page monitor and attach the toolkit's AgentQL host and resolved key."""
            wrapped = cls.create(page)
            wrapped._api_host = api_host
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
                api_host=self._api_host,
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

    _credentialed_page_cls = _CredentialedPage
    return _credentialed_page_cls


def _credentialed_page_for_service(page: Any, *, api_host: str, api_key: str | None) -> Any:  # noqa: ANN401
    """Wrap a Playwright page with per-page AgentQL host and API key bindings."""
    return _get_credentialed_page_cls().for_service(page, api_host=api_host, api_key=api_key)


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


class _AgentQLTools(Toolkit):
    """AgentQL web scraping toolkit with scoped credentials, browser destination relay, and lossless JSON results."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        api_key: str | None = None,
        enable_scrape_website: bool = True,
        enable_custom_scrape_website: bool = False,
        all: bool = False,  # noqa: A002
        agentql_query: str = "",
    ) -> None:
        _require_agentql()
        if not api_key:
            msg = "AGENTQL_API_KEY not set: set the tool's api_key, or AGENTQL_API_KEY in the environment or `.env`."
            raise ValueError(msg)

        self._runtime_paths = runtime_paths
        self.api_key = api_key
        self.agentql_query = agentql_query

        tools: list[Callable[..., Any]] = []
        if all or enable_scrape_website:
            tools.append(self.scrape_website)
        if (all or enable_custom_scrape_website or agentql_query) and agentql_query:
            logger.info("Custom AgentQL query provided. Registering custom scrape function.")
            tools.append(self.custom_scrape_website)

        super().__init__(name="agentql_tools", tools=tools)

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

    def _scrape(self, url: str, query: str, *, custom: bool) -> str:
        from playwright.sync_api import sync_playwright

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
                page = _credentialed_page_for_service(
                    browser.new_page(),
                    api_host=_agentql_api_host(self._runtime_paths),
                    api_key=self.api_key,
                )
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


@register_tool_with_metadata(
    name="agentql",
    file_access=ToolFileAccess.NONE,
    display_name="AgentQL",
    description="AI-powered web scraping and data extraction from websites",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSpider",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("AGENTQL_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("enable_scrape_website", "Enable Scrape Website"),
        bool_config_field("enable_custom_scrape_website", "Enable Custom Scrape Website", default=False),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="agentql_query",
            label="Agentql Query",
            type="text",
            required=False,
            default="",
        ),
    ],
    dependencies=["agentql", "playwright"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#agentql",
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    function_names=("custom_scrape_website", "scrape_website"),
)
def agentql_tools() -> type[Toolkit]:
    """Return AgentQL tools for AI-powered web scraping."""
    _require_agentql()
    return _AgentQLTools
