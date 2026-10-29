"""Worker result preparation reads resources before crossing the JSON boundary."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.media import Audio, File, Image, Video
from agno.tools.function import ToolResult

from mindroom.api import sandbox_runner
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.media import Audio as OwnedAudio
from mindroom.llm.messages import Message
from mindroom.providers.gemini import wire as gemini_wire
from mindroom.providers.openai.media import chat_audio_part, chat_file_part, responses_file_part
from mindroom.tool_system import media_transport
from mindroom.tool_system.agno_toolkits import read_tool_value
from mindroom.tool_system.media_transport import decode_media_result
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.worker_media import serialize_worker_tool_result

if TYPE_CHECKING:
    from pathlib import Path


def _openai_audio_format(result: ToolValue) -> str:
    """Return the format the OpenAI chat wire sends for the first audio of a tool value."""
    part = chat_audio_part(result.audios[0], provider="OpenAI Chat")
    assert part is not None
    return part["input_audio"]["format"]


def _gemini_mime_type(result: ToolValue) -> str:
    """Return the MIME type the Gemini wire sends for the first audio or video of a tool value."""
    message = Message(role="user", audio=result.audios, videos=result.videos)
    route = gemini_wire.MediaRoute(provider="gemini", upload=None)
    contents, _ = asyncio.run(gemini_wire.format_contents([message], route))
    return contents[0]["parts"][0]["inline_data"]["mime_type"]


@pytest.mark.asyncio
async def test_invalid_media_is_a_tool_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad toolkit output must not be mistaken for failure of a healthy worker."""

    async def invalid_output(*_args: object, **_kwargs: object) -> ToolResult:
        return ToolResult(content="Empty artifact", images=[Image(content=b"", mime_type="image/png")])

    monkeypatch.setattr(sandbox_runner, "_run_toolkit_entrypoint", invalid_output)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )
    config = Config.validate_with_runtime({}, paths)
    request = sandbox_runner.PreparedSandboxRunnerExecuteRequest(
        tool_name="calculator",
        function_name="add",
        kwargs={"a": 1, "b": 2},
    )

    response = await sandbox_runner._execute_prepared_request_inprocess(request, paths, config)

    assert response.ok is False
    assert response.failure_kind == "tool"
    assert response.error is not None
    assert "media" in response.error


def test_worker_file_result_becomes_inline_bytes(tmp_path: Path) -> None:
    """A generated path is usable after the worker file no longer exists."""
    path = tmp_path / "generated.png"
    path.write_bytes(b"generated-image")
    result = ToolResult(content="Generated image", images=[Image(filepath=path, mime_type="image/png")])

    envelope = serialize_worker_tool_result(result)
    path.unlink()
    wire = json.dumps(envelope)
    decoded = decode_media_result(json.loads(wire))

    assert str(path) not in wire
    assert isinstance(decoded, ToolValue)
    assert decoded.images
    assert decoded.images[0].content == b"generated-image"
    assert decoded.images[0].filepath is None


def test_worker_text_file_preserves_utf8_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agno text file content has the same bytes and byte limit after transport."""
    result = ToolResult(
        content="Text file",
        files=[File(content="h\u00e9llo", mime_type="text/plain", filename="note.txt")],
    )

    decoded = decode_media_result(serialize_worker_tool_result(result))

    assert isinstance(decoded, ToolValue)
    assert decoded.files
    assert decoded.files[0].content == "h\u00e9llo".encode()
    monkeypatch.setattr(media_transport, "MAX_MEDIA_BYTES", 5)
    with pytest.raises(ValueError, match="byte limit"):
        serialize_worker_tool_result(result)


@pytest.mark.parametrize("source", ["file", "url"])
@pytest.mark.parametrize("explicit", [False, True])
def test_document_name_and_type_survive_inlining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    explicit: bool,
) -> None:
    """Provider payloads retain document identity after worker resources disappear."""
    path = tmp_path / "report one.txt"
    path.write_bytes(b"document")

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"document"),
            headers={"Content-Type": "application/octet-stream"},
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    values: dict[str, Any] = (
        {"filepath": path} if source == "file" else {"url": "https://8.8.8.8/export/report%20one.txt?key=fixture"}
    )
    if explicit:
        values.update(filename="chosen.csv", mime_type="text/csv")
    result = ToolResult(content="Document", files=[File(**values)])

    decoded = decode_media_result(serialize_worker_tool_result(result))
    path.unlink()

    assert isinstance(decoded, ToolValue)
    assert decoded.files
    document = decoded.files[0]
    assert document.filepath is None
    assert document.url is None
    assert document.filename == ("chosen.csv" if explicit else "report one.txt")
    assert document.mime_type == ("text/csv" if explicit else "text/plain")
    payload = chat_file_part(decoded.files[0], provider="OpenAI Chat")
    assert payload is not None
    assert payload["file"]["filename"] == document.filename
    assert payload["file"]["file_data"].startswith(f"data:{document.mime_type};base64,")


@pytest.mark.parametrize("source", ["file", "url"])
@pytest.mark.parametrize("kind", ["mp3", "wav", "webm", "jpg", "avif"])
def test_media_source_type_survives_inlining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    kind: str,
) -> None:
    """Typed media keeps source-derived format after paths and URLs are removed."""
    path = tmp_path / f"generated.{kind}"
    path.write_bytes(b"media-content")

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"media-content"), request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    values: dict[str, Any] = {"filepath": path} if source == "file" else {"url": f"https://8.8.8.8/generated.{kind}"}
    if kind in {"mp3", "wav"}:
        field, media = "audios", Audio(**values)
    elif kind == "webm":
        field, media = "videos", Video(**values)
    else:
        field, media = "images", Image(**values)

    decoded = decode_media_result(serialize_worker_tool_result(ToolResult(content="Media", **{field: [media]})))
    path.unlink()

    assert isinstance(decoded, ToolValue)
    if kind in {"mp3", "wav"}:
        assert decoded.audios
        item = decoded.audios[0]
    elif kind == "webm":
        assert decoded.videos
        item = decoded.videos[0]
    else:
        assert decoded.images
        item = decoded.images[0]
    assert item.filepath is None
    assert item.url is None
    if kind in {"mp3", "wav"}:
        assert isinstance(item, OwnedAudio)
        assert _openai_audio_format(decoded) == kind
        assert _gemini_mime_type(decoded) == f"audio/{kind}"
    elif kind == "webm":
        assert item.format == "webm"
        assert _gemini_mime_type(decoded) == "video/webm"
    else:
        assert item.format is None
        assert item.mime_type == ("image/jpeg" if kind == "jpg" else "image/avif")


def test_explicit_media_format_precedes_resource_name(tmp_path: Path) -> None:
    """Explicit format must not be overridden by a mismatching download name."""
    path = tmp_path / "download.wav"
    path.write_bytes(b"mp3-content")
    result = ToolResult(content="Audio", audios=[Audio(filepath=path, format="mp3")])

    decoded = decode_media_result(serialize_worker_tool_result(result))

    assert isinstance(decoded, ToolValue)
    assert decoded.audios
    assert decoded.audios[0].mime_type is None
    assert _openai_audio_format(decoded) == "mp3"
    assert _gemini_mime_type(decoded) == "audio/mp3"


def test_unknown_file_type_remains_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic HTTP header must not introduce an unsupported File MIME value."""

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"document"),
            headers={"Content-Type": "application/octet-stream"},
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    result = ToolResult(content="Document", files=[File(url="https://8.8.8.8/download")])
    decoded = decode_media_result(serialize_worker_tool_result(result))

    assert isinstance(decoded, ToolValue)
    assert decoded.files
    assert decoded.files[0].mime_type is None
    assert decoded.files[0].filename == "download"


def test_a_declared_type_files_cannot_take_leaves_the_file_name_to_type_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A header naming a type outside the File types the transport accepts gives way to the URL's name."""

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"notes"),
            headers={"Content-Type": "image/png"},
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    result = ToolResult(content="Document", files=[File(url="https://8.8.8.8/export/notes.txt")])
    decoded = decode_media_result(serialize_worker_tool_result(result))

    assert isinstance(decoded, ToolValue)
    assert decoded.files
    assert (decoded.files[0].filename, decoded.files[0].mime_type) == ("notes.txt", "text/plain")


def test_worker_file_result_rejects_nonregular_file(tmp_path: Path) -> None:
    """Directories and devices cannot become media artifacts."""
    result = ToolResult(content="Invalid image", images=[Image(filepath=tmp_path, mime_type="image/png")])

    with pytest.raises(ValueError, match="media"):
        serialize_worker_tool_result(result)


def test_worker_url_result_becomes_inline_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public media downloads happen on the worker through the guarded transport."""

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://8.8.8.8/generated.png"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"remote-image"),
            headers={"Content-Type": "image/png"},
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    result = ToolResult(content="Generated image", images=[Image(url="https://8.8.8.8/generated.png")])

    envelope = serialize_worker_tool_result(result)
    decoded = decode_media_result(json.loads(json.dumps(envelope)))

    assert isinstance(decoded, ToolValue)
    assert decoded.images
    assert decoded.images[0].content == b"remote-image"
    assert decoded.images[0].url is None
    assert decoded.images[0].mime_type == "image/png"


@pytest.mark.parametrize("url", ["file:///private/file", "http://127.0.0.1/media", "http://169.254.169.254/latest"])
def test_worker_url_rejects_local_resources(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Invalid destinations must fail before the HTTP transport opens a connection."""

    def forbidden(_transport: httpx.HTTPTransport, _request: httpx.Request) -> httpx.Response:
        pytest.fail("Forbidden media URL reached the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    result = ToolResult(content="Invalid image", images=[Image(url=url)])

    with pytest.raises(ValueError, match="media"):
        serialize_worker_tool_result(result)


def test_worker_media_redirect_cannot_reach_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect requests pass through the same destination guard."""

    def redirect(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "8.8.8.8"
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", redirect)
    result = ToolResult(content="Redirect", images=[Image(url="https://8.8.8.8/media")])

    with pytest.raises(ValueError, match="media"):
        serialize_worker_tool_result(result)


@pytest.mark.parametrize("source", ["file", "url"])
def test_worker_resource_read_obeys_media_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    """Worker reads cannot bypass the same bound enforced by the wire codec."""
    monkeypatch.setattr(media_transport, "MAX_MEDIA_BYTES", 4)
    path = tmp_path / "oversized.png"
    path.write_bytes(b"12345")

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"12345"), request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    image = Image(filepath=path) if source == "file" else Image(url="https://8.8.8.8/media")
    with pytest.raises(ValueError, match="media"):
        serialize_worker_tool_result(ToolResult(content="Large", images=[image]))


@pytest.mark.parametrize(
    ("path", "header", "explicit_mime", "explicit_format", "expected_format"),
    [
        ("/download", "audio/mpeg", None, None, "mp3"),
        ("/", "audio/mpeg", None, None, "mp3"),
        ("/download.bin", "audio/mpeg", None, None, "mp3"),
        ("/download.unknown", "audio/mpeg", None, None, "mp3"),
        ("/download", "audio/mp3", None, None, "mp3"),
        ("/download", "audio/wave", None, None, "wav"),
        ("/download", "audio/x-wav", None, None, "wav"),
        ("/download", " Audio/MPEG ; charset=binary", None, None, "mp3"),
        ("/download", "audio/wav", "audio/mpeg", None, "mp3"),
        ("/download", "audio/mpeg", "audio/wav", None, "wav"),
        ("/download", "audio/mpeg", "audio/x-unknown", None, None),
        ("/download.wav", "audio/wav", None, "mp3", "mp3"),
        ("/download.mp3", "audio/wav", None, None, "mp3"),
        ("/download.wav", "audio/mpeg", None, None, "wav"),
        ("/download.ogg", "audio/mpeg", None, None, "ogg"),
        ("/download.webm", "audio/mpeg", None, None, "webm"),
        ("/download.unknown", "application/octet-stream", None, None, "unknown"),
        ("/download.unknown", "audio/x-unknown", None, None, "unknown"),
        ("/download", "audio/x-unknown", None, None, None),
    ],
)
def test_worker_audio_mime_fallback_reaches_provider(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    header: str,
    explicit_mime: str | None,
    explicit_format: str | None,
    expected_format: str | None,
) -> None:
    """OpenAI sees the audio type after worker resource references disappear."""

    def respond(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"audio-content"),
            headers={"Content-Type": header},
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    source = Audio(
        url=f"https://8.8.8.8{path}",
        mime_type=explicit_mime,
        format=explicit_format,
    )

    wire = json.dumps(serialize_worker_tool_result(ToolResult(content="Audio", audios=[source])))
    decoded = decode_media_result(json.loads(wire))

    assert isinstance(decoded, ToolValue)
    assert decoded.audios
    audio = decoded.audios[0]
    assert audio.content == b"audio-content"
    assert audio.url is None
    assert audio.filepath is None
    assert audio.format == expected_format
    if explicit_mime is not None:
        assert audio.mime_type == explicit_mime
    assert _openai_audio_format(decoded) == (expected_format or "wav")


@pytest.mark.parametrize("source", ["file", "content"])
def test_worker_audio_explicit_mime_supplies_missing_format(
    tmp_path: Path,
    source: str,
) -> None:
    """Explicit MIME also identifies extensionless files and existing inline bytes."""
    path = tmp_path / "download"
    path.write_bytes(b"mp3-content")
    values: dict[str, Any] = {"filepath": path} if source == "file" else {"content": b"mp3-content"}
    audio = Audio(**values, mime_type="audio/mpeg")

    envelope = serialize_worker_tool_result(ToolResult(content="Audio", audios=[audio]))
    path.unlink()
    decoded = decode_media_result(json.loads(json.dumps(envelope)))

    assert isinstance(decoded, ToolValue)
    assert decoded.audios
    item = decoded.audios[0]
    assert item.content == b"mp3-content"
    assert item.filepath is None
    assert item.mime_type == "audio/mpeg"
    assert item.format == "mp3"
    assert _openai_audio_format(decoded) == "mp3"


# What a worker sends the primary for one result with media of every kind, byte for byte. A worker and the primary it
# serves may run different releases while a deployment rolls out, so this is the wire both sides must keep.
_PINNED_WIRE = (
    '{"mindroom_tool_result": {"version": 1, "kind": "tool_result", "content": "Generated artifacts", "images": '
    '[{"id": "image-1", "mime_type": "image/png", "detail": "high", "original_prompt": "a tree", "revised_prompt": '
    '"an oak", "alt_text": "An oak", "metadata": {"seed": 7}, "data_base64": "iVBORy1ieXRlcw=="}, {"id": "image-2", '
    '"mime_type": "image/png", "data_base64": "iVBORy1pbWFnZQ=="}], "audios": [{"id": "audio-1", "format": "mp3", '
    '"duration": 1.5, "sample_rate": 24000, "channels": 1, "transcript": "Hello", "data_base64": "SUQzLWF1ZGlv"}], '
    '"videos": [{"id": "video-1", "mime_type": "video/mp4", "duration": 2.0, "width": 640, "height": 480, "fps": '
    '30.0, "eta": "now", "data_base64": "bXA0LWJ5dGVz"}], "files": [{"id": "file-1", "mime_type": "text/plain", '
    '"filename": "note.txt", "size": 6, "format": "txt", "name": "note", "data_base64": "aMOpbGxv"}, {"id": '
    '"file-2", "mime_type": "application/pdf", "file_type": "document", "data_base64": "JVBERg=="}, {"id": "file-3", '
    '"mime_type": "text/csv", "filename": "table.csv", "data_base64": "YSxi"}, {"id": "file-4", "filename": '
    '"photo.png", "data_base64": "cG5n"}]}}'
)


def test_the_worker_media_wire_is_pinned_byte_for_byte(tmp_path: Path) -> None:
    """A tool's media crosses the worker wire in the pinned bytes, and the pinned bytes decode to the same media."""
    (tmp_path / "clip.mp3").write_bytes(b"ID3-audio")
    (tmp_path / "page.png").write_bytes(b"\x89PNG-image")
    (tmp_path / "table.csv").write_bytes(b"a,b")
    (tmp_path / "photo.png").write_bytes(b"png")
    result = ToolResult(
        content="Generated artifacts",
        images=[
            Image(
                id="image-1",
                content=b"\x89PNG-bytes",
                mime_type="image/png",
                detail="high",
                original_prompt="a tree",
                revised_prompt="an oak",
                alt_text="An oak",
                metadata={"seed": 7},
            ),
            Image(id="image-2", filepath=tmp_path / "page.png"),
        ],
        audios=[Audio(id="audio-1", filepath=tmp_path / "clip.mp3", duration=1.5, transcript="Hello")],
        videos=[
            Video(
                id="video-1",
                content=b"mp4-bytes",
                mime_type="video/mp4",
                duration=2.0,
                width=640,
                height=480,
                fps=30.0,
                eta="now",
            ),
        ],
        files=[
            File(
                id="file-1",
                content="héllo",
                mime_type="text/plain",
                filename="note.txt",
                size=6,
                format="txt",
                name="note",
            ),
            File(id="file-2", content=b"%PDF", mime_type="application/pdf", file_type="document"),
            File(id="file-3", filepath=tmp_path / "table.csv"),
            # A guessed type outside the document types a file may carry stays unset.
            File(id="file-4", filepath=tmp_path / "photo.png"),
        ],
    )

    assert json.dumps(serialize_worker_tool_result(result)) == _PINNED_WIRE
    assert json.dumps(serialize_worker_tool_result(read_tool_value(result))) == _PINNED_WIRE
    decoded = decode_media_result(json.loads(_PINNED_WIRE))
    assert isinstance(decoded, ToolValue)
    assert decoded.content == "Generated artifacts"
    assert [(item.id, item.content, item.mime_type) for _kind, item in decoded.media()] == [
        ("image-1", b"\x89PNG-bytes", "image/png"),
        ("image-2", b"\x89PNG-image", "image/png"),
        ("audio-1", b"ID3-audio", None),
        ("video-1", b"mp4-bytes", "video/mp4"),
        ("file-1", "héllo".encode(), "text/plain"),
        ("file-2", b"%PDF", "application/pdf"),
        ("file-3", b"a,b", "text/csv"),
        ("file-4", b"png", None),
    ]
    [first, _second] = decoded.images
    assert (first.detail, first.original_prompt, first.revised_prompt, first.alt_text, first.metadata) == (
        "high",
        "a tree",
        "an oak",
        "An oak",
        {"seed": 7},
    )
    [audio] = decoded.audios
    assert (audio.format, audio.duration, audio.sample_rate, audio.channels, audio.transcript) == (
        "mp3",
        1.5,
        24000,
        1,
        "Hello",
    )
    [video] = decoded.videos
    assert (video.duration, video.width, video.height, video.fps, video.eta) == (2.0, 640, 480, 30.0, "now")
    note, pdf, table, photo = decoded.files
    assert (note.filename, note.size, note.format, note.name) == ("note.txt", 6, "txt", "note")
    assert pdf.file_type == "document"
    assert (table.filename, photo.filename) == ("table.csv", "photo.png")


def test_a_worker_envelope_with_the_tools_metadata_still_decodes() -> None:
    """An earlier worker sends a tool's own metadata beside its media; the primary bounds it and reads past it."""
    envelope = json.loads(_PINNED_WIRE)
    envelope["mindroom_tool_result"]["metadata"] = {"job": "complete"}

    assert decode_media_result(envelope) == decode_media_result(json.loads(_PINNED_WIRE))
    envelope["mindroom_tool_result"]["metadata"] = {"job": "x" * (64 * 1024)}
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(envelope)


# What a worker sent for files a tool returned as bytes beside the path it saved them under, before tools returned
# MindRoom's tool values.
_BYTES_BESIDE_A_PATH_WIRE = (
    '{"mindroom_tool_result": {"version": 1, "kind": "tool_result", "content": "Created", "images": [], "audios": [], '
    '"videos": [], "files": [{"id": "report", "mime_type": "application/pdf", "filename": "report.pdf", '
    '"data_base64": "JVBERi0xLjc="}, {"id": "named", "mime_type": "application/pdf", "filename": "Report", "name": '
    '"Report", "data_base64": "JVBERi0xLjc="}, {"id": "typed", "mime_type": "text/csv", "filename": "table.csv", '
    '"data_base64": "YSxi"}, {"id": "unknown", "filename": "blob.bin", "data_base64": "AA=="}]}}'
)


def test_a_file_returned_as_bytes_beside_its_path_keeps_the_paths_name_and_type() -> None:
    """A file a tool returns as bytes with the path it saved them under is named and typed by that path, unread.

    The paths here do not exist, so neither the worker nor the primary can have read them.
    """
    result = ToolResult(
        content="Created",
        files=[
            File(id="report", content=b"%PDF-1.7", filepath="report.pdf"),
            File(id="named", content=b"%PDF-1.7", filepath="out/report.pdf", name="Report"),
            File(id="typed", content=b"a,b", filepath="export.bin", filename="table.csv"),
            File(id="unknown", content=b"\x00", filepath="blob.bin"),
        ],
    )

    wire = json.dumps(serialize_worker_tool_result(result))
    owned = read_tool_value(result)

    assert wire == _BYTES_BESIDE_A_PATH_WIRE
    assert owned is not None
    assert [(file.filepath, file.filename, file.mime_type) for file in owned.files] == [
        (None, "report.pdf", "application/pdf"),
        (None, "Report", "application/pdf"),
        (None, "table.csv", "text/csv"),
        (None, "blob.bin", None),
    ]
    decoded = decode_media_result(json.loads(wire))
    assert isinstance(decoded, ToolValue)
    part = responses_file_part(decoded.files[0], provider="OpenAI Responses")
    assert part is not None
    assert (part["filename"], part["file_data"].split(";", 1)[0]) == ("report.pdf", "data:application/pdf")
