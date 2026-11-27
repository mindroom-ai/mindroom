"""Bright Data web scraping, screenshot, SERP, and structured dataset toolkit."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote
from uuid import uuid4

from mindroom.llm.media import Image
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
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tools.native_helpers import require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_BrightDataAction = Literal["scrape_markdown", "screenshot", "search_engine", "web_data_feed"]
_ACTION_FIELDS: dict[str, str] = {
    "scrape_markdown": "url",
    "screenshot": "url",
    "search_engine": "query engine num_results language country_code",
    "web_data_feed": "source_type url num_of_reviews",
}

_DATASETS: dict[str, str] = {
    "amazon_product": "gd_l7q7dkf244hwjntr0",
    "amazon_product_reviews": "gd_le8e811kzy4ggddlq",
    "amazon_product_search": "gd_lwdb4vjm1ehb499uxs",
    "walmart_product": "gd_l95fol7l1ru6rlo116",
    "walmart_seller": "gd_m7ke48w81ocyu4hhz0",
    "ebay_product": "gd_ltr9mjt81n0zzdk1fb",
    "homedepot_products": "gd_lmusivh019i7g97q2n",
    "zara_products": "gd_lct4vafw1tgx27d4o0",
    "etsy_products": "gd_ltppk0jdv1jqz25mz",
    "bestbuy_products": "gd_ltre1jqe1jfr7cccf",
    "linkedin_person_profile": "gd_l1viktl72bvl7bjuj0",
    "linkedin_company_profile": "gd_l1vikfnt1wgvvqz95w",
    "linkedin_job_listings": "gd_lpfll7v5hcqtkxl6l",
    "linkedin_posts": "gd_lyy3tktm25m4avu764",
    "linkedin_people_search": "gd_m8d03he47z8nwb5xc",
    "crunchbase_company": "gd_l1vijqt9jfj7olije",
    "zoominfo_company_profile": "gd_m0ci4a4ivx3j5l6nx",
    "instagram_profiles": "gd_l1vikfch901nx3by4",
    "instagram_posts": "gd_lk5ns7kz21pck8jpis",
    "instagram_reels": "gd_lyclm20il4r5helnj",
    "instagram_comments": "gd_ltppn085pokosxh13",
    "facebook_posts": "gd_lyclm1571iy3mv57zw",
    "facebook_marketplace_listings": "gd_lvt9iwuh6fbcwmx1a",
    "facebook_company_reviews": "gd_m0dtqpiu1mbcyc2g86",
    "facebook_events": "gd_m14sd0to1jz48ppm51",
    "tiktok_profiles": "gd_l1villgoiiidt09ci",
    "tiktok_posts": "gd_lu702nij2f790tmv9h",
    "tiktok_shop": "gd_m45m1u911dsa4274pi",
    "tiktok_comments": "gd_lkf2st302ap89utw5k",
    "google_maps_reviews": "gd_luzfs1dn2oa0teb81",
    "google_shopping": "gd_ltppk50q18kdw67omz",
    "google_play_store": "gd_lsk382l8xei8vzm4u",
    "apple_app_store": "gd_lsk9ki3u2iishmwrui",
    "reuter_news": "gd_lyptx9h74wtlvpnfu",
    "github_repository_file": "gd_lyrexgxc24b3d4imjt",
    "yahoo_finance_business": "gd_lmrpz3vxmz972ghd7",
    "x_posts": "gd_lwxkxvnf1cynvib9co",
    "zillow_properties_listing": "gd_lfqkr8wm13ixtbd8f5",
    "booking_hotel_listings": "gd_m5mbdl081229ln6t4a",
    "youtube_profiles": "gd_lk538t2k2p1k3oos71",
    "youtube_comments": "gd_lk9q0ew71spt1mxywf",
    "reddit_posts": "gd_lvz8ah06191smkebj4",
    "youtube_videos": "gd_lk56epmy2i5g7lzu0k",
}


class _BrightDataTools(Toolkit):
    """Web scraping, screenshots, search engines, and structured data feeds via Bright Data."""

    def __init__(
        self,
        api_key: str | None = None,
        enable_scrape_markdown: bool = True,
        enable_screenshot: bool = True,
        enable_search_engine: bool = True,
        enable_web_data_feed: bool = True,
        all: bool = False,  # noqa: A002
        serp_zone: str = "serp_api",
        web_unlocker_zone: str = "web_unlocker1",
        verbose: bool = False,
        timeout: int = 600,
    ) -> None:
        self.api_key = api_key
        if not self.api_key:
            logger.error("brightdata_api_key_missing")
            msg = (
                "No Bright Data API key provided. Please provide an api_key or set the "
                "BRIGHT_DATA_API_KEY environment variable."
            )
            raise ValueError(msg)

        self.verbose = verbose
        self.timeout = timeout
        self.endpoint = "https://api.brightdata.com/request"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self.web_unlocker_zone = web_unlocker_zone
        self.serp_zone = serp_zone

        toggles: dict[_BrightDataAction, bool] = {
            "scrape_markdown": all or enable_scrape_markdown,
            "screenshot": all or enable_screenshot,
            "search_engine": all or enable_search_engine,
            "web_data_feed": all or enable_web_data_feed,
        }
        self._enabled_actions: tuple[_BrightDataAction, ...] = tuple(
            action for action, enabled in toggles.items() if enabled
        )
        super().__init__(name="brightdata_tools", tools=(self.brightdata,) if self._enabled_actions else ())

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``brightdata`` schema and parameters to enabled actions."""
        if function.name == "brightdata":
            return restrict_actions(function, self._enabled_actions, action_fields=_ACTION_FIELDS)
        return function

    def _make_request(self, payload: dict[str, Any]) -> str:
        import requests

        if self.verbose:
            logger.info("brightdata_request", url=payload["url"])
        response = requests.post(
            self.endpoint,
            headers=self.headers,
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            msg = f"Failed to scrape: {response.status_code} - {response.text}"
            raise RuntimeError(msg)
        return response.text

    def brightdata(
        self,
        action: _BrightDataAction,
        url: str | None = None,
        query: str | None = None,
        engine: str = "google",
        num_results: int = 10,
        language: str | None = None,
        country_code: str | None = None,
        source_type: str | None = None,
        num_of_reviews: int | None = None,
    ) -> str | ToolValue:
        """Scrape pages, capture screenshots, run SERP searches, or query structured datasets via Bright Data.

        - `scrape_markdown`: Scrape `url` as Markdown.
        - `screenshot`: Capture PNG screenshot of `url` as an image artifact.
        - `search_engine`: Search `query` on `engine` (google, bing, yandex; optional `num_results`, `language`, `country_code`).
        - `web_data_feed`: Fetch structured JSON dataset for `source_type` and `url` (optional `num_of_reviews` for Facebook reviews).
        """
        if (disabled := require_enabled_action(action, self._enabled_actions)) is not None:
            return disabled
        if action == "scrape_markdown":
            return self._scrape_markdown(url or "")
        if action == "screenshot":
            return self._screenshot(url or "")
        if action == "search_engine":
            return self._search_engine(query or "", engine, num_results, language, country_code)
        return self._web_data_feed(source_type or "", url or "", num_of_reviews)

    def _scrape_markdown(self, url: str) -> str:
        if not self.api_key:
            return "Please provide a Bright Data API key"
        if not url:
            return "Please provide a URL to scrape"
        try:
            logger.info("brightdata_scrape_markdown", url=url)
            payload = {
                "url": url,
                "zone": self.web_unlocker_zone,
                "format": "raw",
                "data_format": "markdown",
            }
            return self._make_request(payload)
        except Exception as exc:
            return f"Error scraping URL {url}: {exc}"

    def _screenshot(self, url: str) -> ToolValue:
        if not self.api_key:
            return ToolValue(content="Please provide a Bright Data API key")
        if not url:
            return ToolValue(content="Please provide a URL to screenshot")
        try:
            import requests

            logger.info("brightdata_screenshot", url=url)
            payload = {
                "url": url,
                "zone": self.web_unlocker_zone,
                "format": "raw",
                "data_format": "screenshot",
            }
            response = requests.post(
                self.endpoint,
                headers=self.headers,
                data=json.dumps(payload),
                timeout=self.timeout,
            )
            if response.status_code != 200:
                return ToolValue(
                    content=f"Error taking screenshot of {url}: Error {response.status_code}: {response.text}",
                )

            media_id = str(uuid4())
            image_artifact = Image(
                id=media_id,
                content=response.content,
                mime_type="image/png",
                original_prompt=f"Screenshot of {url}",
            )
            logger.debug("brightdata_screenshot_captured", media_id=media_id)
            return ToolValue(
                content=f"Screenshot captured and added as artifact with ID: {media_id}",
                images=(image_artifact,),
            )
        except Exception as exc:
            return ToolValue(content=f"Error taking screenshot of {url}: {exc}")

    def _search_engine(
        self,
        query: str,
        engine: str,
        num_results: int,
        language: str | None,
        country_code: str | None,
    ) -> str:
        if not self.api_key:
            return "Please provide a Bright Data API key"
        if not query:
            return "Please provide a query to search for"

        encoded_query = quote(query)
        base_urls = {
            "google": f"https://www.google.com/search?q={encoded_query}",
            "bing": f"https://www.bing.com/search?q={encoded_query}",
            "yandex": f"https://yandex.com/search/?text={encoded_query}",
        }
        if engine not in base_urls:
            return f"Unsupported search engine: {engine}. Use 'google', 'bing', or 'yandex'"

        search_url = base_urls[engine]
        if engine == "google":
            params: list[str] = []
            if language:
                params.append(f"hl={language}")
            if country_code:
                params.append(f"gl={country_code}")
            if num_results:
                params.append(f"num={num_results}")
            if params:
                search_url += "&" + "&".join(params)

        try:
            logger.info("brightdata_search_engine", engine=engine, query=query)
            payload = {
                "url": search_url,
                "zone": self.serp_zone,
                "format": "raw",
                "data_format": "markdown",
            }
            return self._make_request(payload)
        except Exception as exc:
            return f"Error searching for query {query}: {exc}"

    def _web_data_feed(self, source_type: str, url: str, num_of_reviews: int | None) -> str:
        if not self.api_key:
            return "Please provide a Bright Data API key"
        if not url:
            return "Please provide a URL to retrieve data from"
        if source_type not in _DATASETS:
            valid_sources = ", ".join(_DATASETS.keys())
            return f"Invalid source_type: {source_type}. Valid options are: {valid_sources}"

        try:
            import requests

            logger.info("brightdata_web_data_feed", source_type=source_type, url=url)
            request_data: dict[str, Any] = {"url": url}
            if source_type == "facebook_company_reviews" and num_of_reviews is not None:
                request_data["num_of_reviews"] = str(num_of_reviews)

            trigger_response = requests.post(
                "https://api.brightdata.com/datasets/v3/trigger",
                params={"dataset_id": _DATASETS[source_type], "include_errors": "true"},
                headers=self.headers,
                json=[request_data],
                timeout=self.timeout,
            )
            trigger_data = trigger_response.json()
            snapshot_id = trigger_data.get("snapshot_id")
            if not snapshot_id:
                return "No snapshot ID returned from trigger request"

            return self._poll_snapshot(snapshot_id, source_type)
        except Exception as exc:
            return f"Error retrieving {source_type} data from {url}: {exc}"

    def _poll_snapshot(self, snapshot_id: str, source_type: str) -> str:
        import requests

        attempts = 0
        max_attempts = self.timeout
        while attempts < max_attempts:
            try:
                snapshot_response = requests.get(
                    f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}",
                    params={"format": "json"},
                    headers=self.headers,
                    timeout=30,
                )
                snapshot_data = snapshot_response.json()
                if isinstance(snapshot_data, dict) and snapshot_data.get("status") == "running":
                    attempts += 1
                    time.sleep(1)
                    continue
                return json.dumps(snapshot_data)
            except Exception:
                attempts += 1
                time.sleep(1)
        return f"Timeout after {max_attempts} seconds waiting for {source_type} data"


@register_tool_with_metadata(
    name="brightdata",
    file_access=ToolFileAccess.NONE,
    display_name="BrightData",
    description="Web scraping, search engine queries, screenshots, and structured data extraction",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSpider",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("BRIGHT_DATA_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("enable_scrape_markdown", "Enable Scrape Markdown"),
        bool_config_field("enable_screenshot", "Enable Screenshot"),
        bool_config_field("enable_search_engine", "Enable Search Engine"),
        bool_config_field("enable_web_data_feed", "Enable Web Data Feed"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="serp_zone",
            env=("BRIGHT_DATA_SERP_ZONE",),
            label="Serp Zone",
            type="text",
            required=False,
            default="serp_api",
        ),
        ConfigField(
            name="web_unlocker_zone",
            env=("BRIGHT_DATA_WEB_UNLOCKER_ZONE",),
            label="Web Unlocker Zone",
            type="text",
            required=False,
            default="web_unlocker1",
        ),
        bool_config_field("verbose", "Verbose", default=False),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=600,
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#brightdata",
    function_names=("brightdata",),
    action_scoped_grants=("brightdata",),
)
def brightdata_tools() -> type[Toolkit]:
    """Return BrightData tools for web scraping and data extraction."""
    return _BrightDataTools
