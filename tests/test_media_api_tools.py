"""Unit tests for native ElevenLabs, Cartesia, Giphy, and Unsplash toolkits."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import httpx

import mindroom.custom_tools.cartesia as cartesia_module
import mindroom.custom_tools.eleven_labs as eleven_labs_module
import mindroom.custom_tools.giphy as giphy_module
import mindroom.custom_tools.unsplash as unsplash_module
from mindroom.custom_tools.cartesia import CartesiaTools
from mindroom.custom_tools.eleven_labs import ElevenLabsTools
from mindroom.custom_tools.giphy import GiphyTools
from mindroom.custom_tools.unsplash import UnsplashTools
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_eleven_labs_voices_tts_and_sfx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ElevenLabs lists voices and returns native ToolValue Audio artifacts, saving when target_directory is set."""
    voice = SimpleNamespace(voice_id="v1", name="Rachel", description="Calm")
    client = SimpleNamespace(
        voices=SimpleNamespace(get_all=Mock(return_value=SimpleNamespace(voices=[voice]))),
        text_to_speech=SimpleNamespace(convert=Mock(return_value=iter([b"aa", b"bb"]))),
        text_to_sound_effects=SimpleNamespace(convert=Mock(return_value=iter([b"sfx"]))),
    )
    monkeypatch.setattr(eleven_labs_module, "ElevenLabs", Mock(return_value=client))
    out_dir = tmp_path / "audio"
    toolkit = ElevenLabsTools(api_key="el-key", target_directory=str(out_dir))

    voices = json.loads(toolkit.get_voices())
    assert voices == [{"id": "v1", "name": "Rachel", "description": "Calm"}]

    tts = toolkit.text_to_speech("Hello")
    assert isinstance(tts, ToolValue)
    assert len(tts.audios) == 1
    assert tts.audios[0].content == b"aabb"
    assert tts.audios[0].mime_type == "audio/mpeg"

    sfx = toolkit.generate_sound_effect("Thunder", duration_seconds=2.5)
    assert len(sfx.audios) == 1
    assert sfx.audios[0].content == b"sfx"
    assert len(list(out_dir.glob("*.mp3"))) == 2


def test_cartesia_voices_tts_and_localize(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cartesia lists voices, generates MP3 audio artifacts, and localizes voices."""
    voice = SimpleNamespace(id="c1", name="Sonic", description="Clear", language="en")
    client = SimpleNamespace(
        voices=SimpleNamespace(
            list=Mock(return_value=SimpleNamespace(items=[voice])),
            localize=Mock(return_value={"id": "c2", "language": "fr"}),
        ),
        tts=SimpleNamespace(bytes=Mock(return_value=iter([b"mp3", b"-data"]))),
    )
    monkeypatch.setattr(cartesia_module.cartesia, "Cartesia", Mock(return_value=client))
    toolkit = CartesiaTools(api_key="ct-key", enable_localize_voice=True)

    listed = json.loads(toolkit.list_voices())
    assert listed == [{"id": "c1", "name": "Sonic", "description": "Clear", "language": "en"}]

    speech = toolkit.text_to_speech("Bonjour")
    assert isinstance(speech, ToolValue)
    assert len(speech.audios) == 1
    assert speech.audios[0].content == b"mp3-data"
    assert speech.audios[0].mime_type == "audio/mpeg"

    localized = json.loads(toolkit.localize_voice("French", "Desc", "fr", "female"))
    assert localized == {"id": "c2", "language": "fr"}


def test_giphy_search_returns_image_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Giphy search parses GIF results into native ToolValue Image artifacts."""
    response = httpx.Response(
        200,
        json={
            "data": [
                {
                    "alt_text": "Happy cat",
                    "images": {"original": {"url": "https://media.giphy.com/cat.gif"}},
                },
            ],
        },
        request=httpx.Request("GET", "https://api.giphy.com/v1/gifs/search"),
    )
    monkeypatch.setattr(giphy_module.httpx, "get", Mock(return_value=response))
    toolkit = GiphyTools(api_key="g-key", limit=2)

    result = toolkit.search_gifs("happy cat")

    assert isinstance(result, ToolValue)
    assert len(result.images) == 1
    assert result.images[0].url == "https://media.giphy.com/cat.gif"
    assert result.images[0].alt_text == "Happy cat"


def test_unsplash_consolidated_actions_and_enable_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsplash handles search, get, random, and download actions and honors enable flags."""
    sample_photo = {
        "id": "p1",
        "description": "Mountains",
        "width": 1600,
        "height": 900,
        "color": "#112233",
        "created_at": "2026-01-01T00:00:00Z",
        "urls": {"raw": "r", "full": "f", "regular": "reg", "small": "s", "thumb": "t"},
        "user": {"name": "Alice", "username": "alice", "links": {"html": "https://unsplash.com/@alice"}},
        "links": {"html": "https://unsplash.com/photos/p1", "download": "https://unsplash.com/photos/p1/download"},
        "likes": 42,
        "tags": [{"title": "nature"}],
        "views": 100,
        "downloads": 5,
    }

    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        req = httpx.Request("GET", url)
        if url.endswith("/search/photos"):
            return httpx.Response(200, json={"total": 1, "total_pages": 1, "results": [sample_photo]}, request=req)
        if url.endswith("/photos/random"):
            return httpx.Response(200, json=sample_photo, request=req)
        if url.endswith("/photos/p1/download"):
            return httpx.Response(200, json={"url": "https://images.unsplash.com/dl"}, request=req)
        return httpx.Response(200, json=sample_photo, request=req)

    monkeypatch.setattr(unsplash_module.httpx, "get", fake_get)
    toolkit = UnsplashTools(access_key="u-key")

    search_res = json.loads(toolkit.unsplash("search", query="mountains", orientation="landscape", color="blue"))
    assert search_res["total"] == 1
    assert search_res["photos"][0]["id"] == "p1"

    get_res = json.loads(toolkit.unsplash("get", photo_id="p1"))
    assert get_res["views"] == 100

    random_res = json.loads(toolkit.unsplash("random", query="mountains"))
    assert len(random_res["photos"]) == 1

    # download is disabled by default
    blocked = toolkit.unsplash("download", photo_id="p1")
    assert "disabled" in blocked

    all_toolkit = UnsplashTools(access_key="u-key", enable_download_photo=True)
    dl_res = json.loads(all_toolkit.unsplash("download", photo_id="p1"))
    assert dl_res == {"photo_id": "p1", "download_url": "https://images.unsplash.com/dl"}
