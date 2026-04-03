"""Tests for MCP tool result conversion."""

from __future__ import annotations

import base64

import pytest
from agno.tools.function import ToolResult
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
from mindroom.mcp.results import tool_result_from_call_result


def test_tool_result_from_call_result_converts_text_images_and_resources() -> None:
    """Convert mixed MCP content blocks into one Agno tool result."""
    image_bytes = b"fake-image"
    result = tool_result_from_call_result(
        "demo",
        CallToolResult(
            content=[
                TextContent(type="text", text="hello"),
                ImageContent(type="image", data=base64.b64encode(image_bytes).decode("utf-8"), mimeType="image/png"),
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri="file:///tmp/demo.txt",
                        text="embedded text",
                        mimeType="text/plain",
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
            structuredContent={"ok": True},
        ),
    )
    assert isinstance(result, ToolResult)
    assert result.content.startswith("hello")
    assert "embedded text" in result.content
    assert "structuredContent" not in result.content
    assert '{"ok": true}' in result.content
    assert result.images is not None
    assert result.images[0].content == image_bytes


def test_tool_result_from_call_result_raises_on_error() -> None:
    """Raise a typed error when the MCP server reports a failed tool call."""
    with pytest.raises(MCPToolCallError, match="tool exploded"):
        tool_result_from_call_result(
            "demo",
            CallToolResult(
                content=[TextContent(type="text", text="tool exploded")],
                isError=True,
                structuredContent={"code": "boom"},
            ),
        )


def test_tool_result_from_call_result_summarizes_binary_embedded_resources() -> None:
    """Summarize embedded binary resources instead of inlining opaque bytes."""
    result = tool_result_from_call_result(
        "demo",
        CallToolResult(
            content=[
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri="file:///tmp/demo.bin",
                        blob=base64.b64encode(b"abc").decode("utf-8"),
                        mimeType="application/octet-stream",
                    ),
                ),
            ],
        ),
    )
    assert "binary blob" in result.content


def test_tool_result_from_call_result_converts_audio_blocks() -> None:
    """Convert MCP audio content blocks into Agno audio artifacts."""
    audio_bytes = b"fake-audio"
    result = tool_result_from_call_result(
        "demo",
        CallToolResult(
            content=[
                AudioContent(
                    type="audio",
                    data=base64.b64encode(audio_bytes).decode("utf-8"),
                    mimeType="audio/ogg",
                ),
            ],
        ),
    )
    assert isinstance(result, ToolResult)
    assert result.audios is not None
    assert result.audios[0].content == audio_bytes
    assert result.audios[0].mime_type == "audio/ogg"
