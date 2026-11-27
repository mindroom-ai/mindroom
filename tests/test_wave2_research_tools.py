"""Offline tests for native Wave 2 research toolkits (Wikipedia, Hacker News, YouTube, and contracts)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import requests
import wikipedia
from wikipedia.exceptions import DisambiguationError

from mindroom.custom_tools import youtube as youtube_module
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.tools.arxiv import arxiv_tools
from mindroom.tools.hackernews import hackernews_tools
from mindroom.tools.pubmed import pubmed_tools
from mindroom.tools.wikipedia import wikipedia_tools
from mindroom.tools.youtube import youtube_tools


@pytest.mark.parametrize(
    ("factory", "expected_name", "expected_functions"),
    [
        (wikipedia_tools, "wikipedia_tools", ["wikipedia_search"]),
        (arxiv_tools, "arxiv_tools", ["arxiv_search", "arxiv_read_papers"]),
        (pubmed_tools, "pubmed", ["pubmed_search"]),
        (hackernews_tools, "hackers_news", ["hackernews_top_stories", "hackernews_user_details"]),
        (youtube_tools, "youtube_tools", ["youtube_video"]),
    ],
)
def test_wave2_research_toolkits_are_native_with_preserved_names_and_order(
    factory: Any,  # noqa: ANN401
    expected_name: str,
    expected_functions: list[str],
) -> None:
    """Each scoped factory returns a native Toolkit subclass with its preserved toolkit name and function order."""
    tool_class = factory()
    toolkit = tool_class()

    assert issubclass(tool_class, Toolkit)
    assert toolkit.name == expected_name
    assert list(toolkit.functions) == expected_functions


def test_wikipedia_search_success_disambiguation_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wikipedia returns document JSON on success and structured JSON for disambiguation and errors."""
    seen: list[tuple[str, bool]] = []

    def fake_summary(query: str, *, auto_suggest: bool = True) -> str:
        seen.append((query, auto_suggest))
        if query == "Mercury":
            raise DisambiguationError(query, ["Mercury (planet)", "Mercury (element)"])
        if query == "boom":
            msg = "service unavailable"
            raise RuntimeError(msg)
        return "Python is a programming language."

    monkeypatch.setattr(wikipedia, "summary", fake_summary)
    toolkit = wikipedia_tools()(auto_suggest=False)

    assert json.loads(toolkit.wikipedia_search("Python")) == {
        "name": "Python",
        "meta_data": {},
        "content": "Python is a programming language.",
    }
    assert json.loads(toolkit.wikipedia_search("Mercury")) == {
        "disambiguation": "Mercury",
        "options": ["Mercury (planet)", "Mercury (element)"],
    }
    assert json.loads(toolkit.wikipedia_search("boom")) == {"error": "service unavailable"}
    assert seen == [("Python", False), ("Mercury", False), ("boom", False)]


def test_hackernews_stories_user_details_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hacker News fetches stories, skips null items, encodes usernames, and reports errors cleanly."""
    requested_paths: list[str] = []
    fail_topstories = False

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one response per mock endpoint.
        raw_path = request.url.raw_path.decode("ascii")
        requested_paths.append(raw_path)
        if raw_path == "/v0/topstories.json":
            if fail_topstories:
                return httpx.Response(500, text="server error")
            return httpx.Response(200, json=[101, 102, 103])
        if raw_path == "/v0/item/101.json":
            return httpx.Response(200, json={"id": 101, "title": "First", "by": "alice"})
        if raw_path == "/v0/item/102.json":
            return httpx.Response(200, content="null")
        if raw_path == "/v0/item/103.json":
            return httpx.Response(200, json={"id": 103, "title": "No Author"})
        if raw_path == "/v0/user/pg.json":
            return httpx.Response(200, json={"id": "pg", "karma": 157000, "about": "essays", "submitted": [1, 2, 3]})
        if raw_path == "/v0/user/error_user.json":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, content="null")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "get", client.get)
        toolkit = hackernews_tools()(timeout=15)

        assert toolkit.hackernews_top_stories(num_stories=0) == "[]"
        assert toolkit.hackernews_top_stories(num_stories=-2) == "[]"

        stories = json.loads(toolkit.hackernews_top_stories(num_stories=3))
        assert stories == [
            {"id": 101, "title": "First", "by": "alice", "username": "alice"},
            {"id": 103, "title": "No Author", "username": "unknown"},
        ]

        user = json.loads(toolkit.hackernews_user_details("pg"))
        assert user == {"id": "pg", "karma": 157000, "about": "essays", "total_items_submitted": 3}

        assert toolkit.hackernews_user_details("missing") == "Error getting user details: user 'missing' not found"
        assert toolkit.hackernews_user_details("error_user").startswith("Error getting user details:")
        assert (
            toolkit.hackernews_user_details("../topstories")
            == "Error getting user details: user '../topstories' not found"
        )

        fail_topstories = True
        assert toolkit.hackernews_top_stories(num_stories=1).startswith("Error fetching stories:")

    assert "/v0/user/..%2Ftopstories.json" in requested_paths


def test_hackernews_enable_flags_and_all() -> None:
    """HackerNewsTools honors individual enable flags and the all override."""
    tool_class = hackernews_tools()

    assert list(tool_class(enable_get_top_stories=False).functions) == ["hackernews_user_details"]
    assert list(tool_class(enable_get_user_details=False).functions) == ["hackernews_top_stories"]
    assert list(tool_class(enable_get_top_stories=False, enable_get_user_details=False, all=True).functions) == [
        "hackernews_top_stories",
        "hackernews_user_details",
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/v/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/", None),
        ("https://www.youtube.com/shorts/", None),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("http://127.0.0.1:8080/watch?v=dQw4w9WgXcQ", None),
    ],
)
def test_youtube_video_id_extraction(url: str, expected: str | None) -> None:
    """YouTube URL parsing supports standard, mobile, shorts, live, and scheme-less forms and rejects other hosts."""
    toolkit = youtube_tools()()

    assert toolkit.get_youtube_video_id(url) == expected


def test_youtube_video_data_uses_oembed_via_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Video metadata fetches YouTube oEmbed over httpx and filters to the documented fields."""
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.params["url"].endswith("v=fail123"):
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200,
            json={
                "title": "Demo Video",
                "author_name": "Channel",
                "author_url": "https://www.youtube.com/@channel",
                "type": "video",
                "height": 113,
                "width": 200,
                "version": "1.0",
                "provider_name": "YouTube",
                "provider_url": "https://www.youtube.com/",
                "thumbnail_url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
                "html": "<iframe>ignored</iframe>",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "get", client.get)
        toolkit = youtube_tools()(timeout=12)

        assert toolkit.youtube_video("data", "") == "No URL provided"
        assert toolkit.youtube_video("data", "https://example.com/video") == "No video ID found"
        assert (
            toolkit.youtube_video("data", "http://[invalid")
            == "Error getting video ID from URL, please provide a valid YouTube url"
        )

        data = json.loads(toolkit.youtube_video("data", "https://youtu.be/abc123"))
        assert data == {
            "title": "Demo Video",
            "author_name": "Channel",
            "author_url": "https://www.youtube.com/@channel",
            "type": "video",
            "height": 113,
            "width": 200,
            "version": "1.0",
            "provider_name": "YouTube",
            "provider_url": "https://www.youtube.com/",
            "thumbnail_url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
        }
        assert toolkit.youtube_video("data", "https://youtu.be/fail123").startswith("Error getting video data:")

    assert len(captured) == 2
    assert str(captured[0].url).startswith("https://www.youtube.com/oembed?")
    assert captured[0].url.params["format"] == "json"
    assert captured[0].url.params["url"] == "https://www.youtube.com/watch?v=abc123"


@dataclass
class _Snippet:
    text: str
    start: float


def test_youtube_captions_timestamps_languages_proxies_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transcript calls forward languages, GenericProxyConfig, and a timeout-enforcing Session."""
    calls: list[dict[str, Any]] = []
    sent_timeouts: list[object] = []

    def fake_super_send(
        _self: requests.Session,
        request: requests.PreparedRequest,
        **kwargs: Any,  # noqa: ANN401
    ) -> requests.Response:
        sent_timeouts.append(kwargs.get("timeout"))
        response = requests.Response()
        response.status_code = 200
        response.request = request
        return response

    monkeypatch.setattr(requests.Session, "send", fake_super_send)

    class FakeTranscriptApi:
        def __init__(
            self,
            *,
            proxy_config: Any = None,  # noqa: ANN401
            http_client: requests.Session | None = None,
        ) -> None:
            self.proxy_config = proxy_config
            self.http_client = http_client

        def fetch(self, video_id: str, **kwargs: Any) -> list[_Snippet]:  # noqa: ANN401
            assert self.http_client is not None
            self.http_client.get("https://www.youtube.com/watch")
            calls.append(
                {
                    "video_id": video_id,
                    "kwargs": kwargs,
                    "proxies": self.proxy_config.to_requests_dict() if self.proxy_config else None,
                },
            )
            if video_id == "empty123":
                return []
            if video_id == "error123":
                msg = "transcripts disabled"
                raise RuntimeError(msg)
            return [_Snippet("Hello world", 5.2), _Snippet("Next section", 125.9)]

    monkeypatch.setattr(youtube_module, "YouTubeTranscriptApi", FakeTranscriptApi)

    toolkit = youtube_tools()(
        languages=["nl", "en"],
        proxies={"https": "http://proxy.internal:8080"},
        timeout=17,
    )

    assert toolkit.youtube_video("captions", "https://youtu.be/abc123") == "Hello world Next section"
    assert toolkit.youtube_video("captions", "https://youtu.be/abc123", languages=["fr"]) == "Hello world Next section"
    assert toolkit.youtube_video("captions", "https://youtu.be/empty123") == "No captions found for video"
    assert toolkit.youtube_video("captions", "https://youtu.be/error123") == (
        "Error getting captions for video: transcripts disabled"
    )
    assert toolkit.youtube_video("timestamps", "https://youtu.be/abc123") == "0:05 - Hello world\n2:05 - Next section"
    assert toolkit.youtube_video("timestamps", "https://youtu.be/error123") == (
        "Error generating timestamps: transcripts disabled"
    )
    assert toolkit.youtube_video("timestamps", "") == "No URL provided"
    assert toolkit.youtube_video("timestamps", "https://example.com/nope") == "No video ID found"

    assert sent_timeouts == [17, 17, 17, 17, 17, 17]
    assert calls[0] == {
        "video_id": "abc123",
        "kwargs": {"languages": ["nl", "en"]},
        "proxies": {"http": "http://proxy.internal:8080", "https": "http://proxy.internal:8080"},
    }
    assert calls[1]["kwargs"] == {"languages": ["fr"]}


def test_youtube_enable_flags_and_all() -> None:
    """YouTubeTools narrows schema/fields on toggles, omits function when all disabled, and honors all override."""
    tool_class = youtube_tools()

    data_only = tool_class(enable_get_video_captions=False, enable_get_video_timestamps=False)
    assert list(data_only.functions) == ["youtube_video"]
    offered_data = adapt_toolkit(data_only).functions[0]
    assert offered_data.parameters["properties"]["action"]["enum"] == ["data"]
    assert set(offered_data.parameters["properties"]) == {"action", "url"}
    assert "`captions`" not in (offered_data.description or "")
    assert "disabled" in data_only.youtube_video("captions", "https://youtu.be/abc123").lower()
    assert "languages" in data_only.youtube_video("data", "https://youtu.be/abc123", languages=["en"])

    captions_only = tool_class(enable_get_video_data=False, enable_get_video_timestamps=False)
    offered_captions = adapt_toolkit(captions_only).functions[0]
    assert offered_captions.parameters["properties"]["action"]["enum"] == ["captions"]
    assert "languages" in offered_captions.parameters["properties"]

    none_enabled = tool_class(
        enable_get_video_captions=False,
        enable_get_video_data=False,
        enable_get_video_timestamps=False,
    )
    assert list(none_enabled.functions) == []

    all_override = tool_class(
        enable_get_video_captions=False,
        enable_get_video_data=False,
        enable_get_video_timestamps=False,
        all=True,
    )
    offered_all = adapt_toolkit(all_override).functions[0]
    assert offered_all.parameters["properties"]["action"]["enum"] == ["captions", "data", "timestamps"]
    assert "languages" in offered_all.parameters["properties"]
