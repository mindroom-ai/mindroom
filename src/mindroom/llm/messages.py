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


# ``Message.provider_data`` key marking an assistant message whose usage sums a retried request's attempts, so usage
# readers do not take the sum for one request's size; ``usage_sums_attempts`` reads it.
AGGREGATE_USAGE_KEY = "mindroom_aggregate_usage"
# ``Message.provider_data`` key naming the model that answered an assistant message, ``{"id": ..., "provider": ...}``,
# which ``model_provenance`` builds for every writer and ``read_model_provenance`` reads for usage attribution
# (``usage/storage.py``, ``agent_loop/helper.py``). It also tells a run's own model messages from the replies its
# input carries (``is_own_model_message``).
MODEL_PROVENANCE_KEY = "mindroom_model"


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """The model that answered a message: its id and its route's provider name, which usage rows and prices key by."""

    model_id: str
    provider: str


def model_provenance(model_id: str, provider: str) -> dict[str, Any]:
    """Return the provider data recording that ``model_id`` answered a message, under ``MODEL_PROVENANCE_KEY``.

    ``provider`` is the route's provider name (``ModelIdentity.provider``), which usage rows and prices key by.
    """
    return {MODEL_PROVENANCE_KEY: {"id": model_id, "provider": provider}}


def read_model_provenance(provider_data: Mapping[str, object] | None) -> ModelProvenance | None:
    """Return the model ``provider_data`` records as having answered its message, as ``model_provenance`` wrote it.

    None when it records no model, or records one in another shape, as a stored row changed by another writer can.
    """
    stamp = (provider_data or {}).get(MODEL_PROVENANCE_KEY)
    if not isinstance(stamp, dict):
        return None
    recorded = cast("dict[str, object]", stamp)
    model_id, provider = recorded.get("id"), recorded.get("provider")
    if isinstance(model_id, str) and isinstance(provider, str):
        return ModelProvenance(model_id=model_id, provider=provider)
    return None


def usage_sums_attempts(provider_data: Mapping[str, object] | None) -> bool:
    """Return whether ``provider_data`` marks its message's usage as the sum of a retried request's attempts.

    Such usage counts toward the totals but describes no one request: not its size, its price, or its time.
    """
    return (provider_data or {}).get(AGGREGATE_USAGE_KEY) is True


def is_own_model_message(message: Message) -> bool:
    """Return whether a model answered ``message`` in the run that holds it.

    A run's input can carry the thread's earlier replies as assistant messages; only a message a model answered
    carries its provenance (``MODEL_PROVENANCE_KEY``), and a history copy is marked ``from_history``.
    """
    return (
        message.role == "assistant"
        and not message.from_history
        and MODEL_PROVENANCE_KEY in (message.provider_data or {})
    )


@dataclass(slots=True, kw_only=True)
class Message:
    """One message in a model conversation.

    A ``system`` message is a prompt, which history never replays and only a paused run stores. ``from_history``
    marks a copy replayed from a stored run (never stored again), and ``add_to_agent_memory=False`` marks turn context
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
    metrics: MessageMetrics = field(default_factory=MessageMetrics)
    created_at: int = field(default_factory=_now)
    from_history: bool = False
    add_to_agent_memory: bool = True

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
        ``from_history`` and ``stop_after_tool_call`` only when True, and ``add_to_agent_memory`` only when False, which
        a run's turn context holds.
        """
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
            "provider_data": self.provider_data,
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
        if not self.add_to_agent_memory:
            result["add_to_agent_memory"] = False
        result["created_at"] = self.created_at
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Message:
        """Rebuild from a stored dict.

        Keys this type does not model are ignored. ``add_to_agent_memory`` is True unless the dict says otherwise, as
        ``to_dict`` writes it and ``model_dump``-shaped payloads carry it.
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
            provider_data=data.get("provider_data"),
            metrics=MessageMetrics.from_dict(metrics) if isinstance(metrics, dict) else MessageMetrics(),
            created_at=created_at if isinstance(created_at, int) else _now(),
            from_history=data.get("from_history", False),
            add_to_agent_memory=data.get("add_to_agent_memory", True),
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


__all__ = [
    "AGGREGATE_USAGE_KEY",
    "MODEL_PROVENANCE_KEY",
    "Message",
    "ModelProvenance",
    "ToolResultView",
    "is_own_model_message",
    "media_items",
    "model_provenance",
    "read_model_provenance",
    "stable_serialize",
    "system_text",
    "system_texts",
    "tool_result_view",
    "usage_sums_attempts",
    "with_joined_system",
]
