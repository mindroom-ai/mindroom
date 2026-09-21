"""Bounded media result transport shared by worker-backed tools."""

import json

import pytest
from agno.media import Image
from agno.tools.function import ToolResult

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


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"mindroom_browser_mcp_result": {}},
        {"mindroom_browser_mcp_result": {"version": 1, "kind": "tool_result", "content": "missing images"}},
    ],
)
def test_media_result_rejects_missing_or_malformed_envelopes(payload: object) -> None:
    """Missing fields and invalid versions fail closed at the transport boundary."""
    with pytest.raises(ValueError, match="browser MCP result"):
        decode_media_result(payload)


def test_media_result_envelope_detection_does_not_claim_ordinary_json() -> None:
    """Only the reserved top-level key identifies a media envelope."""
    assert not is_media_result_envelope({"result": "ordinary"})
    assert is_media_result_envelope({"mindroom_browser_mcp_result": {"version": 999}})


@pytest.mark.parametrize("image_id", [7, "x" * 257])
def test_media_result_rejects_invalid_image_ids(image_id: object) -> None:
    """Image history markers must be strings within the wire bound."""
    payload = {
        "mindroom_browser_mcp_result": {
            "version": 1,
            "kind": "tool_result",
            "content": "",
            "images": [{"mime_type": "image/png", "data_base64": "cG5n", "id": image_id}],
        },
    }

    with pytest.raises(ValueError, match="browser MCP result"):
        decode_media_result(payload)
