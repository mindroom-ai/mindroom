"""Internal MindRoom control and provenance metadata stored inside ``Message.provider_data`` on the wire."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping


class Unset(Enum):
    """Sentinel for a tri-state metadata field absent from a stored message (distinct from explicit ``None``)."""

    UNSET = "unset"


UNSET = Unset.UNSET


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """The model that answered a message: its id and its route's provider name, which usage rows and prices key by."""

    model_id: str
    provider: str


@dataclass(frozen=True, slots=True)
class InvalidStamp:
    """A stored metadata stamp whose raw value does not match the expected schema."""

    raw: object


def _pop_true(pd: dict[str, Any], key: str) -> bool:
    if pd.get(key) is True:
        del pd[key]
        return True
    return False


def _pop_str(pd: dict[str, Any], key: str) -> str | None:
    value = pd.get(key)
    if isinstance(value, str):
        del pd[key]
        return value
    return None


def _pop_bool(pd: dict[str, Any], key: str) -> bool | None:
    value = pd.get(key)
    if isinstance(value, bool):
        del pd[key]
        return value
    return None


def _decode_model_stamp(raw: object) -> ModelProvenance | InvalidStamp:
    if isinstance(raw, dict):
        recorded = cast("dict[str, object]", raw)
        model_id, provider = recorded.get("id"), recorded.get("provider")
        if isinstance(model_id, str) and isinstance(provider, str):
            return ModelProvenance(model_id=model_id, provider=provider)
    return InvalidStamp(raw)


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageMeta:
    """Internal MindRoom control and provenance metadata, stored inside ``provider_data`` on the wire."""

    model: ModelProvenance | InvalidStamp | None = None
    aggregate_usage: bool = False
    attempt_rewritten: bool = False
    fallback_model: str | None = None
    cut_off_text: str | None = None
    media_msgtype: str | None = None
    queued_message_notice: bool | Literal["persisted"] = False
    queued_notice_turn_id: str | None = None
    prompt_update: bool = False
    native_compaction: dict[str, Any] | InvalidStamp | None | Unset = UNSET
    encrypted_replay_reset: str | None = None
    compaction_summary: Literal["scope", "run"] | None = None
    compaction_summary_follows: bool = False
    thread_context_note: bool = False
    own_thread_message: bool = False
    request_identity: str | None = None
    cache_routing: str | None = None
    portable_replay: bool | None = None

    @classmethod
    def from_provider_data(cls, raw: Mapping[str, Any]) -> tuple[dict[str, Any] | None, MessageMeta]:
        """Split canonical internal MindRoom keys out of a stored ``provider_data`` dict without mutating ``raw``."""
        pd = dict(raw)
        model = _decode_model_stamp(pd.pop("mindroom_model")) if "mindroom_model" in pd else None
        raw_notice = pd.get("mindroom_queued_message_notice")
        if raw_notice is True or raw_notice == "persisted":
            del pd["mindroom_queued_message_notice"]
            queued_message_notice: bool | Literal["persisted"] = raw_notice
        else:
            queued_message_notice = False
        raw_summary = pd.get("mindroom_compaction_summary")
        if raw_summary in ("scope", "run"):
            del pd["mindroom_compaction_summary"]
            compaction_summary: Literal["scope", "run"] | None = cast("Literal['scope', 'run']", raw_summary)
        else:
            compaction_summary = None
        native: dict[str, Any] | InvalidStamp | None | Unset = UNSET
        if "mindroom_native_compaction" in pd:
            raw_native = pd.pop("mindroom_native_compaction")
            native = raw_native if raw_native is None or isinstance(raw_native, dict) else InvalidStamp(raw_native)
        meta = cls(
            model=model,
            aggregate_usage=_pop_true(pd, "mindroom_aggregate_usage"),
            attempt_rewritten=_pop_true(pd, "mindroom_attempt_rewritten"),
            fallback_model=_pop_str(pd, "mindroom_fallback_model"),
            cut_off_text=_pop_str(pd, "mindroom_cut_off_text"),
            media_msgtype=_pop_str(pd, "mindroom_media_msgtype"),
            queued_message_notice=queued_message_notice,
            queued_notice_turn_id=_pop_str(pd, "mindroom_queued_message_notice_response_turn_id"),
            prompt_update=_pop_true(pd, "mindroom_prompt_update"),
            native_compaction=native,
            encrypted_replay_reset=_pop_str(pd, "mindroom_encrypted_replay_reset"),
            compaction_summary=compaction_summary,
            compaction_summary_follows=_pop_true(pd, "mindroom_compaction_summary_follows"),
            thread_context_note=_pop_true(pd, "mindroom_thread_context_note"),
            own_thread_message=_pop_true(pd, "mindroom_own_thread_message"),
            request_identity=_pop_str(pd, "mindroom_request"),
            cache_routing=_pop_str(pd, "mindroom_cache_routing"),
            portable_replay=_pop_bool(pd, "mindroom_portable_replay"),
        )
        return pd or None, EMPTY_META if meta == EMPTY_META else meta

    def to_provider_data(self) -> dict[str, Any]:
        """Serialize set metadata fields back to their wire ``provider_data`` keys."""
        if self is EMPTY_META:
            return {}
        notice = self.queued_message_notice
        out: dict[str, Any] = {
            key: value
            for key, value in (
                ("mindroom_aggregate_usage", True if self.aggregate_usage else None),
                ("mindroom_attempt_rewritten", True if self.attempt_rewritten else None),
                ("mindroom_fallback_model", self.fallback_model),
                ("mindroom_cut_off_text", self.cut_off_text),
                ("mindroom_media_msgtype", self.media_msgtype),
                ("mindroom_queued_message_notice", notice if notice is True or notice == "persisted" else None),
                ("mindroom_queued_message_notice_response_turn_id", self.queued_notice_turn_id),
                ("mindroom_prompt_update", True if self.prompt_update else None),
                ("mindroom_encrypted_replay_reset", self.encrypted_replay_reset),
                ("mindroom_compaction_summary", self.compaction_summary),
                ("mindroom_compaction_summary_follows", True if self.compaction_summary_follows else None),
                ("mindroom_thread_context_note", True if self.thread_context_note else None),
                ("mindroom_own_thread_message", True if self.own_thread_message else None),
                ("mindroom_request", self.request_identity),
                ("mindroom_cache_routing", self.cache_routing),
                ("mindroom_portable_replay", self.portable_replay),
            )
            if value is not None
        }
        if isinstance(self.model, ModelProvenance):
            out["mindroom_model"] = {"id": self.model.model_id, "provider": self.model.provider}
        elif isinstance(self.model, InvalidStamp):
            out["mindroom_model"] = self.model.raw
        if isinstance(self.native_compaction, InvalidStamp):
            out["mindroom_native_compaction"] = self.native_compaction.raw
        elif self.native_compaction is not UNSET:
            out["mindroom_native_compaction"] = self.native_compaction
        return out


EMPTY_META = MessageMeta()

__all__ = [
    "EMPTY_META",
    "UNSET",
    "InvalidStamp",
    "MessageMeta",
    "ModelProvenance",
    "Unset",
]
