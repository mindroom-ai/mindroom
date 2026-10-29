"""Claude Messages API wire format: messages and tools in, request blocks out, responses parsed back.

The parser is the single place a finished response becomes an assistant ``Message``: it stores the response's
blocks once, in the shape a later request sends them (``claude_blocks``), and reads the message's text, reasoning,
and tool calls from those blocks. The formatter echoes a stored turn's blocks verbatim, because the API signs
thinking blocks and verifies the sequence on replay; a turn without them is rebuilt from its message fields.
"""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from anthropic.types.beta import BetaMessage, BetaUsage

from mindroom.llm.media import image_format
from mindroom.llm.messages import Message, ToolResultView, system_texts, tool_result_view
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.logging_config import get_logger
from mindroom.model_defaults import CLAUDE_NO_FORCED_TOOL_MODEL_PREFIXES, CLAUDE_PREFIX_CHECKED_THINKING_MODELS
from mindroom.providers.anthropic.repairs import replay_safe_blocks
from mindroom.providers.anthropic.stored_turns import replayed_blocks
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY, as_dict
from mindroom.providers.media_files import file_mime_type, is_text_mime_type, read_media_bytes, unreadable_file_note

if TYPE_CHECKING:
    from collections.abc import Sequence

    from anthropic.types import Message as AnthropicMessage
    from anthropic.types import ToolUseBlock, Usage
    from anthropic.types.beta import BetaToolUseBlock

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

_TRAILING_USER_MESSAGE = "continue"
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_IMAGE_MEDIA_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
# Claude reads a document as a base64 PDF or as plain text; "Binary formats such as .xlsx or .docx are not supported in
# document blocks" (https://platform.claude.com/docs/en/build-with-claude/pdf-support).
_PDF = "application/pdf"
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
    """Strip the decorations that hide which Claude model an id names from the prefix and alias checks.

    A gateway id such as ``anthropic/claude-opus-4-6`` (LiteLLM) loses its provider path, a Bedrock id such as
    ``us.anthropic.claude-opus-4-6-v1:0`` its region and ``anthropic.`` prefix, and a Vertex AI id such as
    ``claude-sonnet-4@20250514`` its snapshot. A Bedrock version suffix never changes a check, because every
    prefix ends before it and no Bedrock id is a bare alias.
    """
    return model_id.rsplit("/", 1)[-1].rsplit("anthropic.", 1)[-1].split("@", 1)[0]


def supports_prefill(model_id: str) -> bool:
    """Return whether the model accepts a request that ends with an assistant turn."""
    core_id = _core_model_id(model_id)
    if not core_id.startswith("claude"):
        return True
    return core_id in _PREFILL_SUPPORTED_ALIASES or core_id.startswith(_PREFILL_SUPPORTED_PREFIXES)


def supports_structured_outputs(model_id: str) -> bool:
    """Return whether the direct API offers ``output_config.format`` for this model, read by its core id as prefill is."""
    core_id = _core_model_id(model_id)
    return core_id not in _NON_STRUCTURED_OUTPUT_ALIASES and not core_id.startswith(_NON_STRUCTURED_OUTPUT_PREFIXES)


def supports_forced_tool_use(model_id: str, *, manual_thinking: bool) -> bool:
    """Return whether the model accepts a request that forces one tool call, read by its core id as prefill is.

    Manual extended thinking (``thinking.type: "enabled"``) rejects forced tool use on every model.
    """
    return not manual_thinking and not _core_model_id(model_id).startswith(CLAUDE_NO_FORCED_TOOL_MODEL_PREFIXES)


def checks_thinking_prefix(model_id: str) -> bool:
    """Return whether the API rejects or drops this model's replayed thinking once the context before it changed."""
    return _core_model_id(model_id).startswith(CLAUDE_PREFIX_CHECKED_THINKING_MODELS)


def stop_reason_of(raw: str | None) -> StopReason | None:
    """Map Claude's ``stop_reason`` onto the shared vocabulary; a reason this map does not know is incomplete."""
    return None if raw is None else _STOP_REASONS.get(raw, "incomplete")


@dataclass(frozen=True, slots=True)
class FormattedConversation:
    """The ``messages`` array and the system texts of one request, one per system message in order."""

    messages: list[dict[str, Any]]
    system: tuple[str, ...]


def format_claude_messages(
    messages: Sequence[Message],
    *,
    append_trailing_user_message: bool,
    earlier_thinking: bool,
    provider: str,
) -> FormattedConversation:
    """Turn the conversation into Claude's alternating ``user``/``assistant`` turns.

    System messages are extracted, each its own system text. Consecutive same-role turns merge into one, dropping
    the earlier assistant turn's thinking: the API verifies an assistant message's thinking sequence against one
    original response. Without ``earlier_thinking``, the turns before the current tool loop replay without their
    thinking (``_without_earlier_thinking``). A conversation that ends with an assistant turn gets a trailing
    ``continue`` user turn when the model rejects prefill. Documents ask for no citations, which no MindRoom reply
    shows. An attachment file that cannot be read raises ``InvalidRequest`` naming ``provider``.
    """
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "user":
            content = _user_blocks(message, provider)
        elif message.role == "assistant":
            content = replayed_blocks(message)
            if not content:
                continue
        elif message.role == "tool":
            content = [_tool_result_block(tool_result_view(message))]
        else:
            msg = f"Claude requests cannot carry a message with role {message.role!r}"
            raise ValueError(msg)
        turns.append({"role": "assistant" if message.role == "assistant" else "user", "content": content})
    merged = _merged_turns(turns)
    if not earlier_thinking:
        merged = _merged_turns(_without_earlier_thinking(merged))
    if append_trailing_user_message and merged and merged[-1]["role"] == "assistant":
        merged.append({"role": "user", "content": [{"type": "text", "text": _TRAILING_USER_MESSAGE}]})
    return FormattedConversation(messages=merged, system=system_texts(messages))


def _merged_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role turns; two assistant turns share one message only without the earlier thinking."""
    merged: list[dict[str, Any]] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            previous = merged[-1]["content"]
            if turn["role"] == "assistant":
                previous = [block for block in previous if block.get("type") not in _THINKING_BLOCK_TYPES]
            merged[-1] = {"role": turn["role"], "content": [*previous, *turn["content"]]}
        else:
            merged.append(turn)
    return merged


def _without_earlier_thinking(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leave the thinking out of the assistant turns before the current tool loop; a turn left empty goes too.

    The loop starts after the last user turn without a tool result, where the API itself starts a new turn. The
    API requires the loop's own thinking as received and allows leaving out every earlier turn's.
    """
    starts = [index for index, turn in enumerate(turns) if turn["role"] == "user" and not _answers_tool_calls(turn)]
    start = starts[-1] if starts else 0
    kept: list[dict[str, Any]] = []
    for index, turn in enumerate(turns):
        if index >= start or turn["role"] != "assistant":
            kept.append(turn)
            continue
        content = [block for block in turn["content"] if block.get("type") not in _THINKING_BLOCK_TYPES]
        if content:
            kept.append({**turn, "content": content})
    return kept


def _answers_tool_calls(turn: dict[str, Any]) -> bool:
    """Return whether a user turn carries tool results, which continue the tool loop instead of starting a turn."""
    return any(block.get("type") == "tool_result" for block in turn["content"])


def _user_blocks(message: Message, provider: str) -> list[Any]:
    content = message.content or ""
    blocks: list[Any] = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    media = [_image_block(image, provider) for image in message.images or ()]
    media += [_document_block(file, provider) for file in message.files or ()]
    # Audio and video are not Claude inputs; the media fallback replaces them with a note before the request.
    return blocks + [block for block in media if block is not None]


def _image_media_type(image: Image, data: bytes | None) -> str | None:
    """Return the declared media type when Claude names it, else the one the bytes show.

    A file name is not consulted: every image type Claude reads has a signature, and a name can disagree with it.
    """
    if image.mime_type in _IMAGE_MEDIA_TYPES.values():
        return image.mime_type
    if image.format and (media_type := _IMAGE_MEDIA_TYPES.get(image.format.lower())) is not None:
        return media_type
    detected = None if data is None else image_format(data)
    return None if detected is None else _IMAGE_MEDIA_TYPES.get(detected)


def _image_block(image: Image, provider: str) -> dict[str, Any] | None:
    data = image.content
    if image.filepath is not None and data is None:
        data = read_media_bytes(image.filepath, kind="image", provider=provider)
        if data is None:
            # The file is empty, which its reader already logged.
            return None
    media_type = _image_media_type(image, data)
    if data is None or media_type is None:
        logger.warning("claude_image_type_unknown", image_id=image.id)
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("utf-8")},
    }


def _document_block(file: File, provider: str) -> dict[str, Any] | None:
    """Return a PDF or text file as a document, a file of another type as a text block noting it, or None.

    None is for a file without bytes or an empty local file; a file noted is never read.
    """
    if file.filepath is None and file.content is None:
        return None
    media_type = file_mime_type(file)
    text = is_text_mime_type(media_type)
    if not text and media_type != _PDF:
        return {"type": "text", "text": unreadable_file_note(file, media_type)}
    if file.filepath is not None:
        data = read_media_bytes(file.filepath, kind="file", provider=provider)
    else:
        data = file.content.encode("utf-8") if isinstance(file.content, str) else file.content
    if data is None:
        return None
    if text:
        # The text document source accepts only text/plain; every text type is sent as plain text.
        source = {"type": "text", "media_type": "text/plain", "data": data.decode("utf-8", errors="replace")}
    else:
        source = {"type": "base64", "media_type": _PDF, "data": base64.b64encode(data).decode("utf-8")}
    return {"type": "document", "source": source}


def _tool_result_block(result: ToolResultView) -> dict[str, Any]:
    """Return the ``tool_result`` block of one result; a failed call is marked ``is_error``, as the API documents."""
    payload: str | list[Any] = result.content
    # LEGACY_COMPAT: Tool results stored as a list of content blocks.
    # Legacy format: a stored ``tool`` message whose ``content`` is a list, which only the Agno runtime's result
    # writer produced.
    # Last legacy release: v2026.10.498 ran that writer; v2026.10.499 replaced it with owned writers that store string
    # content.
    # Handling: each block is sent as stored, and any other item as a text block.
    # Coverage: tests/providers/test_anthropic_family_replay.py::test_a_tool_result_stored_as_a_list_keeps_its_blocks
    if isinstance(result.content, list):
        payload = [
            block if (block := as_dict(item)) is not None and block.get("type") else {"type": "text", "text": str(item)}
            for item in result.content
        ]
    tool_result: dict[str, Any] = {"type": "tool_result", "tool_use_id": result.call_id, "content": payload}
    if result.is_error:
        tool_result["is_error"] = True
    return tool_result


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


# Claude answers a tool whose ``input_schema`` has one of these at its root with a 400 ("input_schema does not support
# oneOf, allOf, or anyOf at the top level"), as an MCP tool whose input is a union has.
_TOP_LEVEL_UNSUPPORTED_SCHEMA_KEYS = frozenset({"anyOf", "allOf", "oneOf"})


def format_claude_tools(tools: Sequence[ToolSpec], *, strict_allowed: bool) -> list[dict[str, Any]]:
    """Return the tool definitions in Claude's shape, sorted by name so the cached prefix stays byte-stable.

    Each schema goes as authored, except that its root loses ``anyOf``, ``allOf``, and ``oneOf`` and is
    ``type: object`` when it names no type, and a strict tool closes every object with ``additionalProperties: false``,
    as strict tool use requires. A tool without a description sends none, and ``strict`` is sent only when it is True
    and the route accepts it.
    """
    formatted: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda item: item.name):
        strict = bool(tool.strict) and strict_allowed
        parameters = {
            key: deepcopy(value)
            for key, value in tool.parameters.items()
            if key not in _TOP_LEVEL_UNSUPPORTED_SCHEMA_KEYS
        }
        parameters.setdefault("type", "object")
        definition: dict[str, Any] = {
            "name": tool.name,
            **({} if tool.description is None else {"description": tool.description}),
            "input_schema": _with_additional_properties_false(parameters) if strict else parameters,
        }
        if strict:
            definition["strict"] = True
        formatted.append(definition)
    return formatted


def parse_claude_usage(usage: Usage | BetaUsage) -> MessageMetrics:
    """Translate Claude usage into message metrics.

    ``input_tokens`` excludes cache reads and writes, so ``total_tokens`` is input plus output. A compacted
    response bills every iteration; the counters sum them while ``provider_metrics.context_usage`` keeps the
    final iteration's active context, which history budgeting reads.
    """
    counts = TokenCounts(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )
    provider_metrics: dict[str, Any] = {}
    if usage.server_tool_use is not None:
        provider_metrics["server_tool_use"] = usage.server_tool_use.model_dump()
    if usage.service_tier:
        provider_metrics["service_tier"] = usage.service_tier
    context_usage = {
        "input_tokens": counts.input_tokens,
        "cache_read_tokens": counts.cache_read_tokens,
        "cache_write_tokens": counts.cache_write_tokens,
    }
    if isinstance(usage, BetaUsage) and usage.iterations:
        iterations = usage.iterations
        counts = TokenCounts(
            input_tokens=sum(iteration.input_tokens for iteration in iterations),
            output_tokens=sum(iteration.output_tokens for iteration in iterations),
            cache_read_tokens=sum(iteration.cache_read_input_tokens for iteration in iterations),
            cache_write_tokens=sum(iteration.cache_creation_input_tokens for iteration in iterations),
        )
        last = iterations[-1]
        context_usage = {
            "input_tokens": last.input_tokens,
            "cache_read_tokens": last.cache_read_input_tokens,
            "cache_write_tokens": last.cache_creation_input_tokens,
        }
    provider_metrics["context_usage"] = context_usage
    return MessageMetrics(tokens=counts.with_totals(), provider_metrics=provider_metrics)


def tool_call_from_block(block: ToolUseBlock | BetaToolUseBlock) -> dict[str, Any]:
    """Return a streamed ``tool_use`` block as the tool call its finished message stores."""
    return _tool_call(block.model_dump())


def _tool_call(block: dict[str, Any]) -> dict[str, Any]:
    """Return a ``tool_use`` block as the stored tool-call dict; a cut-off block keeps the arguments that parsed."""
    return {
        "id": block["id"],
        "type": "function",
        "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
    }


def assistant_message_from_response(
    response: AnthropicMessage | BetaMessage,
    *,
    record_context_management: bool,
) -> Message:
    """Build the assistant message of one finished response.

    ``provider_data`` keeps the response's blocks under ``claude_blocks``, in order and in request shape
    (``replay_safe_blocks``), the raw ``stop_reason``, and ``context_management`` when the model was configured with
    one. The message's text joins the text blocks, its reasoning the thinking blocks, and its redacted reasoning the
    redacted thinking blocks; every tool use becomes a tool call, including the calls of a response the output limit
    cut off. A block of a type the SDK does not know keeps its own ``type``, so it is stored and replayed but read as
    none of these.
    """
    blocks = replay_safe_blocks([block.model_dump(exclude_none=True) for block in response.content])
    texts = [block["text"] for block in blocks if block["type"] == "text"]
    thinking = [block["thinking"] for block in blocks if block["type"] == "thinking"]
    redacted = [block["data"] for block in blocks if block["type"] == "redacted_thinking"]
    tool_calls = [_tool_call(block) for block in blocks if block["type"] == "tool_use"]
    provider_data: dict[str, Any] = {CLAUDE_BLOCKS_KEY: blocks}
    if record_context_management and isinstance(response, BetaMessage) and response.context_management is not None:
        provider_data["context_management"] = response.context_management.model_dump()
    provider_data["stop_reason"] = response.stop_reason
    return Message(
        role="assistant",
        content="".join(texts) or None,
        reasoning_content="".join(thinking) if thinking else None,
        redacted_reasoning_content="".join(redacted) if redacted else None,
        tool_calls=tool_calls or None,
        provider_data=provider_data,
        metrics=parse_claude_usage(response.usage),
    )


__all__ = [
    "FormattedConversation",
    "assistant_message_from_response",
    "checks_thinking_prefix",
    "format_claude_messages",
    "format_claude_tools",
    "parse_claude_usage",
    "stop_reason_of",
    "supports_forced_tool_use",
    "supports_prefill",
    "supports_structured_outputs",
    "tool_call_from_block",
]
