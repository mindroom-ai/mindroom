"""Tests for the native Bright Data toolkit."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import requests

from mindroom.tool_system.tool_function import ToolValue
from mindroom.tools.brightdata import brightdata_tools

if TYPE_CHECKING:
    import pytest


def test_brightdata_youtube_videos_submits_correct_dataset_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """youtube_videos must submit the YouTube Videos dataset ID, not Booking's hotel dataset ID."""
    post_calls: list[dict[str, Any]] = []
    get_calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        post_calls.append({"url": url, "kwargs": kwargs})
        return SimpleNamespace(status_code=200, json=lambda: {"snapshot_id": "snap-yt-1"})

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        get_calls.append({"url": url, "kwargs": kwargs})
        return SimpleNamespace(status_code=200, json=lambda: [{"title": "Demo Video"}])

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    tool = brightdata_tools()(api_key="bd-secret")
    result = tool.web_data_feed("youtube_videos", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "https://api.brightdata.com/datasets/v3/trigger"
    assert post_calls[0]["kwargs"]["params"] == {
        "dataset_id": "gd_lk56epmy2i5g7lzu0k",
        "include_errors": "true",
    }
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == "https://api.brightdata.com/datasets/v3/snapshot/snap-yt-1"
    assert json.loads(result) == [{"title": "Demo Video"}]


def test_brightdata_get_screenshot_returns_raw_png_bytes_in_tool_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_screenshot must return a ToolValue carrying the raw PNG bytes in an Image artifact."""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfake-png-payload"

    def fake_post(url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        del url, kwargs
        return SimpleNamespace(status_code=200, content=png_bytes, text="")

    monkeypatch.setattr(requests, "post", fake_post)

    tool = brightdata_tools()(api_key="bd-secret")
    result = tool.get_screenshot("https://example.com")

    assert isinstance(result, ToolValue)
    assert len(result.images) == 1
    assert result.images[0].mime_type == "image/png"
    assert result.images[0].content == png_bytes
    assert result.images[0].original_prompt == "Screenshot of https://example.com"
