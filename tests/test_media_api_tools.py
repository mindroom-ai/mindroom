"""Unit tests for native ElevenLabs, Cartesia, Giphy, and Unsplash toolkits."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import httpx
import pytest

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
    """Cartesia exercises the real 5.1.0 SDK page type, localize accent parameter, and TTS bytes endpoint."""
    recorded_requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        recorded_requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/voices":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "c1",
                            "access": "public",
                            "created_at": "2026-01-01T00:00:00Z",
                            "description": "Clear",
                            "is_owner": False,
                            "language": "en",
                            "locales": [{"locale": "en-US", "is_native": True}],
                            "name": "Sonic",
                            "status": "active",
                            "tagline": "Clear voice",
                            "visibility": "all",
                        },
                    ],
                    "has_more": False,
                },
                request=request,
            )
        if request.method == "POST" and request.url.path == "/voices/localize":
            return httpx.Response(
                200,
                json={
                    "id": "c2",
                    "access": "private",
                    "created_at": "2026-01-01T00:00:00Z",
                    "description": "Desc",
                    "language": "fr",
                    "name": "French",
                    "tagline": "French voice",
                    "user_id": "u1",
                    "visibility": "owner",
                },
                request=request,
            )
        if request.method == "POST" and request.url.path == "/tts/bytes":
            return httpx.Response(200, content=b"mp3-data", request=request)
        msg = f"Unexpected Cartesia request: {request.method} {request.url.path}"
        raise AssertionError(msg)

    real_client_cls = cartesia_module.cartesia.Cartesia
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        cartesia_module.cartesia,
        "Cartesia",
        lambda **kwargs: real_client_cls(**kwargs, http_client=http_client),
    )
    toolkit = CartesiaTools(api_key="ct-key", enable_localize_voice=True)

    listed = json.loads(toolkit.list_voices())
    assert listed == [{"id": "c1", "name": "Sonic", "description": "Clear", "language": "en"}]

    speech = toolkit.text_to_speech("Bonjour")
    assert isinstance(speech, ToolValue)
    assert len(speech.audios) == 1
    assert speech.audios[0].content == b"mp3-data"
    assert speech.audios[0].mime_type == "audio/mpeg"

    localized = json.loads(toolkit.localize_voice("French", "Desc", "fr", "female", accent="parisian"))
    assert localized["id"] == "c2"
    assert localized["language"] == "fr"

    localize_body = next(body for method, path, body in recorded_requests if path == "/voices/localize")
    assert localize_body is not None
    assert localize_body["accent"] == "parisian"
    assert localize_body["language"] == "fr"
    assert localize_body["original_speaker_gender"] == "female"
    assert localize_body["voice_id"] == toolkit.default_voice_id


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


@pytest.mark.parametrize("action", ["get", "download"])
@pytest.mark.parametrize(
    "bad_photo_id",
    [
        "abc/download",
        "../random",
        "abc?foo=1",
        "abc#frag",
        "abc def",
        "abc\n",
        "https://api.unsplash.com/photos/abc",
    ],
)
def test_unsplash_rejects_path_traversal_and_non_opaque_photo_ids(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    bad_photo_id: str,
) -> None:
    """Unsplash rejects path traversal or query/URL syntax in photo_id before issuing any HTTP call."""
    http_get = Mock(side_effect=AssertionError("httpx.get must not be called for invalid photo_id"))
    monkeypatch.setattr(unsplash_module.httpx, "get", http_get)
    toolkit = UnsplashTools(access_key="u-key", enable_download_photo=True)

    result = toolkit.unsplash(action, photo_id=bad_photo_id)  # type: ignore[arg-type]

    assert result.startswith("Error: Invalid photo ID")
    http_get.assert_not_called()
