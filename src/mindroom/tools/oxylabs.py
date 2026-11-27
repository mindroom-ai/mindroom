"""Oxylabs Web Scraper API toolkit for Google SERP, Amazon, and universal scraping."""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import urlparse

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import reject_unexpected_args

logger = get_logger(__name__)

_MAX_DOMAIN_CODE_LEN = 10
_ASIN_LEN = 10
_MAX_PARSED_HTML_LEN = 1000

_OxylabsAction = Literal["search_google", "amazon_product", "search_amazon", "scrape"]
_ACTION_FIELDS: dict[str, str] = {
    "search_google": "query domain_code",
    "amazon_product": "asin domain_code",
    "search_amazon": "query domain_code",
    "scrape": "url render_javascript markdown",
}


def _field(obj: Any, key: str, default: Any = "") -> Any:  # noqa: ANN401
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _extract_google_results(result: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    organic_results: list[dict[str, Any]] = []
    if getattr(result, "content_parsed", None):
        parsed_results = getattr(result.content_parsed, "results", None)
        if parsed_results is not None:
            raw_results = getattr(parsed_results, "raw", {}) or {}
            organic_results = raw_results.get("organic", [])
    if not organic_results:
        raw_content = getattr(result, "content", None)
        if isinstance(raw_content, dict) and "results" in raw_content:
            organic_results = raw_content["results"].get("organic", [])
    return [
        {
            "title": item.get("title", "").strip(),
            "url": item.get("url", "").strip(),
            "description": item.get("desc", "").strip(),
            "position": item.get("pos", 0),
        }
        for item in organic_results
    ]


def _extract_amazon_product(result: Any) -> dict[str, Any]:  # noqa: ANN401
    raw_content = getattr(result, "content", None)
    content = raw_content if isinstance(raw_content, dict) and raw_content else getattr(result, "content_parsed", None)
    if not content:
        return {}
    bullet_points = _field(content, "bullet_points", None)
    return {
        "found": True,
        "title": _field(content, "title", "").strip(),
        "price": _field(content, "price", 0),
        "currency": _field(content, "currency", ""),
        "rating": _field(content, "rating", 0),
        "reviews_count": _field(content, "reviews_count", 0),
        "url": _field(content, "url", ""),
        "description": _field(content, "description", "").strip(),
        "stock_status": _field(content, "stock", "").strip(),
        "brand": _field(content, "brand", "").strip(),
        "images": _field(content, "images", [])[:3],
        "bullet_points": bullet_points[:5] if bullet_points else [],
    }


def _extract_amazon_search_results(result: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    raw_content = getattr(result, "content", None)
    if isinstance(raw_content, dict) and "results" in raw_content:
        items = raw_content["results"].get("organic", [])
    elif getattr(result, "content_parsed", None):
        parsed_results = getattr(result.content_parsed, "results", None)
        items = getattr(parsed_results, "organic", []) or []
    else:
        return []
    return [
        {
            "title": _field(item, "title", "").strip(),
            "asin": _field(item, "asin", "").strip(),
            "price": _field(item, "price", 0),
            "currency": _field(item, "currency", ""),
            "rating": _field(item, "rating", 0),
            "reviews_count": _field(item, "reviews_count", 0),
            "url": _field(item, "url", "").strip(),
            "position": _field(item, "pos", 0),
            "image": _field(item, "image", "").strip(),
        }
        for item in items
    ]


class _OxylabsTools(Toolkit):
    """Google search, Amazon product lookup/search, and universal website scraping via Oxylabs."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        markdown: bool = False,
    ) -> None:
        from oxylabs import RealtimeClient

        self.username = username
        self.password = password
        self.markdown = markdown

        if not self.username or not self.password:
            msg = (
                "No Oxylabs credentials provided. Please set the OXYLABS_USERNAME and OXYLABS_PASSWORD "
                "environment variables or pass them to the OxylabsTools constructor."
            )
            raise ValueError(msg)

        self.client = RealtimeClient(self.username, self.password)
        super().__init__(name="oxylabs_web_scraping", tools=(self.oxylabs,))

    def oxylabs(
        self,
        action: _OxylabsAction,
        query: str | None = None,
        asin: str | None = None,
        url: str | None = None,
        domain_code: str | None = None,
        render_javascript: bool | None = None,
        markdown: bool | None = None,
    ) -> str:
        """Query Google SERP, Amazon products, or universal web scraping via Oxylabs and return JSON.

        - `search_google`: Google search for `query` on `domain_code` (default `"com"`).
        - `amazon_product`: Amazon product details for 10-char `asin` on `domain_code` (default `"com"`).
        - `search_amazon`: Search Amazon products for `query` on `domain_code` (default `"com"`).
        - `scrape`: Scrape `url` (http/https; optional `render_javascript`, default False, `markdown`).
        """
        if (
            unexpected := reject_unexpected_args(
                action,
                _ACTION_FIELDS,
                {
                    "query": query is not None,
                    "asin": asin is not None,
                    "url": url is not None,
                    "domain_code": domain_code is not None,
                    "render_javascript": render_javascript is not None,
                    "markdown": markdown is not None,
                },
            )
        ) is not None:
            return self._error_response(action, unexpected)
        resolved_domain = "com" if domain_code is None else domain_code
        if action == "search_google":
            return self._search_google(query or "", resolved_domain)
        if action == "amazon_product":
            return self._amazon_product(asin or "", resolved_domain)
        if action == "search_amazon":
            return self._search_amazon(query or "", resolved_domain)
        return self._scrape(
            url or "",
            render_javascript=False if render_javascript is None else render_javascript,
            markdown=markdown,
        )

    def _search_google(self, query: str, domain_code: str) -> str:
        if not query or not query.strip():
            return self._error_response("search_google", "Query cannot be empty", {"query": query})
        if len(domain_code) > _MAX_DOMAIN_CODE_LEN:
            return self._error_response("search_google", "Domain must be a valid string (e.g., 'com', 'co.uk')")
        try:
            cleaned_query = query.strip()
            response = self.client.google.scrape_search(query=cleaned_query, domain=domain_code, parse=True)
            search_results = _extract_google_results(response.results[0]) if response.results else []
            return json.dumps({"tool": "search_google", "query": cleaned_query, "results": search_results}, indent=2)
        except Exception as exc:
            logger.exception("oxylabs_google_search_failed")
            return self._error_response("search_google", f"Google search failed: {exc}", {"query": query})

    def _amazon_product(self, asin: str, domain_code: str) -> str:
        if not asin:
            return self._error_response("get_amazon_product", "ASIN is required and must be a string")
        cleaned_asin = asin.strip().upper()
        if len(cleaned_asin) != _ASIN_LEN or not cleaned_asin.isalnum():
            return self._error_response(
                "get_amazon_product",
                f"Invalid ASIN format: {cleaned_asin}. Must be 10 alphanumeric characters (e.g., 'B07FZ8S74R')",
            )
        if len(domain_code) > _MAX_DOMAIN_CODE_LEN:
            return self._error_response(
                "get_amazon_product",
                "Domain must be a valid string (e.g., 'com', 'co.uk')",
            )
        try:
            response = self.client.amazon.scrape_product(query=cleaned_asin, domain=domain_code, parse=True)
            product_info: dict[str, Any] = {"found": False, "asin": cleaned_asin, "domain": f"amazon.{domain_code}"}
            if response.results:
                product_info.update(_extract_amazon_product(response.results[0]))
            return json.dumps(
                {"tool": "get_amazon_product", "asin": cleaned_asin, "product_info": product_info},
                indent=2,
            )
        except Exception as exc:
            logger.exception("oxylabs_amazon_product_failed", asin=cleaned_asin)
            return self._error_response(
                "get_amazon_product",
                f"Amazon product lookup failed: {exc}",
                {"asin": cleaned_asin},
            )

    def _search_amazon(self, query: str, domain_code: str) -> str:
        if not query or not query.strip():
            return self._error_response("search_amazon_products", "Query cannot be empty")
        if len(domain_code) > _MAX_DOMAIN_CODE_LEN:
            return self._error_response(
                "search_amazon_products",
                "Domain must be a valid string (e.g., 'com', 'co.uk')",
            )
        try:
            cleaned_query = query.strip()
            response = self.client.amazon.scrape_search(query=cleaned_query, domain=domain_code, parse=True)
            products = _extract_amazon_search_results(response.results[0]) if response.results else []
            return json.dumps(
                {"tool": "search_amazon_products", "query": cleaned_query, "products": products},
                indent=2,
            )
        except Exception as exc:
            logger.exception("oxylabs_amazon_search_failed")
            return self._error_response("search_amazon_products", f"Amazon search failed: {exc}", {"query": query})

    def _scrape(self, url: str, *, render_javascript: bool, markdown: bool | None) -> str:
        cleaned_url = url.strip() if url else ""
        if not cleaned_url.startswith(("http://", "https://")) or not urlparse(cleaned_url).netloc:
            return self._error_response(
                "scrape_website",
                f"Invalid URL format: {cleaned_url}. Must start with http:// or https://",
            )
        try:
            from oxylabs.utils.types import render

            use_markdown = markdown if markdown is not None else self.markdown
            render_arg = render.HTML if render_javascript else None
            if use_markdown:
                response = self.client.universal.scrape_url(url=cleaned_url, render=render_arg, markdown=True)
            else:
                response = self.client.universal.scrape_url(url=cleaned_url, render=render_arg, parse=True)

            content_info: dict[str, Any] = {
                "url": cleaned_url,
                "javascript_rendered": render_javascript,
                "markdown": use_markdown,
            }
            if response.results:
                result = response.results[0]
                content = result.content
                content_str = str(content) if content else ""
                content_length = len(content_str)
                scraped_content = (
                    content_str
                    if use_markdown or content_length <= _MAX_PARSED_HTML_LEN
                    else content_str[:_MAX_PARSED_HTML_LEN]
                )
                if content:
                    content_info["scraped"] = True
                content_info.update(
                    {
                        "status_code": getattr(result, "status_code", None),
                        "content_length": content_length,
                        "content": scraped_content if use_markdown else scraped_content.strip(),
                        "has_content": content_length > 0,
                    },
                )
            return json.dumps({"tool": "scrape_website", "url": cleaned_url, "content_info": content_info}, indent=2)
        except Exception as exc:
            logger.exception("oxylabs_scrape_website_failed", url=cleaned_url)
            return self._error_response("scrape_website", f"Website scraping failed: {exc}", {"url": cleaned_url})

    @staticmethod
    def _error_response(tool_name: str, error_message: str, context: dict[str, Any] | None = None) -> str:
        return json.dumps({"tool": tool_name, "error": error_message, "context": context or {}}, indent=2)


@register_tool_with_metadata(
    name="oxylabs",
    file_access=ToolFileAccess.NONE,
    display_name="Oxylabs",
    description="Powerful web scraping capabilities including SERP, Amazon product data, and universal web scraping",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,  # requires username and password
    setup_type=SetupType.API_KEY,  # uses username/password credentials
    icon="FaGlobe",
    icon_color="text-blue-600",  # Web/scraping theme
    config_fields=[
        ConfigField(
            name="username",
            env=("OXYLABS_USERNAME",),
            label="Username",
            type="text",
            required=False,
            placeholder="your_oxylabs_username",
            description="Oxylabs dashboard username",
        ),
        ConfigField(
            name="password",
            env=("OXYLABS_PASSWORD",),
            label="Password",
            type="password",
            required=False,
            placeholder="your_oxylabs_password",
            description="Oxylabs dashboard password",
        ),
        bool_config_field(
            "markdown",
            "Markdown",
            default=False,
            description="Return scraped website content as Markdown instead of parsed HTML",
        ),
    ],
    dependencies=["oxylabs"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#oxylabs",
    function_names=("oxylabs",),
    action_scoped_grants=("oxylabs",),
)
def oxylabs_tools() -> type[Toolkit]:
    """Return Oxylabs tools for web scraping and data extraction."""
    return _OxylabsTools
