# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The conversation message exchanged with models and stored in runs.

Field names are the keys a stored message row's data holds, and those of the Agno-layout messages only the storage v2
conversion still reads. The record is mutable: history replay, compaction, and notices rewrite fields in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.message_meta import EMPTY_META, UNSET, InvalidStamp, MessageMeta, ModelProvenance, Unset
from mindroom.llm.usage import MessageMetrics
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from mindroom.llm.provider import MediaKind

logger = get_logger(__name__)


def stable_serialize(value: object) -> str:
    """Serialize arbitrary values into a stable JSON-ish string."""
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _new_id() -> str:
    return str(uuid4())


def _now() -> int:
    return int(time())


def _media_list[T](decode: Callable[[Mapping[str, Any]], T], items: list[Mapping[str, Any]] | None) -> list[T] | None:
    if not items:
        return None
    return [decode(item) for item in items]


def is_queued_notice_message(message: Message) -> bool:
    """Return whether a message is a queued-message notice MindRoom wrote."""
    marker = message.meta.queued_message_notice
    # LEGACY_COMPAT: Queued-message notices marked "persisted" after delivery.
    # Legacy format: a stored notice whose marker is ``"persisted"`` instead of ``True``; once a reply was delivered,
    # MindRoom rewrote the notices its stored runs held into one marked so, and a reply that opened a scope rewrote a
    # notice a crashed reply had left with ``True``.
    # Last legacy release: v2026.10.760, whose post-delivery rewrite wrote the marker; replacement: the next release,
    # which stores each notice once, as its run held it, with ``True``.
    # Handling: such a notice reads as a notice, as one marked ``True`` does; both replay alike, so no row is rewritten.
    # Coverage: tests/test_agent_turn_runtime.py::test_a_notice_an_earlier_release_stored_replays_as_it_is_stored
    return marker is True or marker == "persisted"


MEDIA_FOLLOW_UP_TEXT = "The tool call above generated the attached media."
PRUNED_MEDIA_FOLLOW_UP_TEXT = "[Earlier tool media omitted to keep context bounded.]"
_MEDIA_FOLLOW_UP_PREFIXES = (MEDIA_FOLLOW_UP_TEXT, PRUNED_MEDIA_FOLLOW_UP_TEXT)


def is_media_follow_up(message: Message) -> bool:
    """Return whether a stored message is a media follow-up the agent loop wrote, which no person said."""
    if message.role != "user" or not isinstance(message.content, str):
        return False
    return any(
        message.content == prefix or message.content.startswith(f"{prefix}\n\n") for prefix in _MEDIA_FOLLOW_UP_PREFIXES
    )


def is_own_model_message(message: Message) -> bool:
    """Return whether a model answered ``message`` in the run that holds it.

    A run's input can carry the thread's earlier replies as assistant messages; only a message a model answered
    carries its provenance (``meta.model``), and a history copy is marked ``from_history``.
    """
    return message.role == "assistant" and not message.from_history and message.meta.model is not None


@dataclass(slots=True, kw_only=True)
class Message:
    """One message in a model conversation.

    A ``system`` message is a prompt, which history never replays and only a paused run stores. ``from_history``
    marks a copy replayed from a stored run (never stored again), and ``is_turn_context=True`` marks turn context
    MindRoom sent to the model rather than conversation, such as memory results and the approval receipt: a run stores
    it where it was sent, unless it stopped after a call, so a resume and later turns send it again unchanged, except
    under a message limit, while text summaries and the readers of what was said leave it out.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[Any] | None = None
    id: str = field(default_factory=_new_id)
    # LEGACY_COMPAT: Messages stored with a compressed copy of their content.
    # Legacy format: a stored message whose ``compressed_content`` holds a shortened copy of a tool result, in
    # ``run_data`` messages, summary-row segments, and tool-job payloads; only Agno's ``compress_tool_results``
    # wrote it.
    # Last legacy release: v2026.10.498, the last release that ran ``compress_tool_results``; v2026.10.499 retired it,
    # and no MindRoom writer has set the field since.
    # Handling: a stored copy reads, and a rewritten row writes it back, so token estimates, summary input, and a
    # replayed viewed-image notice read it in place of the content (``render_message_content``) as before the upgrade;
    # a new message never has one, and no provider is sent it.
    # Coverage: tests/llm/test_codecs.py::test_a_message_agno_stored_with_a_compressed_copy_keeps_it,
    #   tests/test_history_request_messages.py::test_history_viewed_images_fit_the_aggregate_byte_cap
    compressed_content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_name: str | None = None
    tool_args: Any = None
    tool_call_error: bool | None = None
    stop_after_tool_call: bool = False
    images: Sequence[Image] | None = None
    audio: Sequence[Audio] | None = None
    videos: Sequence[Video] | None = None
    files: Sequence[File] | None = None
    reasoning_content: str | None = None
    redacted_reasoning_content: str | None = None
    provider_data: dict[str, Any] | None = None
    meta: MessageMeta = EMPTY_META
    metrics: MessageMetrics = field(default_factory=MessageMetrics)
    created_at: int = field(default_factory=_now)
    from_history: bool = False
    is_turn_context: bool = False

    def get_content_string(self) -> str:
        """Return the text of ``content``: the first text block of a block list, else its JSON."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            if not self.content:
                return ""
            first = self.content[0]
            if isinstance(first, dict) and "text" in first:
                return first["text"]
            return json.dumps(self.content, ensure_ascii=False)
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict: None and empty values dropped, media as dicts, ``created_at`` always.

        A flag is written only when it differs from its default, which ``from_dict`` reads for an absent key:
        ``from_history`` and ``stop_after_tool_call`` only when True, and ``add_to_agent_memory: False`` when
        ``is_turn_context`` is True. Internal ``meta`` entries merge into ``provider_data`` on the wire.
        """
        meta_data = self.meta.to_provider_data()
        if isinstance(self.provider_data, dict):
            provider_data: Any = {**self.provider_data, **meta_data} if meta_data else self.provider_data
        else:
            provider_data = meta_data or self.provider_data
        values: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "from_history": self.from_history or None,
            "compressed_content": self.compressed_content,
            "stop_after_tool_call": self.stop_after_tool_call or None,
            "role": self.role,
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "tool_call_error": self.tool_call_error,
            "tool_calls": self.tool_calls,
            "redacted_reasoning_content": self.redacted_reasoning_content,
            "provider_data": provider_data,
        }
        result = {
            key: value
            for key, value in values.items()
            if value is not None and not (isinstance(value, (list, dict)) and len(value) == 0)
        }
        for key, media in (
            ("images", self.images),
            ("audio", self.audio),
            ("videos", self.videos),
            ("files", self.files),
        ):
            if media:
                result[key] = [item.to_dict() for item in media]
        metrics = self.metrics.to_dict()
        if metrics:
            result["metrics"] = metrics
        if self.is_turn_context:
            result["add_to_agent_memory"] = False
        result["created_at"] = self.created_at
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Message:
        """Rebuild from a stored dict.

        Keys this type does not model are ignored. Stored ``add_to_agent_memory: False`` decodes as
        ``is_turn_context=True``, and the 18 internal MindRoom keys stored inside ``provider_data`` decode into
        ``meta``.
        """
        # LEGACY_COMPAT: Agno 3 message dicts carry keys the owned Message dropped.
        # Legacy format: ``run_data`` messages, summary-row segments, and tool-job payloads written by the
        # Agno-backed runtime may hold ``audio_output``, ``image_output``, ``video_output``, ``file_output``,
        # ``citations``, ``references``, ``checkpoint_status``, ``checkpoint_created_at``, ``temporary``, and media
        # ``media_reference``/``external`` entries.
        # Last legacy release: v2026.10.498; replacement: v2026.10.499.
        # Handling: the keys are ignored on read and never written again; every key MindRoom reads survives.
        # Coverage: tests/llm/test_codecs.py::test_message_from_dict_ignores_agno_only_keys,
        #   tests/llm/test_codecs.py::test_message_from_dict_reads_non_persisted_flags_from_model_dump_payloads
        metrics = data.get("metrics")
        identifier = data.get("id")
        message_id = identifier if isinstance(identifier, str) else _new_id()
        created_at = data.get("created_at")
        tool_call_id, tool_calls = _stored_call_ids(data, message_id)
        raw_pd = data.get("provider_data")
        if isinstance(raw_pd, dict):
            provider_data, meta = MessageMeta.from_provider_data(raw_pd)
        else:
            provider_data, meta = raw_pd, EMPTY_META
        return cls(
            role=data["role"],
            content=data.get("content"),
            id=message_id,
            compressed_content=data.get("compressed_content"),
            name=data.get("name"),
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
            tool_name=data.get("tool_name"),
            tool_args=data.get("tool_args"),
            tool_call_error=data.get("tool_call_error"),
            stop_after_tool_call=data.get("stop_after_tool_call", False),
            images=_media_list(Image.from_dict, data.get("images")),
            audio=_media_list(Audio.from_dict, data.get("audio")),
            videos=_media_list(Video.from_dict, data.get("videos")),
            files=_media_list(File.from_dict, data.get("files")),
            reasoning_content=data.get("reasoning_content"),
            redacted_reasoning_content=data.get("redacted_reasoning_content"),
            provider_data=provider_data,
            meta=meta,
            metrics=MessageMetrics.from_dict(metrics) if isinstance(metrics, dict) else MessageMetrics(),
            created_at=created_at if isinstance(created_at, int) else _now(),
            from_history=data.get("from_history", False),
            is_turn_context=not data.get("add_to_agent_memory", True),
        )


def _is_malformed_call_id(value: object) -> bool:
    return value is not None and not (isinstance(value, str) and value)


def _has_malformed_id(call: object) -> bool:
    return isinstance(call, dict) and _is_malformed_call_id(cast("dict[str, Any]", call).get("id"))


# LEGACY_COMPAT: Stored call ids that are not non-empty text.
# Legacy format: a stored message whose ``tool_call_id``, or the ``id`` of a call in its ``tool_calls``, is empty or not
# a string.
# Last legacy release: for an empty id, v2026.4.35, the last release on Agno 2.4.7, whose Chat stream parser stored the
# first delta's id unchecked; v2026.4.36 moved to Agno 2.5.13, which gives a call with an empty id a uuid4 one, and the
# owned writers from v2026.10.499 store non-empty text. An id that is not text has no known writer: it is unversioned
# external input, since worker code could write the Agno-era run data the storage v2 conversion read.
# Handling: the row decodes with that id as None and one ``stored_tool_call_id_dropped`` debug line naming the message,
# so the load never fails, every wire reads a call id as text or None, and the message's other ids and calls read as
# stored.
# Coverage: tests/llm/test_codecs.py::test_a_message_stored_with_a_malformed_call_id_loads_without_it,
#   tests/llm/test_codecs.py::test_a_message_whose_result_and_call_ids_are_both_malformed_logs_one_line,
#   tests/providers/test_gemini_ollama_synthetic_adapters.py::test_gemini_tool_history_keeps_valid_call_ids_and_drops_invalid_ones,
#   tests/session_storage/test_legacy_agno3_conversion.py::test_converted_call_ids_are_text_or_none_so_folded_calls_read
def _stored_call_ids(data: Mapping[str, Any], message_id: str) -> tuple[Any, Any]:
    """Return a stored message's ``tool_call_id`` and ``tool_calls``, each malformed call id dropped to None."""
    tool_call_id = data.get("tool_call_id")
    tool_calls = data.get("tool_calls")
    dropped = False
    if _is_malformed_call_id(tool_call_id):
        tool_call_id, dropped = None, True
    if isinstance(tool_calls, list) and any(_has_malformed_id(call) for call in tool_calls):
        tool_calls, dropped = [{**call, "id": None} if _has_malformed_id(call) else call for call in tool_calls], True
    if dropped:
        # The row stays as stored and is read again on every load, so the line repeats and stays at debug level.
        logger.debug("stored_tool_call_id_dropped", message_id=message_id)
    return tool_call_id, tool_calls


def system_texts(messages: Sequence[Message]) -> tuple[str, ...]:
    """Return the text of each system message in ``messages``, in order.

    A request's system prompt is its system messages: the first is the stable prefix providers may cache, and the rest
    hold the turn's context. A wire that takes one system text joins them with ``system_text``.
    """
    return tuple(text for message in messages if message.role == "system" and (text := message.get_content_string()))


def system_text(messages: Sequence[Message]) -> str:
    """Return the system prompt of ``messages`` as one text: each system message's text, a blank line between them."""
    return "\n\n".join(system_texts(messages))


def media_items(message: Message) -> Iterator[tuple[MediaKind, Image | Audio | Video | File]]:
    """Yield each media item of ``message`` with its kind: its images, audio, videos, then files, each in their order."""
    for image in message.images or ():
        yield "image", image
    for clip in message.audio or ():
        yield "audio", clip
    for video in message.videos or ():
        yield "video", video
    for file in message.files or ():
        yield "file", file


def with_joined_system(messages: Sequence[Message]) -> list[Message]:
    """Return ``messages`` with their system messages joined into one (``system_text``), in the first one's place.

    Chat templates of many local models accept only one system message, so wires that send messages by role send the
    system prompt as one.
    """
    first = next((index for index, message in enumerate(messages) if message.role == "system"), None)
    if first is None or sum(message.role == "system" for message in messages) == 1:
        return list(messages)
    rest = [message for message in messages[first:] if message.role != "system"]
    return [*messages[:first], Message(role="system", content=system_text(messages)), *rest]


_NO_ERROR_TEXT = "The tool call failed without an error message."


@dataclass(frozen=True, slots=True)
class ToolResultView:
    """What a wire encodes of a tool message: the call it answers, the tool's name, its content, and whether it failed.

    A result stored without content answers with empty content, so no wire leaves its call unanswered, and a failed
    result without error text says that it failed, since Claude rejects a failed result with empty content.
    """

    call_id: str | None
    name: str | None
    content: str | list[Any]
    is_error: bool


# LEGACY_COMPAT: Tool messages stored without content.
# Legacy format: a stored ``tool`` message whose ``content`` is null. The Agno runtime's result writer
# (``create_function_call_result``) stored a call's output, or a failed call's error text, as content that could be
# missing.
# Last legacy release: v2026.10.498 ran that writer; v2026.10.499 replaced it with owned writers that store string
# content.
# Handling: the view gives such a result empty content, so every wire still answers its call when the row replays.
# Coverage: tests/providers/test_provider_families.py::test_every_family_encodes_a_tool_result_with_its_call_name_content_and_failure
# LEGACY_COMPAT: Failed tool results stored without error text.
# Legacy format: a stored ``tool`` message with ``tool_call_error`` true and null or empty ``content``. The Agno
# runtime stored a failed call's error text as content that could be missing, and the owned executor stored
# ``str(error)``, which is empty for exceptions such as ``TimeoutError`` or ``httpx.ReadTimeout``.
# Last legacy release: v2026.10.512; replacement: v2026.10.513, whose executor stores the exception's type name when
# its text is empty.
# Handling: the view gives such a result ``_NO_ERROR_TEXT``, so Claude, which rejects a failed result with empty
# content, can still answer a thread that replays it, and every other wire says the call failed.
# Coverage: tests/providers/test_provider_families.py::test_every_family_encodes_a_tool_result_with_its_call_name_content_and_failure
def tool_result_view(message: Message) -> ToolResultView:
    """Return the tool result a ``tool`` message carries, as every wire encodes it."""
    is_error = message.tool_call_error is True
    content = "" if message.content is None else message.content
    return ToolResultView(
        call_id=message.tool_call_id,
        name=message.tool_name,
        content=_NO_ERROR_TEXT if is_error and not content else content,
        is_error=is_error,
    )


def stored_tool_call(call_id: str, name: str | None, arguments: str) -> dict[str, Any]:
    """Return a plain stored call, leaving its serialized arguments unchanged."""
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


__all__ = [
    "EMPTY_META",
    "MEDIA_FOLLOW_UP_TEXT",
    "PRUNED_MEDIA_FOLLOW_UP_TEXT",
    "UNSET",
    "InvalidStamp",
    "Message",
    "MessageMeta",
    "ModelProvenance",
    "ToolResultView",
    "Unset",
    "is_media_follow_up",
    "is_own_model_message",
    "is_queued_notice_message",
    "media_items",
    "stable_serialize",
    "stored_tool_call",
    "system_text",
    "system_texts",
    "tool_result_view",
    "with_joined_system",
]
