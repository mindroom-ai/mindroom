"""Helpers for converting MCP tool responses into tool values."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

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

from mindroom.llm.media import Audio, Image
from mindroom.mcp.errors import MCPToolCallError
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Iterable


def _summarize_embedded_resource(block: EmbeddedResource) -> str:
    resource = block.resource
    if isinstance(resource, TextResourceContents):
        preview = resource.text[:500]
        return f"Embedded resource {resource.uri}: {preview}"
    if isinstance(resource, BlobResourceContents):
        return f"Embedded resource {resource.uri} ({resource.mime_type or 'unknown mime'}, binary blob)"
    return f"Embedded resource {resource.uri} ({resource.mime_type or 'unknown mime'})"


def _summarize_resource_link(block: ResourceLink) -> str:
    details: list[str] = [f"Resource link: {block.uri}"]
    if block.title:
        details.append(f"title={block.title}")
    if block.description:
        details.append(f"description={block.description}")
    if block.mime_type:
        details.append(f"mime={block.mime_type}")
    return " | ".join(details)


def _compact_structured_content(result: CallToolResult) -> str | None:
    if result.structured_content is None:
        return None
    return json.dumps(result.structured_content, sort_keys=True, ensure_ascii=True)


def _text_lines_from_blocks(content_blocks: Iterable[object]) -> list[str]:
    lines: list[str] = []
    for block in content_blocks:
        if isinstance(block, TextContent):
            lines.append(block.text)
        elif isinstance(block, EmbeddedResource):
            lines.append(_summarize_embedded_resource(block))
        elif isinstance(block, ResourceLink):
            lines.append(_summarize_resource_link(block))
    return lines


def _mcp_block_data_as_bytes(data: str) -> bytes:
    try:
        return base64.b64decode(data)
    except Exception:
        return data.encode("utf-8")


def _image_artifacts_from_blocks(content_blocks: Iterable[object]) -> list[Image]:
    images: list[Image] = []
    for block in content_blocks:
        if not isinstance(block, ImageContent):
            continue
        images.append(Image(content=_mcp_block_data_as_bytes(block.data), mime_type=block.mime_type))
    return images


def _audio_artifacts_from_blocks(content_blocks: Iterable[object]) -> list[Audio]:
    audios: list[Audio] = []
    for block in content_blocks:
        if not isinstance(block, AudioContent):
            continue
        audios.append(Audio(content=_mcp_block_data_as_bytes(block.data), mime_type=block.mime_type))
    return audios


def _raise_for_mcp_call_error(server_id: str, result: CallToolResult) -> None:
    """Raise a structured tool error when the server reports failure."""
    if not result.is_error:
        return
    lines = _text_lines_from_blocks(result.content)
    structured = _compact_structured_content(result)
    if structured:
        lines.append(f"structuredContent={structured}")
    message = "\n".join(line for line in lines if line) or "MCP tool call failed"
    raise MCPToolCallError(server_id, message)


def tool_value_from_call_result(server_id: str, result: CallToolResult) -> ToolValue:
    """Convert one MCP call result into a tool value."""
    _raise_for_mcp_call_error(server_id, result)
    lines = _text_lines_from_blocks(result.content)
    structured = _compact_structured_content(result)
    if structured and (not lines or structured not in lines):
        lines.append(structured)
    content = "\n\n".join(line for line in lines if line).strip() or "MCP tool completed successfully."
    images = _image_artifacts_from_blocks(result.content)
    audios = _audio_artifacts_from_blocks(result.content)
    return ToolValue(content=content, images=tuple(images), audios=tuple(audios))
