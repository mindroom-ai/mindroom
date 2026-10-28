"""Bounded media result transport shared by worker-backed tools."""

import json

import pytest

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.tool_system import media_transport
from mindroom.tool_system.media_transport import (
    decode_media_result,
    encode_media_result,
    is_media_result_envelope,
)
from mindroom.tool_system.tool_function import ToolValue


def test_media_result_roundtrips_image_bytes_without_json_stringification() -> None:
    """Image bytes and their bounded history marker survive JSON transport."""
    result = ToolValue(
        content='{"view_status":"ready"}',
        images=(Image(id="mindroom_viewed_fixture", content=b"png", mime_type="image/png"),),
    )

    envelope = json.loads(json.dumps(encode_media_result(result)))
    decoded = decode_media_result(envelope)

    assert is_media_result_envelope(envelope)
    assert isinstance(decoded, ToolValue)
    assert decoded.content == result.content
    assert decoded.images
    assert decoded.images[0].content == b"png"
    assert decoded.images[0].id == "mindroom_viewed_fixture"


def test_mixed_tool_media_preserves_bytes_and_metadata() -> None:
    """All result media share one typed transport instead of string representations."""
    original = ToolValue(
        content="Generated artifacts",
        images=(Image(id="image", content=b"png", mime_type="image/png", revised_prompt="A tree"),),
        audios=(Audio(id="audio", content=b"mp3", mime_type="audio/mp3", duration=1.5, transcript="Hello"),),
        videos=(Video(id="video", content=b"mp4", mime_type="video/mp4", width=640, height=480),),
        files=(File(id="file", content=b"report", mime_type="text/plain", filename="report.txt"),),
    )

    decoded = decode_media_result(json.loads(json.dumps(encode_media_result(original))))

    assert decoded == original


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_mixed_media_obeys_shared_limits(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    """Individually valid media cannot bypass aggregate bounds by changing kind."""
    result = ToolValue(
        content="Mixed media",
        images=(Image(content=b"123", mime_type="image/png"),),
        audios=(Audio(content=b"456", mime_type="audio/mp3"),),
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
    envelope = encode_media_result(ToolValue(content="image", images=(Image(content=b"png", mime_type="image/png"),)))
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


def _envelope(field: str, entry: dict[str, object]) -> dict[str, object]:
    value: dict[str, object] = {"version": 1, "kind": "tool_result", "content": "", "images": [], "audios": []}
    value.update({"videos": [], "files": [], field: [{**entry, "data_base64": "cG5n"}]})
    return {"mindroom_tool_result": value}


@pytest.mark.parametrize(
    ("field", "entry"),
    [
        ("images", {"detail": 5}),
        ("images", {"metadata": ["seed"]}),
        ("audios", {"sample_rate": True}),
        ("audios", {"duration": "long"}),
        ("videos", {"width": 1.5}),
        ("files", {"size": "6"}),
        ("files", {"citations": True}),
        ("files", {"mime_type": "image/png"}),
    ],
)
def test_decoder_rejects_attributes_their_media_type_does_not_declare(field: str, entry: dict[str, object]) -> None:
    """A medium attribute of another type, an unknown one, or a file type models never read fails the envelope."""
    with pytest.raises(ValueError, match="worker tool result"):
        decode_media_result(_envelope(field, entry))


def test_decoder_reads_an_integer_for_a_float_and_a_null_for_any_attribute() -> None:
    """A whole-number duration and an explicit null decode as the medium's own values; a null id takes a new one."""
    decoded = decode_media_result(_envelope("audios", {"id": None, "duration": 2, "transcript": None}))

    assert isinstance(decoded, ToolValue)
    [audio] = decoded.audios
    assert (audio.content, audio.duration, audio.transcript, audio.sample_rate) == (b"png", 2, None, 24000)
    assert audio.id


@pytest.mark.parametrize("source", [{"url": "https://example.org/report.txt"}, {"filepath": "/workspace/report.txt"}])
def test_encoder_refuses_media_that_still_names_a_resource(source: dict[str, str]) -> None:
    """A medium crosses the wire only as its inline bytes, so one that still names a path or URL is refused."""
    report = File(content=b"report", mime_type="text/plain", **source)

    with pytest.raises(ValueError, match="worker tool result"):
        encode_media_result(ToolValue(content="report", files=(report,)))
