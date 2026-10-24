# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Gemini wire format: request contents, function declarations, schema conversion, and stream parsing.

Request values are plain dicts in google-genai's field names.
The SDK validates them into its own types before it serializes the request.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel

from mindroom.llm.errors import MALFORMED_TOOL_CALL, InvalidRequest, ProviderError
from mindroom.llm.media import IMAGE_HEADER_BYTES, image_mime_type
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, UsageReported, hosted_tool_started
from mindroom.llm.usage import MessageMetrics
from mindroom.logging_config import get_logger
from mindroom.providers.remote_media import download_media
from mindroom.providers.stored_tool_calls import call_arguments

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from google.genai.types import (
        GenerateContentConfig,
        GenerateContentResponse,
        GenerateContentResponseUsageMetadata,
        Part,
    )

    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import StopReason, StreamEvent, ToolChoice, ToolSpec

logger = get_logger(__name__)

_ROLE_MAP = {"assistant": "model", "tool": "user"}
# Gemini rejects requests above 20 MB, so larger local media goes through the Files API, which Vertex AI lacks.
_INLINE_MEDIA_LIMIT_BYTES = 20 * 1024 * 1024
_FORMAT_TO_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "heic": "image/heic",
    "heif": "image/heif",
    "mp3": "audio/mp3",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "mp4": "video/mp4",
    "mov": "video/mov",
    "avi": "video/avi",
    "webm": "video/webm",
    "pdf": "application/pdf",
}
# The finish reason of a candidate whose tool call the model malformed.
_MALFORMED_FUNCTION_CALL = "MALFORMED_FUNCTION_CALL"
_FILTERED_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    },
)


def _without_none(**values: Any) -> dict[str, Any]:  # noqa: ANN401
    return {key: value for key, value in values.items() if value is not None}


# ---------------------------------------------------------------------------
# Request contents
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediaRoute:
    """Where one request's media can go.

    ``upload(path, mime_type)`` sends a local file through the Files API and returns its URI; it is None on
    Vertex AI, which has no Files API. ``provider`` names the adapter in the errors for media that cannot be sent.
    """

    provider: str
    upload: Callable[[Path, str], Awaitable[str]] | None = None


async def format_contents(
    messages: Sequence[Message],
    route: MediaRoute,
) -> tuple[list[dict[str, Any]], str | list[Any] | None]:
    """Return the request contents and the system instruction for ``messages``.

    Local media is read off the event loop and remote media downloaded; media that cannot be sent raises
    ``InvalidRequest``, so the media fallback replaces it with its note instead of the model never seeing it.
    """
    contents: list[dict[str, Any]] = []
    system_instruction: str | list[Any] | None = None
    for message in messages:
        if message.role == "system":
            system_instruction = message.content
            continue
        role = _ROLE_MAP.get(message.role, message.role)
        parts = _message_parts(message, role)
        if role == "user" and message.tool_calls is None and not _is_tool_result(message):
            parts = await _with_media(message, parts, route)
        # Gemini rejects consecutive contents with the same role.
        if parts and contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        elif parts:
            contents.append({"role": role, "parts": parts})
    return contents, system_instruction


def _is_tool_result(message: Message) -> bool:
    return message.role == "tool" and message.tool_call_id is not None and message.tool_name is not None


def _message_parts(message: Message, role: str) -> list[dict[str, Any]]:
    """Return the text, function call, and function response parts of one message."""
    content = message.content
    if role == "model" and message.tool_calls:
        parts = [] if content is None else [_text_part(str(content), message.provider_data)]
        parts.extend(_function_call_part(call) for call in message.tool_calls)
        return parts
    if _is_tool_result(message):
        function_response: dict[str, Any] = {"name": message.tool_name, "response": {"result": content}}
        if call_id := _provider_id(message.tool_call_id):
            function_response["id"] = call_id
        return [{"function_response": function_response}]
    return [_text_part(content, message.provider_data)] if isinstance(content, str) else []


async def _with_media(message: Message, parts: list[dict[str, Any]], route: MediaRoute) -> list[dict[str, Any]]:
    images = [part for image in message.images or () if (part := await _image_part(image, route)) is not None]
    videos = [part for video in message.videos or () if (part := await _video_part(video, route)) is not None]
    audio = [part for clip in message.audio or () if (part := await _audio_part(clip, route)) is not None]
    files = [part for file in message.files or () if (part := await _file_part(file, route)) is not None]
    # Google recommends videos before the prompt; each video is placed first, so the last one leads.
    return [*reversed(videos), *parts, *images, *audio, *files]


def _provider_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _text_part(text: str, provider_data: Mapping[str, Any] | None) -> dict[str, Any]:
    part: dict[str, Any] = {"text": text}
    if provider_data and "thought_signature" in provider_data:
        part["thought_signature"] = base64.b64decode(provider_data["thought_signature"])
    return part


def _function_call_part(call: Mapping[str, Any]) -> dict[str, Any]:
    function = call["function"]
    function_call = _without_none(name=function["name"], args=call_arguments(function), id=_provider_id(call.get("id")))
    part: dict[str, Any] = {"function_call": function_call}
    if "thought_signature" in call:
        part["thought_signature"] = base64.b64decode(call["thought_signature"])
    return part


def _inline(data: bytes, mime_type: str) -> dict[str, Any]:
    return {"inline_data": {"data": data, "mime_type": mime_type}}


def _file_uri(uri: str, mime_type: str) -> dict[str, Any]:
    return {"file_data": {"file_uri": uri, "mime_type": mime_type}}


def _read_local_media(path: Path, kind: str, provider: str, inline_limit: int) -> tuple[int, bytes]:
    """Return the size of a local media file and its bytes: all of them up to ``inline_limit``, else its header."""
    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            return size, stream.read() if size <= inline_limit else stream.read(IMAGE_HEADER_BYTES)
    except OSError as error:
        logger.warning("gemini_media_file_unreadable", kind=kind, path=str(path), error=str(error))
        msg = f"The {kind} attachment could not be read"
        raise InvalidRequest(msg, provider=provider) from error


async def _local_part(
    path: str,
    kind: str,
    route: MediaRoute,
    mime_type: Callable[[bytes], str],
    *,
    always_upload: bool = False,
) -> dict[str, Any] | None:
    """Inline a local file up to the inline limit, or reference its Files API upload where the route has one.

    ``mime_type`` gets the file's bytes, or only its header when the file is uploaded.
    """
    file_path = Path(path)
    # TODO(simplify): local audio and video always go through the Files API where there is one, even when small
    # enough to inline. Inline them up to the limit like other media and drop ``always_upload``. Operators would
    # see fewer Files API uploads; models get the same media.
    inline_limit = 0 if always_upload and route.upload is not None else _INLINE_MEDIA_LIMIT_BYTES
    size, data = await asyncio.to_thread(_read_local_media, file_path, kind, route.provider, inline_limit)
    if not size:
        return None
    if size <= inline_limit:
        return _inline(data, mime_type(data))
    if route.upload is None:
        msg = f"The {kind} attachment is too large to send inline to Vertex AI"
        raise InvalidRequest(msg, provider=route.provider)
    file_mime_type = mime_type(data)
    return _file_uri(await route.upload(file_path, file_mime_type), file_mime_type)


async def _downloaded(url: str, kind: str, route: MediaRoute) -> tuple[bytes, str | None]:
    return await download_media(url, kind=kind, limit_bytes=_INLINE_MEDIA_LIMIT_BYTES, provider=route.provider)


def _media_mime_type(media: Audio | Video, default: str) -> str:
    if media.mime_type:
        return media.mime_type
    if media.format:
        media_format = media.format.lower()
        return _FORMAT_TO_MIME.get(media_format) or f"{default.split('/', maxsplit=1)[0]}/{media_format}"
    if media.filepath:
        suffix = Path(media.filepath).suffix.lower().lstrip(".")
        if suffix in _FORMAT_TO_MIME:
            return _FORMAT_TO_MIME[suffix]
    return default


async def _image_part(image: Image, route: MediaRoute) -> dict[str, Any] | None:
    if image.url is not None:
        data, _ = await _downloaded(image.url, "image", route)
        return _inline(data, image_mime_type(image, data))
    if image.filepath is not None:
        return await _local_part(image.filepath, "image", route, partial(image_mime_type, image))
    return _inline(image.content, image_mime_type(image, image.content)) if image.content else None


async def _audio_part(audio: Audio, route: MediaRoute) -> dict[str, Any] | None:
    mime_type = _media_mime_type(audio, "audio/mp3")
    if audio.url is not None:
        data, _ = await _downloaded(audio.url, "audio", route)
        return _inline(data, mime_type)
    if audio.filepath is not None:
        return await _local_part(audio.filepath, "audio", route, lambda _: mime_type, always_upload=True)
    return _inline(audio.content, mime_type) if audio.content else None


async def _video_part(video: Video, route: MediaRoute) -> dict[str, Any] | None:
    mime_type = _media_mime_type(video, "video/mp4")
    if video.url is not None:
        return _file_uri(video.url, mime_type)
    if video.filepath is not None:
        return await _local_part(video.filepath, "video", route, lambda _: mime_type, always_upload=True)
    return _inline(video.content, mime_type) if video.content else None


def _file_mime_type(file: File, name: str, declared: str | None = None) -> str:
    """Return the file's own MIME type, else the one its download declared, else its name's, else PDF."""
    return file.mime_type or declared or mimetypes.guess_type(name)[0] or "application/pdf"


async def _file_part(file: File, route: MediaRoute) -> dict[str, Any] | None:
    if file.content:
        data = file.content.encode("utf-8") if isinstance(file.content, str) else file.content
        return _inline(data, _file_mime_type(file, file.filename or ""))
    if (url := file.url) is not None:
        # Gemini fetches a Cloud Storage file, or an HTTPS file of known type, itself; other files are downloaded.
        if url.startswith("gs://"):
            return _file_uri(url, _file_mime_type(file, url))
        if url.startswith("https://") and file.mime_type:
            return _file_uri(url, file.mime_type)
        data, declared = await _downloaded(url, "file", route)
        return _inline(data, _file_mime_type(file, url, declared))
    if file.filepath is None:
        return None
    mime_type = _file_mime_type(file, file.filepath)
    return await _local_part(file.filepath, "file", route, lambda _: mime_type)


# ---------------------------------------------------------------------------
# Tools, schemas, and generation settings
# ---------------------------------------------------------------------------


def function_declarations(tools: Sequence[ToolSpec]) -> dict[str, Any] | None:
    """Return the Gemini tool that declares ``tools``, sorted by name for stable request prefixes."""
    # A tool without a description sends an empty one, kept as main behaves.
    declarations = [
        _without_none(name=tool.name, description=tool.description or "", parameters=convert_schema(tool.parameters))
        for tool in sorted(tools, key=lambda tool: tool.name)
    ]
    return {"function_declarations": declarations} if declarations else None


def without_tool_selection(config: Mapping[str, Any], *, vertexai: bool) -> GenerateContentConfig:
    """Return the config of a decision request: function schemas kept, native tools gone, JSON output requested.

    ``config`` is the request dict this adapter builds. JSON output is requested except on Vertex AI when the
    request keeps function declarations.
    """
    from google.genai.types import (  # noqa: PLC0415 - provider SDK loads on first request
        FunctionCallingConfig,
        FunctionCallingConfigMode,
        GenerateContentConfig,
        Tool,
        ToolConfig,
    )

    generation_config = GenerateContentConfig.model_validate(config)
    if generation_config.cached_content:
        # Cached content may contain native tools that this request cannot inspect.
        msg = "Participation decisions cannot inspect tools in Gemini cached content"
        raise ValueError(msg)
    if generation_config.http_options is not None and generation_config.http_options.extra_body:
        msg = "Participation decisions cannot safely apply Gemini body overrides"
        raise ValueError(msg)
    # Function-calling NONE does not disable grounding or other native tools.
    declaration_tools: list[Tool] = [
        Tool(function_declarations=tool.function_declarations)
        for tool in generation_config.tools or []
        if isinstance(tool, Tool) and tool.function_declarations
    ]
    # Without declarations, function-calling settings govern nothing and are dropped.
    function_calling = FunctionCallingConfig(mode=FunctionCallingConfigMode.NONE)
    tool_config = ToolConfig(function_calling_config=function_calling) if declaration_tools else None
    # Gemini can emit function calls under NONE and even without declarations; JSON output cannot.
    # Google documents JSON output beside function calling only for Gemini 3 models. Live Gemini API
    # runs in September 2026 saw it accepted under NONE by the Gemini 3 and 2.5 models tried.
    # Accepted risk: models that reject JSON output with declarations, or JSON output at all (reportedly
    # older Gemini models and Gemma), fail every check closed, the same outcome as a leaked function call.
    # Vertex AI acceptance beside declarations is unverified, so Vertex gets JSON only without them.
    json_output = not (vertexai and declaration_tools)
    # The reply's authored output schema never shapes a decision.
    return generation_config.model_copy(
        update={
            "tools": declaration_tools or None,
            "tool_config": tool_config,
            "response_mime_type": "application/json" if json_output else None,
            "response_schema": None,
            "response_json_schema": None,
        },
    )


def tool_config(tool_choice: ToolChoice | None) -> dict[str, Any] | None:
    """Return the function-calling config for ``tool_choice``; ``"none"`` turns function calling off."""
    return None if tool_choice is None else {"function_calling_config": {"mode": "NONE"}}


def response_schema(response_format: object) -> type[BaseModel] | dict[str, Any] | None:
    """Return the response schema for a pydantic output model, converted where Gemini needs it."""
    if not (isinstance(response_format, type) and issubclass(response_format, BaseModel)):
        return None
    schema = response_format.model_json_schema()
    if not _needs_conversion(schema):
        return response_format
    return convert_schema(schema) or response_format


def _needs_conversion(schema: Mapping[str, Any]) -> bool:
    """Return whether a schema has dict fields or self references, which Gemini cannot take as a class."""
    if _has_additional_properties(schema):
        return True
    definitions: Mapping[str, Any] = schema.get("$defs", {})
    return any(
        isinstance(definition, dict) and _has_self_reference(definition, f"#/$defs/{name}")
        for name, definition in definitions.items()
    )


def _has_additional_properties(schema: Mapping[str, Any]) -> bool:
    if "additionalProperties" in schema:
        return True
    properties: Mapping[str, Any] = schema.get("properties", {})
    children = [*properties.values(), schema.get("items")]
    return any(isinstance(child, dict) and _has_additional_properties(child) for child in children)


def _has_self_reference(schema: Mapping[str, Any], target: str) -> bool:
    if schema.get("$ref") == target:
        return True
    properties: Mapping[str, Any] = schema.get("properties", {})
    alternatives = [sub for key in ("anyOf", "oneOf", "allOf") for sub in schema.get(key, ())]
    children = [*properties.values(), schema.get("items"), *alternatives]
    return any(isinstance(child, dict) and _has_self_reference(child, target) for child in children)


def convert_schema(  # noqa: C901, PLR0911, PLR0912
    schema: Mapping[str, Any],
    root: Mapping[str, Any] | None = None,
    visited: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Convert a JSON schema to Gemini's schema dialect, resolving ``$ref`` and breaking cycles.

    The input is never modified.
    """
    # TODO(simplify): tool and output schemas are rewritten into Gemini's OpenAPI subset, which drops some
    # descriptions and invents an ``example_key`` property for dicts. Send them as authored, in the SDK's
    # recommended ``parameters_json_schema`` and ``response_json_schema``, and delete this. Models would notice.
    root = schema if root is None else root
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in visited:
            return {"type": "OBJECT", "description": f"Circular reference to {ref}"}
        definitions = root.get("$defs", {})
        name = ref.split("/")[-1]
        if ref.startswith("#/$defs/") and name in definitions:
            return convert_schema(definitions[name], root, visited | {ref})
        return None

    schema_type = schema.get("type", "")
    if schema_type is None or schema_type == "null":
        return None
    description = schema.get("description")
    title = schema.get("title")
    default = schema.get("default")

    if "enum" in schema:
        return _without_none(type="STRING", enum=schema["enum"], description=description, default=default, title=title)

    if schema_type == "object":
        if "properties" in schema:
            properties = {key: _property_schema(value, root, visited) for key, value in schema["properties"].items()}
            if properties:
                return _without_none(
                    type="OBJECT",
                    properties=properties,
                    required=schema.get("required", []),
                    description=description,
                    default=default,
                    title=title,
                )
        elif isinstance(additional := schema.get("additionalProperties"), dict) and "type" in additional:
            return _dictionary_schema(additional, description=description, default=default)
        return _without_none(type="OBJECT", description=description, default=default, title=title)

    if schema_type == "array" and "items" in schema:
        items = (convert_schema(schema["items"], root, visited) if schema["items"] else None) or {"type": "STRING"}
        return _without_none(
            type="ARRAY",
            description=description,
            items=items,
            min_items=schema.get("minItems"),
            max_items=schema.get("maxItems"),
            title=title,
        )

    if schema_type == "string":
        return _without_none(
            type="STRING",
            description=description,
            default=default,
            title=title,
            format=schema.get("format"),
        )

    if schema_type in ("integer", "number"):
        return _without_none(
            type=schema_type.upper(),
            description=description,
            default=default,
            title=title,
            maximum=schema.get("maximum"),
            minimum=schema.get("minimum"),
        )

    if schema_type == "" and "anyOf" in schema:
        converted = [convert_schema(sub, root, visited) for sub in schema["anyOf"]]
        alternatives = [sub for sub in converted if sub is not None]
        if len(alternatives) == 1:
            # TODO(simplify): a nullable ``anyOf`` keeps only its alternative's fields, so a parameter such as
            # ``background_after`` loses its description. Keep the outer description. Models would notice (#3075).
            alternatives[0]["nullable"] = any(sub is None for sub in converted)
            return alternatives[0]
        return _without_none(any_of=alternatives, description=description, default=default, title=title)

    if isinstance(schema_type, list):
        schema_type = next((name for name in schema_type if name != "null"), "")
    if schema_type:
        return _without_none(type=schema_type.upper(), description=description, default=default, title=title)
    return None


def _property_schema(value: Mapping[str, Any], root: Mapping[str, Any], visited: frozenset[str]) -> dict[str, Any]:
    value_type = value.get("type", "")
    # A ["<type>", "null"] list type is sent as its first entry, kept as main behaves.
    nullable = isinstance(value_type, list) and "null" in value_type
    converted = convert_schema({**value, "type": value_type[0]} if nullable else value, root, visited)
    if converted is None:
        return _without_none(title=value.get("title"), description=value.get("description"))
    if nullable:
        converted["nullable"] = True
    return converted


def _dictionary_schema(additional: Mapping[str, Any], *, description: str | None, default: object) -> dict[str, Any]:
    """Represent a ``dict[str, T]`` as an object with one example property, since Gemini needs properties."""
    type_value = additional.get("type", "string")
    if isinstance(type_value, list):
        value_type = type_value[0].upper() if type_value else "STRING"
        suffix = f" (supports union types: {', '.join(type_value)})"
    else:
        value_type = type_value.upper()
        suffix = ""
    example: dict[str, Any] = {
        "type": value_type,
        "description": (
            f"Example key-value pair. This object can contain any number of keys with {value_type.lower()} values"
            f"{suffix}."
        ),
    }
    if value_type == "ARRAY":
        example["items"] = {}
    return _without_none(
        type="OBJECT",
        properties={"example_key": example},
        description=description
        or f"Dictionary with {value_type.lower()} values{suffix}. Can contain any number of key-value pairs.",
        default=default,
    )


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GeminiReply:
    """What one streamed Gemini reply produced so far."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thought_signature: str | None = None
    usage: MessageMetrics | None = None
    finish_reason: str | None = None
    prompt_blocked: bool = False

    @property
    def finished(self) -> bool:
        """Whether Gemini ended the reply: a candidate carried its finish reason, or the prompt was blocked."""
        return self.finish_reason is not None or self.prompt_blocked


def read_chunk(
    reply: GeminiReply,
    chunk: GenerateContentResponse,
    *,
    whole_response: bool = False,
) -> list[StreamEvent]:
    """Add one streamed chunk (or one ``whole_response`` from a non-streamed call) to ``reply`` and return its events.

    Gemini repeats the reply's cumulative usage on every chunk, so each chunk that carries usage reports it first, as
    a partial report that replaces the one before. Only a finished candidate's usage, the reply's total, counts toward
    the reply's own usage, apart from a whole response without candidates (see the TODO below). A candidate that ended
    in a malformed tool call reports its usage and nothing else; ``malformed_tool_call_error`` then ends the reply. A
    chunk that shows Google Search grounding or code execution at work reports that hosted tool's start.
    """
    if not chunk.candidates:
        if chunk.prompt_feedback is not None and chunk.prompt_feedback.block_reason is not None:
            # A blocked prompt gets feedback instead of a candidate, which ends the reply.
            reply.prompt_blocked = True
        # TODO(simplify): a chunk without candidates counts its usage only when the reply arrives whole, blocked or
        # not, so a streamed blocked prompt reports none. Also count a streamed chunk that blocks the prompt.
        # Operators would see the usage of streamed blocked prompts.
        if not whole_response or chunk.usage_metadata is None:
            return []
        reply.usage = _usage_metrics(chunk.usage_metadata)
        return [UsageReported(reply.usage, final=False)]
    candidate = chunk.candidates[0]
    events: list[StreamEvent] = []
    finish_reason = None if candidate.finish_reason is None else candidate.finish_reason.value
    if finish_reason is not None:
        reply.finish_reason = finish_reason
    if chunk.usage_metadata is not None:
        usage = _usage_metrics(chunk.usage_metadata)
        so_far = usage if reply.usage is None else reply.usage + usage
        if finish_reason is not None:
            reply.usage = so_far
        events.append(UsageReported(so_far, final=False))
    if finish_reason != _MALFORMED_FUNCTION_CALL:
        parts = candidate.content.parts if candidate.content is not None else None
        if candidate.grounding_metadata is not None:
            events.append(hosted_tool_started("google_search"))
        if any(part.executable_code is not None or part.code_execution_result is not None for part in parts or ()):
            events.append(hosted_tool_started("code_execution"))
        events.extend(event for part in parts or () for event in _read_part(reply, part))
    return events


def malformed_tool_call_error(reply: GeminiReply, *, provider: str) -> ProviderError | None:
    """Return the error of a reply that ended in a tool call the model malformed, or None for any other reply."""
    if reply.finish_reason != _MALFORMED_FUNCTION_CALL:
        return None
    msg = f"Generation ended with finish reason: {_MALFORMED_FUNCTION_CALL}"
    return ProviderError(msg, provider=provider, code=MALFORMED_TOOL_CALL)


def _read_part(reply: GeminiReply, part: Part) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    signature = base64.b64encode(part.thought_signature).decode("ascii") if part.thought_signature else None
    if part.text is not None:
        if part.thought:
            reply.reasoning += part.text
            if part.text:
                events.append(ReasoningDelta(part.text))
        elif part.text:
            reply.content += part.text
            events.append(TextDelta(part.text))
        if signature is not None:
            reply.thought_signature = signature
    if part.function_call is not None:
        call: dict[str, Any] = {
            "id": part.function_call.id or str(uuid4()),
            "type": "function",
            "function": {
                "name": part.function_call.name,
                "arguments": "" if part.function_call.args is None else json.dumps(part.function_call.args),
            },
        }
        if signature is not None:
            call["thought_signature"] = signature
        reply.tool_calls.append(call)
        events.append(ToolCallReady(call))
    return events


def _usage_metrics(usage: GenerateContentResponseUsageMetadata) -> MessageMetrics:
    """Return the usage of one reply; thinking counts as output, as Google bills it."""
    input_tokens = usage.prompt_token_count or 0
    reasoning_tokens = usage.thoughts_token_count or 0
    output_tokens = (usage.candidates_token_count or 0) + reasoning_tokens
    return MessageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=usage.cached_content_token_count or 0,
        provider_metrics=None if usage.traffic_type is None else {"traffic_type": usage.traffic_type.value},
    )


def stop_reason(reply: GeminiReply) -> StopReason | None:
    """Return the normalized reason the reply stopped, or None when the stream never finished a candidate."""
    if reply.finish_reason is None:
        return None
    if reply.finish_reason == "MAX_TOKENS":
        return "output_limit"
    if reply.finish_reason in _FILTERED_FINISH_REASONS:
        return "content_filter"
    if reply.finish_reason != "STOP":
        return "incomplete"
    return "tool_use" if reply.tool_calls else "end"


__all__ = [
    "GeminiReply",
    "MediaRoute",
    "convert_schema",
    "format_contents",
    "function_declarations",
    "malformed_tool_call_error",
    "read_chunk",
    "response_schema",
    "stop_reason",
    "tool_config",
    "without_tool_selection",
]
