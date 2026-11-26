"""Native Unsplash photo search and retrieval toolkit."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit

logger = get_logger(__name__)

UnsplashAction = Literal["search", "get", "random", "download"]
UnsplashOrientation = Literal["landscape", "portrait", "squarish"]
UnsplashColor = Literal[
    "black_and_white",
    "black",
    "white",
    "yellow",
    "orange",
    "red",
    "purple",
    "magenta",
    "green",
    "teal",
    "blue",
]

_BASE_URL = "https://api.unsplash.com"


def _format_photo(photo: dict[str, Any]) -> dict[str, Any]:
    urls = photo.get("urls") or {}
    user = photo.get("user") or {}
    links = photo.get("links") or {}
    return {
        "id": photo.get("id"),
        "description": photo.get("description") or photo.get("alt_description"),
        "width": photo.get("width"),
        "height": photo.get("height"),
        "color": photo.get("color"),
        "created_at": photo.get("created_at"),
        "urls": {key: urls.get(key) for key in ("raw", "full", "regular", "small", "thumb")},
        "author": {
            "name": user.get("name"),
            "username": user.get("username"),
            "profile_url": (user.get("links") or {}).get("html"),
        },
        "links": {"html": links.get("html"), "download": links.get("download")},
        "likes": photo.get("likes"),
        "tags": [
            tag.get("title") for tag in (photo.get("tags") or [])[:5] if isinstance(tag, dict) and tag.get("title")
        ],
    }


class UnsplashTools(Toolkit):
    """Search and retrieve royalty-free photos from Unsplash."""

    def __init__(
        self,
        access_key: str | None = None,
        enable_search_photos: bool = True,
        enable_get_photo: bool = True,
        enable_get_random_photo: bool = True,
        enable_download_photo: bool = False,
        all: bool = False,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        self.access_key = access_key or os.getenv("UNSPLASH_ACCESS_KEY")
        if not self.access_key:
            logger.warning("No Unsplash API key provided; set UNSPLASH_ACCESS_KEY")
        self.timeout = timeout
        self._enabled_actions: frozenset[str] = frozenset(
            action
            for action, enabled in (
                ("search", all or enable_search_photos),
                ("get", all or enable_get_photo),
                ("random", all or enable_get_random_photo),
                ("download", all or enable_download_photo),
            )
            if enabled
        )
        super().__init__(
            name="unsplash",
            tools=[self.unsplash] if self._enabled_actions else [],
        )

    def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
        response = httpx.get(
            f"{_BASE_URL}{endpoint}",
            params=params,
            headers={"Authorization": f"Client-ID {self.access_key}", "Accept-Version": "v1"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def unsplash(
        self,
        action: UnsplashAction,
        query: str | None = None,
        photo_id: str | None = None,
        count: int | None = None,
        page: int = 1,
        orientation: UnsplashOrientation | None = None,
        color: UnsplashColor | None = None,
    ) -> str:
        """Search, fetch, pick random photos, or track downloads on Unsplash.

        - `search`: requires `query`; optional `count` (1-30, default 10), `page`, `orientation`, `color`.
        - `get`: requires `photo_id`; returns photo metadata, EXIF, location, views, downloads.
        - `random`: optional `query`, `orientation`, `count` (1-30, default 1).
        - `download`: requires `photo_id`; triggers Unsplash download tracking and returns `download_url`.

        Args:
            action: Operation to run.
            query: Search keywords (`search` required, `random` optional).
            photo_id: Unsplash photo ID (`get`, `download`).
            count: Result count (`search` per_page default 10, `random` default 1; clamped to 1-30).
            page: Result page number (`search`, default 1).
            orientation: Optional orientation filter (`search`, `random`).
            color: Optional color filter (`search`).

        """
        if action not in self._enabled_actions:
            return f"Error: Unsplash action '{action}' is disabled in configuration."
        if not self.access_key:
            return "Error: No Unsplash API key provided. Set UNSPLASH_ACCESS_KEY environment variable."

        try:
            if action == "search":
                output = self._search(query=query, count=count, page=page, orientation=orientation, color=color)
            elif action == "get":
                output = self._get(photo_id=photo_id)
            elif action == "random":
                output = self._random(query=query, orientation=orientation, count=count)
            else:
                output = self._download(photo_id=photo_id)
        except Exception as exc:
            logger.exception("Unsplash request failed", action=action)
            return f"Error executing Unsplash {action}: {exc}"
        return output

    def _search(
        self,
        *,
        query: str | None,
        count: int | None,
        page: int,
        orientation: UnsplashOrientation | None,
        color: UnsplashColor | None,
    ) -> str:
        if not query:
            return "Error: Please provide a search query."
        params: dict[str, Any] = {
            "query": query,
            "per_page": min(max(1, 10 if count is None else count), 30),
            "page": max(1, page),
        }
        if orientation is not None:
            params["orientation"] = orientation
        if color is not None:
            params["color"] = color
        data = self._request("/search/photos", params)
        return json.dumps(
            {
                "total": data.get("total", 0),
                "total_pages": data.get("total_pages", 0),
                "photos": [_format_photo(photo) for photo in data.get("results", [])],
            },
        )

    def _get(self, *, photo_id: str | None) -> str:
        if not photo_id:
            return "Error: Please provide a photo ID."
        photo = self._request(f"/photos/{photo_id}")
        result = _format_photo(photo)
        if exif := photo.get("exif"):
            result["exif"] = {
                key: exif.get(key) for key in ("make", "model", "aperture", "exposure_time", "focal_length", "iso")
            }
        if location := photo.get("location"):
            result["location"] = {key: location.get(key) for key in ("name", "city", "country")}
        result["views"] = photo.get("views")
        result["downloads"] = photo.get("downloads")
        return json.dumps(result)

    def _random(
        self,
        *,
        query: str | None,
        orientation: UnsplashOrientation | None,
        count: int | None,
    ) -> str:
        params: dict[str, Any] = {"count": min(max(1, 1 if count is None else count), 30)}
        if query:
            params["query"] = query
        if orientation is not None:
            params["orientation"] = orientation
        data = self._request("/photos/random", params)
        photos = [_format_photo(photo) for photo in (data if isinstance(data, list) else [data])]
        return json.dumps({"photos": photos})

    def _download(self, *, photo_id: str | None) -> str:
        if not photo_id:
            return "Error: Please provide a photo ID."
        data = self._request(f"/photos/{photo_id}/download")
        return json.dumps({"photo_id": photo_id, "download_url": data.get("url")})
