# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The conversation message exchanged with models and stored in runs.

Field names are Agno 3's, so stored ``run_data`` messages and summary-row segments decode without
migration. The record is mutable: history replay, compaction, and notices rewrite fields in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.usage import MessageMetrics

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def _new_id() -> str:
    return str(uuid4())


def _now() -> int:
    return int(time())


def _media_list[T](decode: Callable[[Mapping[str, Any]], T], items: list[Mapping[str, Any]] | None) -> list[T] | None:
    if not items:
        return None
    return [decode(item) for item in items]


# ``Message.provider_data`` key marking an assistant message whose usage sums a retried request's attempts, so usage
# readers do not take the sum for one request's size (``model_usage.py``, ``usage/storage.py``).
AGGREGATE_USAGE_KEY = "mindroom_aggregate_usage"


@dataclass(slots=True, kw_only=True)
class Message:
    """One message in a model conversation.

    ``from_history`` marks a copy replayed from a stored run (never stored again), and
    ``add_to_agent_memory=False`` marks a message sent to the model but never stored.
    """

    role: str
    content: str | list[Any] | None = None
    id: str = field(default_factory=_new_id)
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
                return first.get("text", "")
            return json.dumps(self.content, ensure_ascii=False)
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict: None and empty values dropped, media as dicts, ``created_at`` always."""
        values: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "from_history": self.from_history,
            "compressed_content": self.compressed_content,
            "stop_after_tool_call": self.stop_after_tool_call,
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
        result["created_at"] = self.created_at
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Message:
        """Rebuild from a stored dict.

        Keys this type does not model are ignored.
        The non-persisted flags are read when present so ``model_dump``-shaped payloads decode too.
        """
        # LEGACY_COMPAT: Agno 3 message dicts carry keys the owned Message dropped.
        # Legacy format: ``run_data`` messages, summary-row segments, and tool-job payloads written by the
        # Agno-backed runtime may hold ``audio_output``, ``image_output``, ``video_output``, ``file_output``,
        # ``citations``, ``references``, ``checkpoint_status``, ``checkpoint_created_at``, ``temporary``, and media
        # ``media_reference``/``external`` entries.
        # Last legacy release: v2026.10.474; replacement: unreleased.
        # Handling: the keys are ignored on read and never written again; every key MindRoom reads survives.
        # Coverage: tests/llm/test_codecs.py::test_message_from_dict_ignores_agno_only_keys,
        #   tests/llm/test_codecs.py::test_message_from_dict_reads_non_persisted_flags_from_model_dump_payloads
        metrics = data.get("metrics")
        identifier = data.get("id")
        created_at = data.get("created_at")
        return cls(
            role=data["role"],
            content=data.get("content"),
            id=identifier if isinstance(identifier, str) else _new_id(),
            compressed_content=data.get("compressed_content"),
            name=data.get("name"),
            tool_call_id=data.get("tool_call_id"),
            tool_calls=data.get("tool_calls"),
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


__all__ = [
    "AGGREGATE_USAGE_KEY",
    "Message",
]
