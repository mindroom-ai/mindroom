"""Bounded media result transport shared by worker-backed tools."""

import json

import pytest
from agno.media import Audio, File, Image, Video
from agno.tools.function import ToolResult

from mindroom.tool_system import media_transport
from mindroom.tool_system.media_transport import (
    decode_media_result,
    encode_media_result,
    is_media_result_envelope,
)


def test_media_result_roundtrips_image_bytes_without_json_stringification() -> None:
    """Image bytes and their bounded history marker survive JSON transport."""
    result = ToolResult(
        content='{"view_status":"ready"}',
        images=[Image(id="mindroom_viewed_fixture", content=b"png", mime_type="image/png")],
    )

    envelope = json.loads(json.dumps(encode_media_result(result)))
    decoded = decode_media_result(envelope)

    assert is_media_result_envelope(envelope)
    assert isinstance(decoded, ToolResult)
    assert decoded.content == result.content
    assert decoded.images
    assert decoded.images[0].content == b"png"
    assert decoded.images[0].id == "mindroom_viewed_fixture"


def test_mixed_tool_media_preserves_bytes_and_metadata() -> None:
    """All result media share one typed transport instead of string representations."""
    original = ToolResult(
        content="Generated artifacts",
        metadata={"job": "complete"},
        images=[Image(id="image", content=b"png", mime_type="image/png", revised_prompt="A tree")],
        audios=[Audio(id="audio", content=b"mp3", mime_type="audio/mp3", duration=1.5, transcript="Hello")],
        videos=[Video(id="video", content=b"mp4", mime_type="video/mp4", width=640, height=480)],
        files=[File(id="file", content=b"report", mime_type="text/plain", filename="report.txt")],
    )

    decoded = decode_media_result(json.loads(json.dumps(encode_media_result(original))))

    assert isinstance(decoded, ToolResult)
    assert decoded == original


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_mixed_media_obeys_shared_limits(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    """Individually valid media cannot bypass aggregate bounds by changing kind."""
    result = ToolResult(
        content="Mixed media",
        images=[Image(content=b"123", mime_type="image/png")],
        audios=[Audio(content=b"456", mime_type="audio/mp3")],
    )
    envelope = encode_media_result(result)
    if limit == "count":
        monkeypatch.setattr(media_transport, "MAX_MEDIA_COUNT", 1)
    else:
        monkeypatch.setattr(media_transport, "MAX_TOTAL_MEDIA_BYTES", 5)

    with pytest.raises(ValueError, match="worker tool result"):
        encode_media_result(result)
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(envelope)


@pytest.mark.parametrize("source", ["url", "filepath", "media_reference", "external"])
def test_decoder_never_restores_worker_resource_references(source: str) -> None:
    """Wire media must not make the primary read a worker-controlled resource."""
    envelope = encode_media_result(ToolResult(content="image", images=[Image(content=b"png", mime_type="image/png")]))
    value = next(iter(envelope.values()))
    value["images"][0][source] = "file:///private/resource"

    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(envelope)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"mindroom_tool_result": {}},
        {
            "mindroom_tool_result": {
                "version": 1,
                "kind": "tool_result",
                "audios": [],
                "videos": [],
                "files": [],
                "content": "missing images",
            },
        },
    ],
)
def test_media_result_rejects_missing_or_malformed_envelopes(payload: object) -> None:
    """Missing fields and invalid versions fail closed at the transport boundary."""
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(payload)


def test_media_result_envelope_detection_does_not_claim_ordinary_json() -> None:
    """Only the reserved top-level key identifies a media envelope."""
    assert not is_media_result_envelope({"result": "ordinary"})
    assert is_media_result_envelope({"mindroom_tool_result": {"version": 999}})


@pytest.mark.parametrize("image_id", [7, "x" * 257])
def test_media_result_rejects_invalid_image_ids(image_id: object) -> None:
    """Image history markers must be strings within the wire bound."""
    payload = {
        "mindroom_tool_result": {
            "version": 1,
            "kind": "tool_result",
            "audios": [],
            "videos": [],
            "files": [],
            "content": "",
            "images": [{"mime_type": "image/png", "data_base64": "cG5n", "id": image_id}],
        },
    }

    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(payload)
