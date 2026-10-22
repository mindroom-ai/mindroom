"""Claude Messages API wire format: owned messages and tools in, request blocks out, responses parsed back.

The formatter echoes a stored assistant turn's ``content_blocks`` verbatim, because the API signs thinking
blocks and verifies the sequence on replay; everything else is rebuilt from the message fields. The parser
is the single place a finished response becomes an assistant ``Message``, whether it arrived streamed or not.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from anthropic.types import RedactedThinkingBlock, TextBlock, ThinkingBlock, ToolUseBlock, Usage
from anthropic.types.beta import (
    BetaMessage,
    BetaRedactedThinkingBlock,
    BetaTextBlock,
    BetaThinkingBlock,
    BetaToolUseBlock,
    BetaUsage,
)

from mindroom.llm.messages import Message
from mindroom.llm.usage import MessageMetrics
from mindroom.logging_config import get_logger
from mindroom.providers.claude_wire_blocks import as_dict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from anthropic.types import Message as AnthropicMessage
    from anthropic.types import MessageDeltaUsage

    from mindroom.llm.media import File, Image
    from mindroom.llm.provider import StopReason, ToolSpec

logger = get_logger(__name__)

# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
# Models that support assistant message prefill. This is a closed set: prefill was deprecated starting with
# Claude 4.6 and every later model rejects it, so a request to those models must end with a user turn.
_PREFILL_SUPPORTED_PREFIXES = (
    "claude-3-",
    "claude-sonnet-4-0",
    "claude-sonnet-4-1",
    "claude-sonnet-4-2",
    "claude-sonnet-4-5",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2",
    "claude-opus-4-5",
    "claude-haiku-4-0",
    "claude-haiku-4-1",
    "claude-haiku-4-2",
    "claude-haiku-4-5",
)
# Aliases like "claude-sonnet-4" point at a 4-0 release, which supports prefill; exact matches only, because
# a prefix check would also match "claude-sonnet-4-6".
_PREFILL_SUPPORTED_ALIASES = frozenset({"claude-sonnet-4", "claude-opus-4", "claude-haiku-4"})
# Models without native structured outputs, another closed set: every later Claude model supports them.
_NON_STRUCTURED_OUTPUT_PREFIXES = ("claude-3-",)
_NON_STRUCTURED_OUTPUT_ALIASES = frozenset(
    {
        "claude-sonnet-4-20250514",
        "claude-sonnet-4",
        "claude-sonnet-4-0",
        "claude-opus-4-20250514",
        "claude-opus-4",
        "claude-opus-4-0",
    },
)

TRAILING_USER_MESSAGE = "continue"
_ROLE_MAP = {"system": "system", "developer": "system", "user": "user", "assistant": "assistant", "tool": "user"}
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_IMAGE_MEDIA_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
# Documents Claude reads as plain text; every other type is a base64 document, which the API accepts for PDF only.
_TEXT_DOCUMENT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/html",
        "text/css",
        "text/markdown",
        "text/xml",
        "text/rtf",
        "text/javascript",
        "text/x-python",
        "application/json",
        "application/x-javascript",
        "application/x-python",
    },
)
# Claude stop reasons of a response the output limit cut off before it was complete.
OUTPUT_LIMIT_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "output_limit",
    "model_context_window_exceeded": "output_limit",
    "refusal": "refusal",
    "pause_turn": "incomplete",
}


def _core_model_id(model_id: str) -> str:
    """Strip the provider-specific decorations of a Claude model id.

    Handles ``anthropic/claude-sonnet-4-6`` (LiteLLM), ``us.anthropic.claude-sonnet-4-6-v1:0`` (Bedrock),
    and ``claude-sonnet-4@20250514`` (Vertex AI).
    """
    core_id = model_id.rsplit("/", 1)[-1]
    core_id = core_id.split("anthropic.")[-1] if "anthropic." in core_id else core_id
    core_id = core_id.split("@", 1)[0]
    if ":0" in core_id:
        core_id = core_id.split("-v")[0] if "-v" in core_id else core_id.split(":")[0]
    return core_id


def supports_prefill(model_id: str) -> bool:
    """Return whether the model accepts a request that ends with an assistant turn."""
    core_id = _core_model_id(model_id)
    if not core_id.startswith("claude"):
        return True
    return core_id in _PREFILL_SUPPORTED_ALIASES or core_id.startswith(_PREFILL_SUPPORTED_PREFIXES)


def supports_structured_outputs(model_id: str) -> bool:
    """Return whether the direct API offers ``output_config.format`` for this model."""
    return model_id not in _NON_STRUCTURED_OUTPUT_ALIASES and not model_id.startswith(_NON_STRUCTURED_OUTPUT_PREFIXES)


def stop_reason_of(raw: str | None) -> StopReason | None:
    """Map Claude's ``stop_reason`` onto the shared vocabulary; a reason this map does not know is incomplete."""
    return None if raw is None else _STOP_REASONS.get(raw, "incomplete")


@dataclass(frozen=True, slots=True)
class FormattedConversation:
    """The ``messages`` array and the joined system text of one request."""

    messages: list[dict[str, Any]]
    system: str


def format_claude_messages(
    messages: Sequence[Message],
    *,
    append_trailing_user_message: bool,
) -> FormattedConversation:
    """Turn the conversation into Claude's alternating ``user``/``assistant`` turns.

    System and developer messages are extracted and joined with a space. Consecutive same-role turns merge
    into one, dropping the earlier assistant turn's thinking: the API verifies an assistant message's thinking
    sequence against one original response. A conversation that ends with an assistant turn gets a trailing
    ``continue`` user turn when the model rejects prefill.
    """
    turns: list[dict[str, Any]] = []
    system_texts: list[str] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            system_texts.append(message.get_content_string())
            continue
        if message.role == "user":
            content = _user_blocks(message)
        elif message.role == "assistant":
            content = _assistant_blocks(message)
            if not content:
                continue
        elif message.role == "tool":
            content = [_tool_result_block(message)]
        else:
            msg = f"Claude requests cannot carry a message with role {message.role!r}"
            raise ValueError(msg)
        turns.append({"role": _ROLE_MAP[message.role], "content": content})
    merged = _merged_turns(turns)
    if append_trailing_user_message and merged and merged[-1]["role"] == "assistant":
        merged.append({"role": "user", "content": [{"type": "text", "text": TRAILING_USER_MESSAGE}]})
    return FormattedConversation(messages=merged, system=" ".join(system_texts))


def _block_type(block: Any) -> str | None:  # noqa: ANN401
    """Return the type of a request block, whether it is a dict or one of the SDK's block models."""
    return block.get("type") if isinstance(block, dict) else block.type


def _merged_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role turns; two assistant turns share one message only without the earlier thinking."""
    merged: list[dict[str, Any]] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            previous = merged[-1]["content"]
            if turn["role"] == "assistant":
                previous = [block for block in previous if _block_type(block) not in _THINKING_BLOCK_TYPES]
            merged[-1] = {"role": turn["role"], "content": [*previous, *turn["content"]]}
        else:
            merged.append(turn)
    return merged


def _user_blocks(message: Message) -> list[Any]:
    content = message.content or ""
    blocks: list[Any] = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    for image in message.images or ():
        block = _image_block(image)
        if block is not None:
            blocks.append(block)
    for file in message.files or ():
        block = _document_block(file)
        if block is not None:
            blocks.append(block)
    # Audio and video are not Claude inputs; the media fallback replaces them with a note before the request.
    return blocks


def _image_media_type(image: Image, data: bytes | None) -> str | None:
    if image.mime_type in _IMAGE_MEDIA_TYPES.values():
        return image.mime_type
    if image.format and (media_type := _IMAGE_MEDIA_TYPES.get(image.format.lower())) is not None:
        return media_type
    if image.filepath and (media_type := _IMAGE_MEDIA_TYPES.get(Path(image.filepath).suffix[1:].lower())):
        return media_type
    if data is not None:
        for signature, media_type in _IMAGE_SIGNATURES:
            if data.startswith(signature):
                return media_type
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
    return None


def _url_document_stays_remote(file: File) -> bool:
    """Return whether Claude reads this URL document itself: a PDF, or a document of unknown type."""
    return file.url is not None and file.mime_type in {None, "application/pdf"}


async def _fetched(url: str, http_client: httpx.AsyncClient) -> tuple[bytes, str | None] | None:
    """Return the bytes and media type behind ``url``, or None with a warning when it cannot be read."""
    try:
        response = await http_client.get(url)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        logger.warning("claude_url_media_dropped", url=url, error=str(error))
        return None
    media_type = response.headers.get("content-type", "").partition(";")[0].strip()
    return response.content, media_type or None


async def _inlined_images(images: Sequence[Image] | None, http_client: httpx.AsyncClient) -> list[Image]:
    """Return the images with every URL-backed one fetched into ``content``, minus those that failed."""
    inlined: list[Image] = []
    for image in images or ():
        if image.url is None:
            inlined.append(image)
            continue
        fetched = await _fetched(image.url, http_client)
        if fetched is None:
            continue
        data, media_type = fetched
        url_format = Path(urlparse(image.url).path).suffix[1:].lower() or None
        inlined.append(
            replace(
                image,
                url=None,
                content=data,
                mime_type=image.mime_type or media_type,
                format=image.format or url_format,
            ),
        )
    return inlined


async def _inlined_files(files: Sequence[File] | None, http_client: httpx.AsyncClient) -> list[File]:
    """Return the documents with every URL Claude would not read itself fetched into ``content``."""
    inlined: list[File] = []
    for file in files or ():
        if file.url is None or _url_document_stays_remote(file):
            inlined.append(file)
            continue
        fetched = await _fetched(file.url, http_client)
        if fetched is not None:
            inlined.append(replace(file, url=None, content=fetched[0]))
    return inlined


async def inline_url_media(messages: Sequence[Message], http_client: httpx.AsyncClient) -> Sequence[Message]:
    """Fetch URL-backed images and URL documents Claude would not read itself, so they are sent inline.

    A URL that cannot be read drops its media with a warning instead of failing the request, because the
    message replays on every later request and an expired or private URL would fail them all. The input is
    returned unchanged when no message carries such media.
    """
    inlined: list[Message] | None = None
    for index, message in enumerate(messages):
        if message.role != "user" or not _has_remote_media(message):
            continue
        if inlined is None:
            inlined = list(messages)
        inlined[index] = replace(
            message,
            images=await _inlined_images(message.images, http_client),
            files=await _inlined_files(message.files, http_client),
        )
    return messages if inlined is None else inlined


def _has_remote_media(message: Message) -> bool:
    return any(image.url is not None for image in message.images or ()) or any(
        file.url is not None and not _url_document_stays_remote(file) for file in message.files or ()
    )


def _image_block(image: Image) -> dict[str, Any] | None:
    data = image.content
    if data is None and image.filepath is not None:
        path = Path(image.filepath)
        if not path.is_file():
            logger.warning("claude_image_file_missing", filepath=image.filepath)
            return None
        data = path.read_bytes()
    media_type = _image_media_type(image, data)
    if data is None or media_type is None:
        logger.warning("claude_image_type_unknown", image_id=image.id)
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("utf-8")},
    }


def _document_block(file: File) -> dict[str, Any] | None:
    if file.url is not None:
        # Only a document ``inline_url_media`` left remote reaches here.
        source: dict[str, Any] = {"type": "url", "url": file.url}
    else:
        if file.filepath is not None:
            path = Path(file.filepath)
            if not path.is_file():
                logger.warning("claude_document_file_missing", filepath=file.filepath)
                return None
            data = path.read_bytes()
            media_type = file.mime_type or mimetypes.guess_type(file.filepath)[0] or "application/pdf"
        elif file.content is not None:
            data = file.content.encode("utf-8") if isinstance(file.content, str) else file.content
            media_type = file.mime_type or "application/pdf"
        else:
            return None
        if media_type in _TEXT_DOCUMENT_MEDIA_TYPES:
            # The text document source accepts only text/plain; other text types are sent as plain text.
            source = {"type": "text", "media_type": "text/plain", "data": data.decode("utf-8", errors="replace")}
        else:
            source = {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("utf-8")}
    return {"type": "document", "source": source, "citations": {"enabled": True}}


def serialize_content_blocks(blocks: Sequence[Any]) -> list[dict[str, Any]]:
    """Serialize response blocks or stored block dicts in order; an unknown spelling of redacted thinking is fixed."""
    serialized: list[dict[str, Any]] = []
    for block in blocks:
        block_dict = as_dict(block) if isinstance(block, dict) else block.model_dump(exclude_none=True)
        if block_dict is None or not block_dict.get("type"):
            continue
        if block_dict.get("type") == "redacted_reasoning_content":
            block_dict = {**block_dict, "type": "redacted_thinking"}
        serialized.append(block_dict)
    return serialized


def _stored_assistant_blocks(message: Message) -> list[dict[str, Any]] | None:
    """Return the response blocks stored on an assistant turn, or None when it predates block storage.

    A ``tool_use`` block is kept only while the message still carries the matching tool call: a response
    cut off by ``max_tokens`` can hold a tool use that never ran, and the API rejects one without a result.
    """
    stored = (message.provider_data or {}).get("content_blocks")
    if not isinstance(stored, list) or not stored:
        return None
    tool_call_ids = {call.get("id") for call in message.tool_calls or [] if isinstance(call, dict)}
    return [
        dict(block)
        for block in serialize_content_blocks(stored)
        if block.get("type") != "tool_use" or block.get("id") in tool_call_ids
    ]


def _assistant_blocks(message: Message) -> list[Any]:
    """Return an assistant turn's blocks: the stored response blocks, or a rebuild from the message fields.

    Rebuilt thinking, text, and tool-use blocks are the SDK's block models rather than dicts. The cache ladder
    places breakpoints on dict blocks only, so a turn rebuilt from another provider's fields never takes a
    breakpoint while an echoed stored turn can, which keeps breakpoint placement where the goldens pin it.
    """
    stored = _stored_assistant_blocks(message)
    if stored is not None:
        return stored
    provider_data = message.provider_data or {}
    blocks: list[Any] = []
    signature = provider_data.get("signature")
    if message.reasoning_content is not None and isinstance(signature, str) and signature:
        blocks.append(ThinkingBlock(thinking=message.reasoning_content, signature=signature, type="thinking"))
    # Reasoning without a signature (another provider's, or a partially stored response) cannot be replayed.
    if message.redacted_reasoning_content is not None:
        blocks.append(RedactedThinkingBlock(data=message.redacted_reasoning_content, type="redacted_thinking"))
    # LEGACY_COMPAT: Claude turns stored with server-tool blocks but without the response's content blocks.
    # Legacy format: an assistant message whose provider_data carries "server_tool_blocks" (each response block
    # other than text, thinking, and tool_use, as the SDK's model_dump) and no "content_blocks"; selected
    # whenever such a turn is rebuilt from its fields.
    # Last legacy release: v2026.9.43 stored them through Agno 3.0.5; v2026.9.44 (Agno 3.0.9) also stores every
    # response block in order under "content_blocks", which replays verbatim instead.
    # Handling: the stored server-tool blocks replay after the thinking and before the text and tool uses, as
    # Agno's formatter rebuilt them; a stored entry that is not a dict is skipped.
    # Coverage: tests/providers/test_anthropic_family_replay.py::test_a_turn_stored_without_content_blocks_replays_its_server_tool_blocks
    server_blocks = provider_data.get("server_tool_blocks")
    if isinstance(server_blocks, list):
        blocks.extend(block for block in server_blocks if isinstance(block, dict))
    if isinstance(message.content, str) and message.content.strip():
        blocks.append(TextBlock(text=message.content, type="text"))
    for call in message.tool_calls or []:
        function = call["function"]
        blocks.append(
            ToolUseBlock(
                id=call["id"],
                input=json.loads(function["arguments"]) if "arguments" in function else {},
                name=function["name"],
                type="tool_use",
            ),
        )
    return blocks


def _tool_result_block(message: Message) -> dict[str, Any]:
    content = message.content
    payload: str | list[Any]
    if isinstance(content, list):
        payload = [
            block if (block := as_dict(item)) is not None and block.get("type") else {"type": "text", "text": str(item)}
            for item in content
        ]
    else:
        payload = "" if content is None else str(content)
    return {"type": "tool_result", "tool_use_id": message.tool_call_id, "content": payload}


def build_system_blocks(system: str, *, cache_system_prompt: bool, extended_cache_time: bool) -> list[dict[str, Any]]:
    """Return the ``system`` array: one text block, marked for caching when the model caches its prompt."""
    block: dict[str, Any] = {"text": system, "type": "text"}
    if cache_system_prompt:
        cache_control: dict[str, str] = {"type": "ephemeral"}
        if extended_cache_time:
            cache_control["ttl"] = "1h"
        block["cache_control"] = cache_control
    return [block]


def _with_additional_properties_false(schema: Any) -> Any:  # noqa: ANN401
    """Return the schema with ``additionalProperties: false`` on every object schema that lists properties.

    Dict-shaped schemas use ``additionalProperties`` as their value schema and are left alone.
    """
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    if "properties" in result:
        result["additionalProperties"] = False
    for key, value in result.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: _with_additional_properties_false(item) for name, item in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = _with_additional_properties_false(value)
        elif key in {"anyOf", "allOf", "oneOf"} and isinstance(value, list):
            result[key] = [_with_additional_properties_false(item) for item in value]
    return result


def format_claude_tools(tools: Sequence[ToolSpec], *, strict_allowed: bool) -> list[dict[str, Any]]:
    """Return the tool definitions in Claude's shape, sorted by name so the cached prefix stays byte-stable.

    A tool without a description gets an empty one, ``strict`` is sent only when it is True and the route accepts
    it, and the approval flags never reach the wire.
    """
    formatted: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda item: item.name):
        parameters = dict(tool.parameters)
        properties = parameters.get("properties") or {}
        input_properties: dict[str, Any] = {}
        for name, schema in properties.items():
            property_schema = _with_additional_properties_false(schema)
            if isinstance(property_schema, dict) and "description" not in property_schema:
                property_schema["description"] = ""
            input_properties[name] = property_schema
        input_schema: dict[str, Any] = {
            "type": parameters.get("type", "object"),
            "properties": input_properties,
            "required": parameters.get("required", []),
            "additionalProperties": False,
        }
        # Raw JSON schemas, such as MCP input schemas, may share nested types through $defs or definitions.
        for key in ("$defs", "definitions"):
            if key in parameters:
                input_schema[key] = parameters[key]
        definition: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": input_schema,
        }
        if tool.strict and strict_allowed:
            definition["strict"] = True
        formatted.append(definition)
    return formatted


def parse_claude_usage(usage: Usage | MessageDeltaUsage | BetaUsage) -> MessageMetrics:
    """Translate Claude usage into message metrics.

    ``input_tokens`` excludes cache reads and writes, so ``total_tokens`` is input plus output. A compacted
    response bills every iteration; the counters sum them while ``provider_metrics.context_usage`` keeps the
    final iteration's active context, which history budgeting reads.
    """
    metrics = MessageMetrics(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )
    provider_metrics: dict[str, Any] = {}
    if usage.server_tool_use is not None:
        provider_metrics["server_tool_use"] = usage.server_tool_use.model_dump()
    if isinstance(usage, (Usage, BetaUsage)) and usage.service_tier:
        provider_metrics["service_tier"] = usage.service_tier
    context_usage = {
        "input_tokens": metrics.input_tokens,
        "cache_read_tokens": metrics.cache_read_tokens,
        "cache_write_tokens": metrics.cache_write_tokens,
    }
    if isinstance(usage, BetaUsage) and usage.iterations:
        iterations = usage.iterations
        metrics.input_tokens = sum(iteration.input_tokens for iteration in iterations)
        metrics.output_tokens = sum(iteration.output_tokens for iteration in iterations)
        metrics.cache_read_tokens = sum(iteration.cache_read_input_tokens for iteration in iterations)
        metrics.cache_write_tokens = sum(iteration.cache_creation_input_tokens for iteration in iterations)
        last = iterations[-1]
        context_usage = {
            "input_tokens": last.input_tokens,
            "cache_read_tokens": last.cache_read_input_tokens,
            "cache_write_tokens": last.cache_creation_input_tokens,
        }
    metrics.total_tokens = metrics.input_tokens + metrics.output_tokens
    provider_metrics["context_usage"] = context_usage
    metrics.provider_metrics = provider_metrics
    return metrics


def tool_call_from_block(block: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return a ``tool_use`` block as the stored tool-call dict; a cut-off block keeps the arguments that parsed."""
    return {"id": block.id, "type": "function", "function": {"name": block.name, "arguments": json.dumps(block.input)}}


def assistant_message_from_response(
    response: AnthropicMessage | BetaMessage,
    *,
    record_context_management: bool,
) -> Message:
    """Build the assistant message of one finished response.

    ``provider_data`` keeps ``content_blocks`` (the response blocks in order, as the API produced them),
    ``server_tool_blocks`` (every other block, including types the SDK does not know), the thinking ``signature``,
    the raw ``stop_reason``, and ``context_management`` when the model was configured with one. Every tool
    use becomes a tool call, including the calls of a response the output limit cut off.
    """
    texts: list[str] = []
    thinking: list[str] = []
    signature: str | None = None
    redacted: str | None = None
    server_tool_blocks: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for block in response.content:
        # The SDK builds a block of a type it does not know as a text block that keeps its own ``type``.
        if block.type == "text" and isinstance(block, (TextBlock, BetaTextBlock)):
            texts.append(block.text)
        elif block.type == "thinking" and isinstance(block, (ThinkingBlock, BetaThinkingBlock)):
            thinking.append(block.thinking)
            signature = block.signature
        elif block.type == "redacted_thinking" and isinstance(
            block,
            (RedactedThinkingBlock, BetaRedactedThinkingBlock),
        ):
            redacted = block.data
        elif block.type == "tool_use" and isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
            tool_calls.append(tool_call_from_block(block))
        else:
            server_tool_blocks.append(block.model_dump())
    provider_data: dict[str, Any] = {}
    if response.content:
        provider_data["content_blocks"] = serialize_content_blocks(response.content)
    if thinking:
        provider_data["signature"] = signature
    if server_tool_blocks:
        provider_data["server_tool_blocks"] = server_tool_blocks
    if record_context_management and isinstance(response, BetaMessage) and response.context_management is not None:
        provider_data["context_management"] = response.context_management.model_dump()
    provider_data["stop_reason"] = response.stop_reason
    return Message(
        role="assistant",
        content="".join(texts) or None,
        reasoning_content="".join(thinking) if thinking else None,
        redacted_reasoning_content=redacted,
        tool_calls=tool_calls or None,
        provider_data=provider_data,
        metrics=parse_claude_usage(response.usage),
    )


__all__ = [
    "OUTPUT_LIMIT_STOP_REASONS",
    "TRAILING_USER_MESSAGE",
    "FormattedConversation",
    "assistant_message_from_response",
    "build_system_blocks",
    "format_claude_messages",
    "format_claude_tools",
    "inline_url_media",
    "parse_claude_usage",
    "serialize_content_blocks",
    "stop_reason_of",
    "supports_prefill",
    "supports_structured_outputs",
    "tool_call_from_block",
]
