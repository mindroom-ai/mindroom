# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The conversation message exchanged with models and stored in runs.

Field names are the keys stored ``run_data`` messages and summary-row segments hold, so they decode without
migration. The record is mutable: history replay, compaction, and notices rewrite fields in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Any, cast
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
# ``Message.provider_data`` key naming the model that answered an assistant message, ``{"id": ..., "provider": ...}``,
# which ``model_provenance`` builds for every writer and ``read_model_provenance`` reads for usage attribution
# (``usage/storage.py``, ``memory/_mem0_usage.py``). It also tells a run's own model messages from the replies its
# input carries (``is_own_model_message``).
MODEL_PROVENANCE_KEY = "mindroom_model"
# The roles of the prompt messages MindRoom writes: history never replays them, and only a paused run stores them.
PROMPT_MESSAGE_ROLES = frozenset({"system", "developer"})


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """The model that answered a message: its id and the metrics provider string usage rows and pricing key by."""

    model_id: str
    provider: str


def model_provenance(model_id: str, provider: str) -> dict[str, Any]:
    """Return the provider data recording that ``model_id`` answered a message, under ``MODEL_PROVENANCE_KEY``.

    ``provider`` is the metrics provider string (``ModelIdentity.provider_name``) that usage rows and pricing key by.
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
        # Last legacy release: v2026.10.478; replacement: unreleased.
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


def split_thinking(content: str) -> tuple[str | None, str]:
    """Split ``<think>`` reasoning from a reply that arrived whole.

    A missing opening tag means the reasoning starts the reply. Without a closing tag, or when the reasoning is
    empty, the reply comes back unchanged with no reasoning, so an empty ``<think></think>`` block stays in the text.
    """
    end = content.find("</think>")
    if end == -1:
        return None, content
    start = content.find("<think>")
    reasoning = (content[:end] if start == -1 else content[start + len("<think>") : end]).strip()
    if not reasoning:
        return None, content
    return reasoning, content[end + len("</think>") :].strip()


__all__ = [
    "AGGREGATE_USAGE_KEY",
    "MODEL_PROVENANCE_KEY",
    "PROMPT_MESSAGE_ROLES",
    "Message",
    "ModelProvenance",
    "is_own_model_message",
    "model_provenance",
    "read_model_provenance",
    "split_thinking",
]
