# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Gemini wire format: request contents, function declarations, and stream parsing.

Request values are plain dicts in google-genai's field names.
The SDK validates them into its own types before it serializes the request.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from google.genai.types import (
    FunctionCallingConfig,
    FunctionCallingConfigMode,
    GenerateContentConfig,
    Tool,
    ToolConfig,
)

from mindroom.llm.errors import MALFORMED_TOOL_CALL, InvalidRequest, ProviderError
from mindroom.llm.media import canonical_mime_type, image_mime_type
from mindroom.llm.messages import system_text, tool_result_view
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, UsageReported, hosted_tool_started
from mindroom.llm.tool_search import call_arguments
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.media_files import read_media_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from google.genai.types import (
        GenerateContentResponse,
        GenerateContentResponseUsageMetadata,
        Part,
    )

    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.llm.messages import Message, ToolResultView
    from mindroom.llm.provider import StopReason, StreamEvent, ToolSpec

_ROLE_MAP = {"assistant": "model", "tool": "user"}
# Gemini rejects requests above 20 MB, so larger local media goes through the Files API, which Vertex AI lacks.
_INLINE_MEDIA_LIMIT_BYTES = 20 * 1024 * 1024
# ``Message.provider_data`` key of a Gemini reply's parts in arrival order (``reply_provider_data``).
_GEMINI_PARTS_KEY = "gemini_parts"
# The part fields of a server-side tool's call and result, such as a Google Search the model ran.
_SERVER_TOOL_FIELDS = frozenset({"tool_call", "tool_response"})
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
# Text types outside ``text/``, which Gemini reads as plain text.
_TEXT_FILE_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/x-javascript",
        "application/x-python",
    },
)
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
    upload: Callable[[Path, str], Awaitable[str]] | None


async def format_contents(
    messages: Sequence[Message],
    route: MediaRoute,
    *,
    server_tool_parts: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return the request contents and the system instruction for ``messages``, their system prompt as one text.

    Local media is read off the event loop; a file or video URL goes for Gemini to fetch, and the pipeline downloaded
    every other URL (``Capabilities.url_media``). Media that cannot be sent raises ``InvalidRequest``, so the media
    fallback replaces it with its note instead of the model never seeing it. ``server_tool_parts`` says whether the
    request asks for the server-side tool parts (``include_server_side_tool_invocations``), the only request that
    takes stored ones back.
    """
    contents: list[dict[str, Any]] = []
    # LEGACY_COMPAT: Tool results stored without their tool's name.
    # Legacy format: a stored ``tool`` message whose ``tool_name`` is null, as rows the Agno-backed runtime or another
    # writer stored can be; current writers record the name with every result.
    # Last legacy release: no single cutoff: v2026.10.498 was the last release whose Agno-backed writer stored tool
    # results, the owned writers from v2026.10.499 always store the name, and foreign rows may still lack it.
    # Handling: such a result takes the name of the call with its id earlier in the request, so it goes to Gemini as a
    # function response instead of user text; one whose call the request does not hold still goes as text.
    # Coverage: tests/providers/test_gemini_ollama_synthetic_adapters.py::test_gemini_names_a_tool_result_stored_without_its_name_after_its_call
    call_names = {
        call.get("id"): (call.get("function") or {}).get("name")
        for message in messages
        for call in message.tool_calls or ()
    }
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool" and message.tool_name is None and message.tool_call_id in call_names:
            message = replace(message, tool_name=call_names[message.tool_call_id])  # noqa: PLW2901
        role = _ROLE_MAP.get(message.role, message.role)
        parts = _message_parts(message, role, server_tool_parts=server_tool_parts)
        if role == "user" and message.tool_calls is None and not _is_tool_result(message):
            parts = await _with_media(message, parts, route)
        # Gemini rejects consecutive contents with the same role.
        if parts and contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        elif parts:
            contents.append({"role": role, "parts": parts})
    for content in contents:
        # Gemini 4 rejects a last user turn that ends with anything but its function responses, as a request ending
        # with a model turn, so the text and media that follow tool results go before them.
        content["parts"].sort(key=lambda part: "function_response" in part)
    return contents, system_text(messages) or None


def _is_tool_result(message: Message) -> bool:
    return message.role == "tool" and message.tool_name is not None


def _message_parts(message: Message, role: str, *, server_tool_parts: bool) -> list[dict[str, Any]]:
    """Return the text, function call, and function response parts of one message.

    A model message that stored its parts (``reply_provider_data``) replays them in their order, each text part with
    its own signature, and its server-side tool parts when ``server_tool_parts`` is set.
    """
    content = message.content
    stored = (message.provider_data or {}).get(_GEMINI_PARTS_KEY) if role == "model" else None
    if isinstance(stored, list):
        return _stored_parts(stored, message.tool_calls or [], server_tool_parts=server_tool_parts)
    if role == "model" and message.tool_calls:
        parts = [] if content is None else [_text_part(str(content), message.provider_data)]
        parts.extend(_function_call_part(call) for call in message.tool_calls)
        return parts
    if _is_tool_result(message):
        return [{"function_response": _function_response(tool_result_view(message))}]
    return [_text_part(content, message.provider_data)] if isinstance(content, str) else []


def _function_response(result: ToolResultView) -> dict[str, Any]:
    """Return one result as a function response; a failed call's content goes under ``error``, as the API documents."""
    response = {"error": result.content} if result.is_error else {"result": result.content}
    return _without_none(name=result.name, response=response, id=result.call_id)


async def _with_media(message: Message, parts: list[dict[str, Any]], route: MediaRoute) -> list[dict[str, Any]]:
    """Return the message's text parts followed by its images, audio, videos, and files, each in the order it came."""
    images = [part for image in message.images or () if (part := await _image_part(image, route)) is not None]
    videos = [part for video in message.videos or () if (part := await _video_part(video, route)) is not None]
    audio = [part for clip in message.audio or () if (part := await _audio_part(clip, route)) is not None]
    files = [part for file in message.files or () if (part := await _file_part(file, route)) is not None]
    return [*parts, *images, *audio, *videos, *files]


def _stored_parts(
    stored: list[Any],
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    server_tool_parts: bool,
) -> list[dict[str, Any]]:
    """Return a stored reply's parts in order; a call the parts do not name follows them.

    Server-side tool parts go back only to a request that asks for them: Gemini requires each one back as given there
    (https://ai.google.dev/gemini-api/docs/generate-content/tool-combination), and Vertex AI rejects them.
    """
    calls = {call.get("id"): call for call in tool_calls}
    parts: list[dict[str, Any]] = []
    for entry in stored:
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            parts.append(_text_part(entry["text"], entry))
        elif isinstance(entry, dict) and (call := calls.pop(entry.get("function_call_id"), None)) is not None:
            parts.append(_function_call_part(call))
        elif isinstance(entry, dict) and server_tool_parts and entry.keys() & _SERVER_TOOL_FIELDS:
            parts.append(_server_tool_part(entry))
    return [*parts, *(_function_call_part(call) for call in calls.values())]


# LEGACY_COMPAT: Gemini replies stored with one message-level thought signature.
# Legacy format: a stored Gemini assistant message whose ``provider_data`` holds ``thought_signature`` and no
# ``gemini_parts``: the reply's text parts joined into ``content`` and the last signed text part's signature.
# Last legacy release: v2026.10.600 wrote this shape; the release after it stores ``gemini_parts``.
# Handling: such a message replays as one text part carrying that signature, then its calls, as it did.
# Coverage: tests/providers/test_gemini_ollama_synthetic_adapters.py::test_gemini_replays_a_reply_in_the_order_its_parts_arrived
def _text_part(text: str, provider_data: Mapping[str, Any] | None) -> dict[str, Any]:
    part: dict[str, Any] = {"text": text}
    if provider_data and "thought_signature" in provider_data:
        part["thought_signature"] = base64.b64decode(provider_data["thought_signature"])
    return part


def _server_tool_part(entry: Mapping[str, Any]) -> dict[str, Any]:
    part = {key: value for key, value in entry.items() if key in _SERVER_TOOL_FIELDS}
    if "thought_signature" in entry:
        part["thought_signature"] = base64.b64decode(entry["thought_signature"])
    return part


def _function_call_part(call: Mapping[str, Any]) -> dict[str, Any]:
    function = call["function"]
    function_call = _without_none(name=function["name"], args=call_arguments(function), id=call.get("id"))
    part: dict[str, Any] = {"function_call": function_call}
    if "thought_signature" in call:
        part["thought_signature"] = base64.b64decode(call["thought_signature"])
    return part


def _inline(data: bytes, mime_type: str) -> dict[str, Any]:
    return {"inline_data": {"data": data, "mime_type": mime_type}}


def _file_uri(uri: str, mime_type: str) -> dict[str, Any]:
    return {"file_data": {"file_uri": uri, "mime_type": mime_type}}


async def _local_part(
    path: str,
    media_id: str,
    kind: str,
    route: MediaRoute,
    mime_type: Callable[[bytes], str],
) -> dict[str, Any] | None:
    """Inline a local file up to the inline limit, or reference its Files API upload where the route has one.

    ``mime_type`` gets the file's bytes, or only its header when the file is uploaded. An empty file adds no part, and
    a failure names ``media_id``, the id of the file's media item, as the rejected one.
    """
    file_path = Path(path)
    limit = _INLINE_MEDIA_LIMIT_BYTES
    read = partial(read_media_file, file_path, media_id=media_id, kind=kind, provider=route.provider, limit=limit)
    if (local := await asyncio.to_thread(read)) is None:
        return None
    size, data = local
    if size <= _INLINE_MEDIA_LIMIT_BYTES:
        return _inline(data, mime_type(data))
    if route.upload is None:
        msg = f"The {kind} attachment is too large to send inline to Vertex AI"
        raise InvalidRequest(msg, provider=route.provider, rejected_media=frozenset({media_id}))
    file_mime_type = mime_type(data)
    return _file_uri(await route.upload(file_path, file_mime_type), file_mime_type)


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
    if image.filepath is not None:
        return await _local_part(image.filepath, image.id, "image", route, partial(image_mime_type, image))
    return _inline(image.content, image_mime_type(image, image.content)) if image.content else None


async def _audio_part(audio: Audio, route: MediaRoute) -> dict[str, Any] | None:
    mime_type = _media_mime_type(audio, "audio/mp3")
    if audio.filepath is not None:
        return await _local_part(audio.filepath, audio.id, "audio", route, lambda _: mime_type)
    return _inline(audio.content, mime_type) if audio.content else None


async def _video_part(video: Video, route: MediaRoute) -> dict[str, Any] | None:
    mime_type = _media_mime_type(video, "video/mp4")
    if video.url is not None:
        return _file_uri(video.url, mime_type)
    if video.filepath is not None:
        return await _local_part(video.filepath, video.id, "video", route, lambda _: mime_type)
    return _inline(video.content, mime_type) if video.content else None


def _file_mime_type(file: File, name: str) -> str:
    """Return the file's own MIME type, else its name's, else PDF."""
    return file.mime_type or mimetypes.guess_type(name)[0] or "application/pdf"


def _sent_file_type(mime_type: str) -> str | None:
    """Return the type a file of ``mime_type`` is sent as: plain text for any text type, None for a type Gemini rejects.

    Gemini reads PDFs, text, images, audio, and video; it rejects other types, such as ``application/octet-stream``.
    """
    canonical = canonical_mime_type(mime_type) or ""
    if canonical.startswith("text/") or canonical in _TEXT_FILE_MIME_TYPES:
        return "text/plain"
    if canonical == "application/pdf" or canonical.startswith(("image/", "audio/", "video/")):
        return canonical
    return None


def _omitted_file(file: File, mime_type: str) -> dict[str, Any]:
    return {"text": f"[Attachment omitted: {file.filename or 'file'} ({mime_type}) is a file type Gemini cannot read]"}


def _typed_file_part(file: File, mime_type: str, part: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Return ``part`` built for the type the file is sent as, or the note naming a file Gemini cannot read."""
    sent_type = _sent_file_type(mime_type)
    return _omitted_file(file, mime_type) if sent_type is None else part(sent_type)


async def _file_part(file: File, route: MediaRoute) -> dict[str, Any] | None:
    if file.content:
        data = file.content.encode("utf-8") if isinstance(file.content, str) else file.content
        return _typed_file_part(file, _file_mime_type(file, file.filename or ""), partial(_inline, data))
    if (url := file.url) is not None:
        return _typed_file_part(file, _file_mime_type(file, url), partial(_file_uri, url))
    if file.filepath is None:
        return None
    mime_type = _file_mime_type(file, file.filepath)
    if (sent_type := _sent_file_type(mime_type)) is None:
        return _omitted_file(file, mime_type)
    return await _local_part(file.filepath, file.id, "file", route, lambda _: sent_type)


# ---------------------------------------------------------------------------
# Tools, schemas, and generation settings
# ---------------------------------------------------------------------------


def function_declarations(tools: Sequence[ToolSpec]) -> dict[str, Any] | None:
    """Return the Gemini tool that declares ``tools``, sorted by name for stable request prefixes.

    Each schema goes as authored, as JSON Schema (``parameters_json_schema``), which the Gemini API and Vertex AI both
    take; a tool without a description sends none.
    """
    declarations = [
        _without_none(name=tool.name, description=tool.description, parameters_json_schema=dict(tool.parameters))
        for tool in sorted(tools, key=lambda tool: tool.name)
    ]
    return {"function_declarations": declarations} if declarations else None


def without_tool_selection(config: Mapping[str, Any], *, vertexai: bool) -> GenerateContentConfig:
    """Return the config of a decision request: function schemas kept, native tools gone, JSON output requested.

    ``config`` is the request dict this adapter builds. JSON output is requested except on Vertex AI when the
    request keeps function declarations.
    """
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


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GeminiReply:
    """What one streamed Gemini reply produced so far.

    ``parts`` holds the reply's text, function calls, and server-side tool parts in the order they arrived: a text
    part as ``{"text"}`` with the ``thought_signature`` Gemini signed it with, a function call as
    ``{"function_call_id"}``, and a server-side tool's call or result as its ``tool_call`` or ``tool_response`` field
    with its signature. Unsigned streamed text joins the open unsigned text part; a signed part, even one with empty
    text, stays its own part, because Google requires each signature back in the part it came in, never merged with
    another part.
    """

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    finish_reason: str | None = None
    prompt_blocked: bool = False

    @property
    def finished(self) -> bool:
        """Whether Gemini ended the reply: a candidate carried its finish reason, or the prompt was blocked."""
        return self.finish_reason is not None or self.prompt_blocked


def read_chunk(reply: GeminiReply, chunk: GenerateContentResponse) -> list[StreamEvent]:
    """Add one streamed chunk to ``reply`` and return its events.

    Gemini repeats the reply's cumulative usage on every chunk, so each chunk that carries usage reports it first, as
    a partial report that replaces the one before. Only the usage of a finished candidate or of a blocked prompt, the
    reply's total, counts toward the reply's own usage. A candidate that ended in a malformed tool call reports its
    usage and nothing else; ``malformed_tool_call_error`` then ends the reply. A chunk that shows Google Search
    grounding or code execution at work reports that hosted tool's start.
    """
    if not chunk.candidates:
        if chunk.prompt_feedback is None or chunk.prompt_feedback.block_reason is None:
            return []
        # A blocked prompt gets feedback instead of a candidate, which ends the reply.
        reply.prompt_blocked = True
        if chunk.usage_metadata is None:
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
        elif part.text or signature is not None:
            if part.text:
                reply.content += part.text
                events.append(TextDelta(part.text))
            _add_text(reply, part.text, signature)
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
        reply.parts.append({"function_call_id": call["id"]})
        events.append(ToolCallReady(call))
    if server_tool := part.model_dump(mode="json", include=set(_SERVER_TOOL_FIELDS), exclude_none=True):
        reply.parts.append(server_tool if signature is None else {**server_tool, "thought_signature": signature})
    return events


def _add_text(reply: GeminiReply, text: str, signature: str | None) -> None:
    """Join unsigned ``text`` to the open unsigned text part; a signed part, or text after a call, starts a part."""
    last = reply.parts[-1] if reply.parts else None
    if signature is None and last is not None and "text" in last and "thought_signature" not in last:
        last["text"] += text
        return
    reply.parts.append({"text": text} if signature is None else {"text": text, "thought_signature": signature})


def reply_provider_data(reply: GeminiReply) -> dict[str, Any]:
    """Return the replay state a finished reply stores: its parts, when their order, signatures, or kinds matter.

    A reply that is only unsigned text replays the same from its content, so it stores none.
    """
    if any(part.keys() != {"text"} for part in reply.parts):
        return {_GEMINI_PARTS_KEY: reply.parts}
    return {}


def _usage_metrics(usage: GenerateContentResponseUsageMetadata) -> MessageMetrics:
    """Return the usage of one reply; thinking counts as output, as Google bills it."""
    reasoning_tokens = usage.thoughts_token_count or 0
    counts = TokenCounts(
        # The prompt a hosted tool adds, such as fetched URL context or search results, counts as input too.
        input_tokens=(usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0),
        output_tokens=(usage.candidates_token_count or 0) + reasoning_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=usage.cached_content_token_count or 0,
    )
    return MessageMetrics(
        tokens=counts.with_totals(),
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
    "format_contents",
    "function_declarations",
    "malformed_tool_call_error",
    "read_chunk",
    "reply_provider_data",
    "stop_reason",
    "without_tool_selection",
]
