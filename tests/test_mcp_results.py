"""Tests for MCP tool result conversion."""

from __future__ import annotations

import base64

import pytest
from mcp.types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

from mindroom.mcp.errors import MCPToolCallError
from mindroom.mcp.results import tool_value_from_call_result


def test_tool_value_from_call_result_converts_text_images_and_resources() -> None:
    """Convert mixed MCP content blocks into one tool value."""
    image_bytes = b"fake-image"
    result = tool_value_from_call_result(
        "demo",
        CallToolResult(
            content=[
                TextContent(type="text", text="hello"),
                ImageContent(type="image", data=base64.b64encode(image_bytes).decode("utf-8"), mime_type="image/png"),
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri="file:///tmp/demo.txt",
                        text="embedded text",
                        mime_type="text/plain",
                    ),
                ),
                ResourceLink(
                    type="resource_link",
                    uri="file:///tmp/other.txt",
                    name="other",
                    title="Other",
                    description="linked resource",
                ),
            ],
            structured_content={"ok": True},
        ),
    )
    assert result.content.startswith("hello")
    assert "embedded text" in result.content
    assert "structuredContent" not in result.content
    assert '{"ok": true}' in result.content
    assert result.images
    assert result.images[0].content == image_bytes


def test_tool_value_from_call_result_raises_on_error() -> None:
    """Raise a typed error when the MCP server reports a failed tool call."""
    with pytest.raises(MCPToolCallError, match="tool exploded"):
        tool_value_from_call_result(
            "demo",
            CallToolResult(
                content=[TextContent(type="text", text="tool exploded")],
                is_error=True,
                structured_content={"code": "boom"},
            ),
        )


def test_tool_value_from_call_result_summarizes_binary_embedded_resources() -> None:
    """Summarize embedded binary resources instead of inlining opaque bytes."""
    result = tool_value_from_call_result(
        "demo",
        CallToolResult(
            content=[
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri="file:///tmp/demo.bin",
                        blob=base64.b64encode(b"abc").decode("utf-8"),
                        mime_type="application/octet-stream",
                    ),
                ),
            ],
        ),
    )
    assert "binary blob" in result.content


def test_tool_value_from_call_result_converts_audio_blocks() -> None:
    """Convert MCP audio content blocks into audio media."""
    audio_bytes = b"fake-audio"
    result = tool_value_from_call_result(
        "demo",
        CallToolResult(
            content=[
                AudioContent(
                    type="audio",
                    data=base64.b64encode(audio_bytes).decode("utf-8"),
                    mime_type="audio/ogg",
                ),
            ],
        ),
    )
    assert result.audios
    assert result.audios[0].content == audio_bytes
    assert result.audios[0].mime_type == "audio/ogg"


def test_tool_value_from_call_result_keeps_invalid_base64_media_as_utf8_bytes() -> None:
    """Keep permissive MCP media decoding for non-base64 payloads."""
    image_data = "not-base64-image"
    audio_data = "not-base64-audio"
    result = tool_value_from_call_result(
        "demo",
        CallToolResult(
            content=[
                ImageContent(type="image", data=image_data, mime_type="image/png"),
                AudioContent(type="audio", data=audio_data, mime_type="audio/wav"),
            ],
        ),
    )
    assert result.images
    assert result.images[0].content == image_data.encode("utf-8")
    assert result.images[0].mime_type == "image/png"
    assert result.audios
    assert result.audios[0].content == audio_data.encode("utf-8")
    assert result.audios[0].mime_type == "audio/wav"
