"""Native YouTube toolkit using youtube_transcript_api and YouTube oEmbed via httpx."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

if TYPE_CHECKING:
    from youtube_transcript_api import FetchedTranscript

logger = get_logger(__name__)

_YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
_YOUTUBE_HOSTS = frozenset(
    {
        "www.youtube.com",
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "www.youtube-nocookie.com",
        "youtube-nocookie.com",
        "www.youtube.com.br",
        "youtube.com.br",
        "www.youtube.co.uk",
        "youtube.co.uk",
    },
)
_YOUTUBE_PATH_PREFIXES = ("/embed/", "/v/", "/shorts/", "/live/")
_OEMBED_FIELDS = (
    "title",
    "author_name",
    "author_url",
    "type",
    "height",
    "width",
    "version",
    "provider_name",
    "provider_url",
    "thumbnail_url",
)


class _VideoIdError(ValueError):
    """Raised when a YouTube URL cannot yield a valid video ID."""


class _TimeoutSession(requests.Session):
    """Requests session applying a default timeout when transcript calls omit one."""

    def __init__(self, timeout: int) -> None:
        super().__init__()
        self._timeout = timeout

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # noqa: ANN401
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


class YouTubeTools(Toolkit):
    """Extract metadata, captions, and timestamps from YouTube videos."""

    name = "youtube_tools"

    def __init__(
        self,
        enable_get_video_captions: bool = True,
        enable_get_video_data: bool = True,
        enable_get_video_timestamps: bool = True,
        all: bool = False,  # noqa: A002
        languages: list[str] | None = None,
        proxies: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.languages = languages
        self.proxies = proxies
        self.timeout = timeout
        super().__init__(name="youtube_tools")
        if not (all or enable_get_video_captions):
            self.functions.pop(self.get_youtube_video_captions.__name__, None)
        if not (all or enable_get_video_data):
            self.functions.pop(self.get_youtube_video_data.__name__, None)
        if not (all or enable_get_video_timestamps):
            self.functions.pop(self.get_video_timestamps.__name__, None)

    def get_youtube_video_id(self, url: str) -> str | None:
        """Extract the video ID from a YouTube URL."""
        parsed_url = urlparse(url)
        if not parsed_url.scheme and not parsed_url.netloc:
            parsed_url = urlparse(f"//{url}")
        hostname = parsed_url.hostname

        if hostname == "youtu.be":
            return parsed_url.path.lstrip("/").split("/")[0] or None
        if hostname in _YOUTUBE_HOSTS:
            if parsed_url.path == "/watch":
                return parse_qs(parsed_url.query).get("v", [None])[0]
            if parsed_url.path.startswith(_YOUTUBE_PATH_PREFIXES):
                parts = parsed_url.path.split("/")
                return parts[2] or None if len(parts) > 2 else None
        return None

    def _require_video_id(self, url: str) -> str:
        """Validate ``url`` and return its video ID or raise ``_VideoIdError`` with the tool message."""
        if not url:
            msg = "No URL provided"
            raise _VideoIdError(msg)
        try:
            video_id = self.get_youtube_video_id(url)
        except Exception as exc:
            msg = "Error getting video ID from URL, please provide a valid YouTube url"
            raise _VideoIdError(msg) from exc
        if video_id is None:
            msg = "No video ID found"
            raise _VideoIdError(msg)
        return video_id

    def _fetch_captions(self, video_id: str) -> FetchedTranscript:
        """Fetch transcript snippets for ``video_id`` using configured languages, proxies, and timeout."""
        proxy_config = (
            GenericProxyConfig(http_url=self.proxies.get("http"), https_url=self.proxies.get("https"))
            if self.proxies
            else None
        )
        with _TimeoutSession(self.timeout) as session:
            api = YouTubeTranscriptApi(proxy_config=proxy_config, http_client=session)
            if self.languages:
                return api.fetch(video_id, languages=self.languages)
            return api.fetch(video_id)

    @tool
    def get_youtube_video_captions(self, url: str) -> str:
        """Use this function to get captions from a YouTube video.

        Args:
            url: The URL of the YouTube video.

        Returns:
            str: The captions of the YouTube video.

        """
        logger.debug("Getting captions for YouTube video", url=url)
        try:
            video_id = self._require_video_id(url)
        except _VideoIdError as exc:
            return str(exc)
        try:
            captions = self._fetch_captions(video_id)
        except Exception as exc:
            return f"Error getting captions for video: {exc}"
        return " ".join(line.text for line in captions) if captions else "No captions found for video"

    @tool
    def get_youtube_video_data(self, url: str) -> str:
        """Function to get video data from a YouTube URL.

        Data returned includes {title, author_name, author_url, type, height, width, version, provider_name, provider_url, thumbnail_url}

        Args:
            url: The URL of the YouTube video.

        Returns:
            str: JSON data of the YouTube video.

        """
        logger.debug("Getting video data for YouTube video", url=url)
        try:
            video_id = self._require_video_id(url)
        except _VideoIdError as exc:
            return str(exc)
        try:
            response = httpx.get(
                _YOUTUBE_OEMBED_URL,
                params={"format": "json", "url": f"https://www.youtube.com/watch?v={video_id}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            video_data = response.json()
            clean_data = {field: video_data.get(field) for field in _OEMBED_FIELDS}
        except Exception as exc:
            return f"Error getting video data: {exc}"
        return json.dumps(clean_data, indent=4)

    @tool
    def get_video_timestamps(self, url: str) -> str:
        """Generate timestamps for a YouTube video based on captions.

        Args:
            url: The URL of the YouTube video.

        Returns:
            str: Timestamps and summaries for the video.

        """
        logger.debug("Getting timestamps for YouTube video", url=url)
        try:
            video_id = self._require_video_id(url)
        except _VideoIdError as exc:
            return str(exc)
        try:
            captions = self._fetch_captions(video_id)
            timestamps = [
                f"{minutes}:{seconds:02d} - {line.text}"
                for line in captions
                for minutes, seconds in (divmod(int(line.start), 60),)
            ]
        except Exception as exc:
            return f"Error generating timestamps: {exc}"
        return "\n".join(timestamps)
