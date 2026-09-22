"""Worker result preparation reads resources before crossing the JSON boundary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.media import Audio, File, Image, Video
from agno.models.google.utils import get_mime_type
from agno.tools.function import ToolResult
from agno.utils.openai import _format_file_for_message, audio_to_message

from mindroom.api import sandbox_runner
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system import media_transport
from mindroom.tool_system.media_transport import decode_media_result
from mindroom.tool_system.worker_media import serialize_worker_tool_result

if TYPE_CHECKING:
    from pathlib import Path


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
    assert isinstance(decoded, ToolResult)
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

    assert isinstance(decoded, ToolResult)
    assert decoded.files
    assert decoded.files[0].get_content_bytes() == "h\u00e9llo".encode()
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

    assert isinstance(decoded, ToolResult)
    assert decoded.files
    document = decoded.files[0]
    assert document.filepath is None
    assert document.url is None
    assert document.filename == ("chosen.csv" if explicit else "report one.txt")
    assert document.mime_type == ("text/csv" if explicit else "text/plain")
    payload = _format_file_for_message(document)
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

    assert isinstance(decoded, ToolResult)
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
        assert isinstance(item, Audio)
        assert audio_to_message([item])[0]["input_audio"]["format"] == kind
        assert get_mime_type(item, "audio/mpeg") == f"audio/{kind}"
    elif kind == "webm":
        assert item.format == "webm"
        assert get_mime_type(item, "video/mp4") == "video/webm"
    else:
        assert item.format is None
        assert item.mime_type == ("image/jpeg" if kind == "jpg" else "image/avif")


def test_explicit_media_format_precedes_resource_name(tmp_path: Path) -> None:
    """Explicit format must not be overridden by a mismatching download name."""
    path = tmp_path / "download.wav"
    path.write_bytes(b"mp3-content")
    result = ToolResult(content="Audio", audios=[Audio(filepath=path, format="mp3")])

    decoded = decode_media_result(serialize_worker_tool_result(result))

    assert isinstance(decoded, ToolResult)
    assert decoded.audios
    assert decoded.audios[0].mime_type is None
    assert audio_to_message(decoded.audios)[0]["input_audio"]["format"] == "mp3"
    assert get_mime_type(decoded.audios[0], "audio/wav") == "audio/mp3"


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

    assert isinstance(decoded, ToolResult)
    assert decoded.files
    assert decoded.files[0].mime_type is None
    assert decoded.files[0].filename == "download"


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

    assert isinstance(decoded, ToolResult)
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
