"""Media URLs: the pipeline downloads every URL a route does not send, with one size cap and one failure rule.

Each family's bare adapter runs inside the production pipeline over a mock wire, and the media servers answer
through a mock transport, so what these tests prove is what the registry runs.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, complete
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers import model_http_client
from mindroom.providers.pipeline import remote_media
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.media import FALLBACK_MARKER
from tests.providers.family_support import CLAUDE_DEPRECATION_FILTER, FAMILIES
from tests.providers.golden_support import (
    Wire,
    hide_provider_environment,
    httpx2_transport,
    pipeline_provider,
    sse_response,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

pytestmark = pytest.mark.filterwarnings(CLAUDE_DEPRECATION_FILTER)

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c636001000000050001a5f645400000000049454e44ae426082",
)
_PDF = b"%PDF-1.4\n%%EOF\n"
_NOTE = f"{FALLBACK_MARKER}\n{INLINE_MEDIA_FALLBACK_PROMPT}"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_provider_environment(monkeypatch)
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)


def _media_server(monkeypatch: pytest.MonkeyPatch, responses: dict[str, httpx.Response]) -> list[str]:
    """Answer the pipeline's downloads, which go through the model HTTP client, from ``responses`` by URL.

    Returns the URLs downloaded.
    """
    downloaded: list[str] = []

    def respond(request: httpx2.Request) -> httpx.Response:
        downloaded.append(str(request.url))
        return responses.get(str(request.url), httpx.Response(404))

    pool = httpx2.AsyncClient(transport=httpx2_transport(respond), follow_redirects=True)
    monkeypatch.setattr(model_http_client, "_loop_client", lambda: pool)
    return downloaded


def _provider(family: str, root: Path, wire: Wire) -> ModelProvider:
    return pipeline_provider(FAMILIES[family].adapter(root, wire))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.mark.asyncio
async def test_claude_receives_downloaded_images_and_documents_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude gets every user turn's URL images and documents as bytes, a PDF too, beside media already inline."""
    downloaded = _media_server(
        monkeypatch,
        {
            "https://media.example/photo": httpx.Response(200, content=_PNG, headers={"content-type": "image/png"}),
            "https://media.example/notes": httpx.Response(
                200,
                content=b"# Notes\n",
                headers={"content-type": "text/markdown"},
            ),
            "https://media.example/report.pdf": httpx.Response(200, content=_PDF),
        },
    )
    wire = Wire(FAMILIES["anthropic"].reply())
    inline = Image(content=_PNG, mime_type="image/png")
    first = Message(role="user", content="Look.", images=[inline, Image(url="https://media.example/photo")])
    second = Message(
        role="user",
        content="Read.",
        files=[File(url="https://media.example/notes"), File(url="https://media.example/report.pdf")],
    )
    messages = [first, Message(role="assistant", content="Seen."), second]

    await complete(_provider("anthropic", tmp_path, wire), ModelRequest(messages=messages))

    png = {"type": "base64", "media_type": "image/png", "data": _b64(_PNG)}
    sent_first, _, sent_second = wire.body()["messages"]
    assert [block.get("source") for block in sent_first["content"]] == [None, png, png]
    assert [block.get("source") for block in sent_second["content"]] == [
        None,
        {"type": "text", "media_type": "text/plain", "data": "# Notes\n"},
        {"type": "base64", "media_type": "application/pdf", "data": _b64(_PDF)},
    ]
    assert downloaded == [
        "https://media.example/photo",
        "https://media.example/notes",
        "https://media.example/report.pdf",
    ]
    assert first.images is not None
    assert first.images[1].url is not None, "the request's downloaded copies leave the input alone"


@pytest.mark.asyncio
async def test_gemini_receives_downloaded_images_and_audio_and_fetches_files_and_videos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini fetches file and video URLs itself; image and audio URLs are downloaded and inlined."""
    downloaded = _media_server(
        monkeypatch,
        {
            "https://media.test/generated": httpx.Response(200, content=_PNG),
            "https://media.test/speech": httpx.Response(200, content=b"RIFF-speech"),
        },
    )
    wire = Wire(FAMILIES["gemini"].reply())
    message = Message(
        role="user",
        content="Use these",
        images=[Image(url="https://media.test/generated")],
        audio=[Audio(url="https://media.test/speech", format="wav")],
        files=[File(url="https://media.test/report.pdf", mime_type="application/pdf")],
        videos=[Video(url="https://media.test/clip.mp4")],
    )

    await complete(_provider("gemini", tmp_path, wire), ModelRequest(messages=[message]))

    parts = wire.body()["contents"][0]["parts"]
    inline = [
        (part["inlineData"]["mimeType"], base64.urlsafe_b64decode(part["inlineData"]["data"]))
        for part in parts
        if "inlineData" in part
    ]
    assert inline == [("image/png", _PNG), ("audio/wav", b"RIFF-speech")]
    assert [part["fileData"] for part in parts if "fileData" in part] == [
        {"fileUri": "https://media.test/clip.mp4", "mimeType": "video/mp4"},
        {"fileUri": "https://media.test/report.pdf", "mimeType": "application/pdf"},
    ]
    assert downloaded == ["https://media.test/generated", "https://media.test/speech"]


@pytest.mark.asyncio
async def test_ollama_receives_downloaded_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama takes images only as bytes, so an image a tool returned as a URL is downloaded."""
    _media_server(monkeypatch, {"https://media.test/generated": httpx.Response(200, content=_PNG)})
    wire = Wire(FAMILIES["ollama"].reply())
    message = Message(role="user", content="Look", images=[Image(url="https://media.test/generated")])

    await complete(_provider("ollama", tmp_path, wire), ModelRequest(messages=[message]))

    assert wire.body()["messages"][-1]["images"] == [_b64(_PNG)]


@pytest.mark.asyncio
async def test_chat_fetches_image_urls_and_receives_downloaded_files_and_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat Completions sends an image URL for the provider to fetch; file and audio URLs are downloaded.

    A downloaded file is typed as its server declared, else by its URL's name, which also names a file without one.
    """
    downloaded = _media_server(
        monkeypatch,
        {
            "https://media.test/notes": httpx.Response(200, content=b"notes", headers={"content-type": "text/plain"}),
            "https://media.test/table.csv": httpx.Response(200, content=b"a,b"),
            "https://media.test/speech": httpx.Response(200, content=b"RIFF-speech"),
        },
    )
    wire = Wire(FAMILIES["openai_chat"].reply())
    message = Message(
        role="user",
        content="Use these",
        images=[Image(url="https://media.test/photo.png")],
        audio=[Audio(url="https://media.test/speech", format="wav")],
        files=[File(url="https://media.test/notes", filename="notes.txt"), File(url="https://media.test/table.csv")],
    )

    await complete(_provider("openai_chat", tmp_path, wire), ModelRequest(messages=[message]))

    assert wire.body()["messages"][-1]["content"] == [
        {"type": "file", "file": {"filename": "table.csv", "file_data": f"data:text/csv;base64,{_b64(b'a,b')}"}},
        {"type": "file", "file": {"filename": "notes.txt", "file_data": f"data:text/plain;base64,{_b64(b'notes')}"}},
        {"type": "text", "text": "Use these"},
        {"type": "image_url", "image_url": {"url": "https://media.test/photo.png"}},
        {"type": "input_audio", "input_audio": {"data": _b64(b"RIFF-speech"), "format": "wav"}},
    ]
    assert downloaded == ["https://media.test/speech", "https://media.test/notes", "https://media.test/table.csv"]


@pytest.mark.asyncio
async def test_responses_fetches_image_and_file_urls_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses sends image and file URLs for the provider to fetch, so nothing is downloaded."""
    downloaded = _media_server(monkeypatch, {})
    wire = Wire(FAMILIES["openai_responses"].reply())
    message = Message(
        role="user",
        content="Use these",
        images=[Image(url="https://media.test/photo.png")],
        files=[File(url="https://media.test/report.pdf")],
    )

    await complete(_provider("openai_responses", tmp_path, wire), ModelRequest(messages=[message]))

    assert wire.body()["input"][-1]["content"] == [
        {"type": "input_text", "text": "Use these"},
        {"type": "input_image", "image_url": "https://media.test/photo.png"},
        {"type": "input_file", "file_url": "https://media.test/report.pdf"},
    ]
    assert downloaded == []


def _url_media(kind: str, url: str) -> dict[str, Any]:
    media = {
        "image": Image(url=url),
        "file": File(url=url, mime_type="text/plain"),
        "audio": Audio(url=url, format="wav"),
    }
    return {{"image": "images", "file": "files", "audio": "audio"}[kind]: [media[kind]]}


# Each family with a media kind it neither sends by URL nor omits, so the pipeline downloads it.
_DOWNLOADED = [
    ("anthropic", "image"),
    ("anthropic", "file"),
    ("gemini", "image"),
    ("gemini", "audio"),
    ("gemini_vertexai", "image"),
    ("ollama", "image"),
    ("openai_chat", "file"),
    ("openai_chat", "audio"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["unreachable", "oversized", "empty"])
@pytest.mark.parametrize(("family", "kind"), _DOWNLOADED)
async def test_media_a_url_cannot_supply_reaches_every_family_as_the_media_note(
    family: str,
    kind: str,
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL that fails, is empty, or exceeds the download cap sends the media fallback's note on every family.

    The media is never dropped silently.
    """
    monkeypatch.setattr(remote_media, "_DOWNLOAD_LIMIT_BYTES", 128)
    url = f"https://media.test/{source}"
    _media_server(
        monkeypatch,
        {
            "https://media.test/oversized": httpx.Response(200, content=b"0" * 256),
            "https://media.test/empty": httpx.Response(200, content=b""),
        },
    )
    spec = FAMILIES[family]
    wire = Wire(spec.reply())
    message = Message(role="user", content="Look", **_url_media(kind, url))

    turn = await complete(_provider(family, tmp_path, wire), ModelRequest(messages=[message]))

    assert (turn.content, len(wire.requests)) == ("ok", 1)
    assert spec.user_text(wire.body()) == f"Look\n\n{_NOTE}"


@pytest.mark.asyncio
async def test_a_guidance_retry_resends_the_downloaded_media_without_downloading_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The malformed-call guidance retry sends the bytes the first attempt downloaded."""
    downloaded = _media_server(monkeypatch, {"https://media.test/generated": httpx.Response(200, content=_PNG)})
    malformed = {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"}]}
    wire = Wire(sse_response([malformed]), FAMILIES["gemini"].reply())
    tool = ToolSpec(name="get_weather", description="Weather.", parameters={"type": "object"}, strict=None)
    message = Message(role="user", content="Look", images=[Image(url="https://media.test/generated")])

    await complete(_provider("gemini", tmp_path, wire), ModelRequest(messages=[message], tools=[tool]))

    assert len(wire.requests) == 2
    assert all("inlineData" in json.dumps(wire.body(index)) for index in range(2))
    assert downloaded == ["https://media.test/generated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://media.test/expired.png", "file:///srv/photo.png"])
async def test_a_url_the_provider_cannot_fetch_reaches_the_model_as_the_media_note(
    url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL the route sends goes to the provider whatever it names, and the provider's rejection sends the note."""
    downloaded = _media_server(monkeypatch, {})
    rejected = httpx.Response(400, json={"error": {"message": "Invalid image URL", "type": "invalid_request_error"}})
    spec = FAMILIES["openai_chat"]
    wire = Wire(rejected, spec.reply())
    message = Message(role="user", content="Look", images=[Image(url=url)])

    turn = await complete(_provider("openai_chat", tmp_path, wire), ModelRequest(messages=[message]))

    assert turn.content == "ok"
    assert wire.body(0)["messages"][-1]["content"][1] == {"type": "image_url", "image_url": {"url": url}}
    assert spec.user_text(wire.body()) == f"Look\n\n{_NOTE}"
    assert downloaded == []


@pytest.mark.asyncio
async def test_downloaded_audio_without_a_format_keeps_the_type_its_server_or_url_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio a tool returns as a bare URL, such as an MP3, is sent as the format its server or URL name says.

    A declared type that names no audio leaves the URL's name to decide, and an explicit format stands whatever the
    server declares.
    """
    _media_server(
        monkeypatch,
        {
            "https://media.test/speech": httpx.Response(200, content=b"ID3-a", headers={"content-type": "audio/mpeg"}),
            "https://media.test/song.mp3": httpx.Response(200, content=b"ID3-b"),
            "https://media.test/track.mp3": httpx.Response(
                200,
                content=b"ID3-d",
                headers={"content-type": "application/octet-stream"},
            ),
            "https://media.test/clip": httpx.Response(200, content=b"RIFF-c", headers={"content-type": "audio/mpeg"}),
        },
    )
    audio = [
        Audio(url="https://media.test/speech"),
        Audio(url="https://media.test/song.mp3"),
        Audio(url="https://media.test/track.mp3"),
        Audio(url="https://media.test/clip", format="wav"),
    ]
    chat_wire, gemini_wire = Wire(FAMILIES["openai_chat"].reply()), Wire(FAMILIES["gemini"].reply())
    request = ModelRequest(messages=[Message(role="user", content="Listen", audio=audio)])

    await complete(_provider("openai_chat", tmp_path, chat_wire), request)
    await complete(_provider("gemini", tmp_path, gemini_wire), request)

    chat_parts = chat_wire.body()["messages"][-1]["content"][1:]
    assert [part["input_audio"]["format"] for part in chat_parts] == ["mp3", "mp3", "mp3", "wav"]
    gemini_parts = gemini_wire.body()["contents"][0]["parts"][1:]
    assert [part["inlineData"]["mimeType"] for part in gemini_parts] == [
        "audio/mpeg",
        "audio/mp3",
        "audio/mp3",
        "audio/wav",
    ]


@pytest.mark.asyncio
async def test_a_downloaded_file_the_server_calls_octet_stream_is_typed_and_named_by_its_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``application/octet-stream`` names no type, so a file's URL name types and names it, as worker media does."""
    untyped = {"content-type": "application/octet-stream"}
    _media_server(
        monkeypatch,
        {"https://media.test/files/report.pdf": httpx.Response(200, content=_PDF, headers=untyped)},
    )
    wire = Wire(FAMILIES["openai_chat"].reply())
    message = Message(role="user", content="Read", files=[File(url="https://media.test/files/report.pdf")])

    await complete(_provider("openai_chat", tmp_path, wire), ModelRequest(messages=[message]))

    assert wire.body()["messages"][-1]["content"][0] == {
        "type": "file",
        "file": {"filename": "report.pdf", "file_data": f"data:application/pdf;base64,{_b64(_PDF)}"},
    }


@pytest.mark.asyncio
async def test_media_downloads_go_through_the_model_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downloads share the model requests' client, so they trust the same certificates and proxies."""
    downloaded: list[str] = []

    def respond(request: httpx2.Request) -> httpx.Response:
        downloaded.append(str(request.url))
        return httpx.Response(200, content=_PNG)

    pool = httpx2.AsyncClient(transport=httpx2_transport(respond), follow_redirects=True)
    monkeypatch.setattr(model_http_client, "_loop_client", lambda: pool)
    wire = Wire(FAMILIES["ollama"].reply())
    message = Message(role="user", content="Look", images=[Image(url="https://media.test/generated")])

    await complete(_provider("ollama", tmp_path, wire), ModelRequest(messages=[message]))

    assert downloaded == ["https://media.test/generated"]
    assert wire.body()["messages"][-1]["images"] == [_b64(_PNG)]
