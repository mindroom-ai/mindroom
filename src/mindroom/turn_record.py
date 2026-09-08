"""Canonical durable facts for one Matrix turn."""

from __future__ import annotations

import typing
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.timestamp_formatting import normalize_timestamp_ms

if typing.TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class SourceEventMetadata:
    """Durable model-facing metadata for one source Matrix event."""

    sender: str
    timestamp_ms: float | None = None
    discovery_event_id: str | None = None

    def __post_init__(self) -> None:
        """Normalize the timestamp once for every physical representation."""
        object.__setattr__(self, "timestamp_ms", normalize_timestamp_ms(self.timestamp_ms))

    def _to_record(self) -> dict[str, object]:
        """Return a JSON-safe representation for durable metadata."""
        record: dict[str, object] = {"sender": self.sender}
        if self.timestamp_ms is not None:
            record["timestamp_ms"] = self.timestamp_ms
        if self.discovery_event_id is not None:
            record["discovery_event_id"] = self.discovery_event_id
        return record

    @classmethod
    def _from_raw(cls, raw_metadata: object) -> SourceEventMetadata | None:
        """Build source metadata from a persisted JSON-like value."""
        if not isinstance(raw_metadata, Mapping):
            return None
        metadata = typing.cast("Mapping[str, object]", raw_metadata)
        sender = metadata.get("sender")
        if not isinstance(sender, str) or not sender:
            return None
        timestamp_ms = normalize_timestamp_ms(metadata.get("timestamp_ms"))
        return cls(sender, timestamp_ms, canonical_optional_string(metadata.get("discovery_event_id")))


SourceEventRevision = tuple[int, str]


class EditPreparation(Enum):
    """A locked edit snapshot needs rebuilding without settling its callback."""

    REBUILD = "rebuild"


class RevisionSnapshotChangedError(RuntimeError):
    """A stale request must retain its callback and retry canonical preparation."""


@dataclass(frozen=True)
class RevisionReplay:
    """Physical edit provenance, independent of source completion and selection."""

    source_event_id: str
    timestamp_ms: int
    redacted: bool = False
    cleanup_pending: bool = False
    response_event_id: str | None = None
    legacy_summary_provenance: bool = False

    def to_record(self) -> dict[str, object]:
        """Serialize ledger-owned provenance and cleanup state."""
        return {
            "source_event_id": self.source_event_id,
            "timestamp_ms": self.timestamp_ms,
            "redacted": self.redacted,
            "cleanup_pending": self.cleanup_pending,
            "response_event_id": self.response_event_id,
            "legacy_summary_provenance": self.legacy_summary_provenance,
        }


def _revision_replay_map(raw: Mapping[str, object] | None) -> Mapping[str, RevisionReplay]:
    """Freeze validated ledger provenance."""
    result: dict[str, RevisionReplay] = {}
    for event_id, value in (raw or {}).items():
        if isinstance(value, RevisionReplay):
            result[event_id] = value
        elif isinstance(value, Mapping):
            value = typing.cast("Mapping[str, object]", value)
            source = value.get("source_event_id")
            timestamp = value.get("timestamp_ms")
            if isinstance(source, str) and isinstance(timestamp, int) and not isinstance(timestamp, bool):
                result[event_id] = RevisionReplay(
                    source,
                    timestamp,
                    value.get("redacted") is True,
                    value.get("cleanup_pending") is True,
                    canonical_optional_string(value.get("response_event_id")),
                    value.get("legacy_summary_provenance") is True,
                )
    return MappingProxyType(result)


@dataclass(frozen=True)
class _CanonicalSourceState:
    """Canonical source identity and source-owned facts."""

    source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...]
    redacted_source_event_ids: tuple[str, ...]
    pending_redaction_cleanup_event_ids: tuple[str, ...]
    anchor_event_id: str | None
    source_event_prompts: Mapping[str, str] | None
    source_event_revisions: Mapping[str, SourceEventRevision] | None
    suppressed_source_event_revisions: Mapping[str, SourceEventRevision] | None
    source_event_metadata: Mapping[str, SourceEventMetadata] | None


@dataclass(frozen=True)
class _CanonicalDeliveryState:
    """Canonical response and visible-echo linkage."""

    response_event_id: str | None
    visible_echo_event_id: str | None
    visible_echo_is_fallback: bool | None


@dataclass(frozen=True)
class _CanonicalDispatchState:
    """Canonical monotonic dispatch receipt orders."""

    latest_edit_receipt_order: int | None
    user_stop_receipt_order: int | None
    user_stop_settled_receipt_order: int | None


@dataclass(frozen=True)
class _CanonicalCommandState:
    """Canonical command execution checkpoint."""

    command_execution_started: bool
    command_result_text: str | None


@dataclass(frozen=True)
class _CanonicalContextState:
    """Canonical requester and conversation context."""

    response_owner: str | None
    requester_id: str | None
    correlation_id: str | None
    history_scope: HistoryScope | None
    conversation_target: MessageTarget | None


@dataclass(frozen=True)
class TurnRecord:
    """Canonical immutable identity, outcome, and regeneration facts for one turn."""

    source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...] = ()
    redacted_source_event_ids: tuple[str, ...] = ()
    pending_redaction_cleanup_event_ids: tuple[str, ...] = ()
    anchor_event_id: str | None = None
    response_event_id: str | None = None
    completed: bool = True
    visible_echo_event_id: str | None = None
    visible_echo_is_fallback: bool | None = None
    source_event_prompts: Mapping[str, str] | None = None
    source_event_revisions: Mapping[str, SourceEventRevision] | None = None
    revision_replay: Mapping[str, RevisionReplay] | None = None
    suppressed_source_event_revisions: Mapping[str, SourceEventRevision] | None = None
    latest_edit_receipt_order: int | None = None
    user_stop_receipt_order: int | None = None
    user_stop_settled_receipt_order: int | None = None
    source_event_metadata: Mapping[str, SourceEventMetadata] | None = None
    response_owner: str | None = None
    requester_id: str | None = None
    correlation_id: str | None = None
    command_execution_started: bool = False
    command_result_text: str | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None
    timestamp: float = 0.0

    @classmethod
    def create(
        cls,
        source_event_ids: Sequence[str],
        *,
        discovery_event_ids: Sequence[str] = (),
        redacted_source_event_ids: Sequence[str] = (),
        pending_redaction_cleanup_event_ids: Sequence[str] = (),
        anchor_event_id: str | None = None,
        response_event_id: str | None = None,
        completed: bool = True,
        visible_echo_event_id: str | None = None,
        visible_echo_is_fallback: bool | None = None,
        source_event_prompts: Mapping[str, str] | None = None,
        source_event_revisions: Mapping[str, object] | None = None,
        revision_replay: Mapping[str, object] | None = None,
        suppressed_source_event_revisions: Mapping[str, object] | None = None,
        latest_edit_receipt_order: int | None = None,
        user_stop_receipt_order: int | None = None,
        user_stop_settled_receipt_order: int | None = None,
        source_event_metadata: Mapping[str, object] | None = None,
        response_owner: str | None = None,
        requester_id: str | None = None,
        correlation_id: str | None = None,
        command_execution_started: bool = False,
        command_result_text: str | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
        timestamp: float = 0.0,
    ) -> TurnRecord:
        """Create a canonical record from permissive runtime inputs."""
        source = _canonical_source_state(
            source_event_ids,
            discovery_event_ids=discovery_event_ids,
            redacted_source_event_ids=redacted_source_event_ids,
            pending_redaction_cleanup_event_ids=pending_redaction_cleanup_event_ids,
            anchor_event_id=anchor_event_id,
            source_event_prompts=source_event_prompts,
            source_event_revisions=source_event_revisions,
            suppressed_source_event_revisions=suppressed_source_event_revisions,
            source_event_metadata=source_event_metadata,
        )
        delivery = _canonical_delivery_state(
            response_event_id,
            visible_echo_event_id,
            visible_echo_is_fallback,
        )
        dispatch = _canonical_dispatch_state(
            latest_edit_receipt_order,
            user_stop_receipt_order,
            user_stop_settled_receipt_order,
        )
        command = _canonical_command_state(command_execution_started, command_result_text)
        context = _canonical_context_state(
            response_owner,
            requester_id,
            correlation_id,
            history_scope,
            conversation_target,
        )
        return cls(
            source_event_ids=source.source_event_ids,
            discovery_event_ids=source.discovery_event_ids,
            redacted_source_event_ids=source.redacted_source_event_ids,
            pending_redaction_cleanup_event_ids=source.pending_redaction_cleanup_event_ids,
            anchor_event_id=source.anchor_event_id,
            response_event_id=delivery.response_event_id,
            completed=completed,
            visible_echo_event_id=delivery.visible_echo_event_id,
            visible_echo_is_fallback=delivery.visible_echo_is_fallback,
            source_event_prompts=source.source_event_prompts,
            source_event_revisions=source.source_event_revisions,
            revision_replay=_revision_replay_map(revision_replay) or None,
            suppressed_source_event_revisions=source.suppressed_source_event_revisions,
            latest_edit_receipt_order=dispatch.latest_edit_receipt_order,
            user_stop_receipt_order=dispatch.user_stop_receipt_order,
            user_stop_settled_receipt_order=dispatch.user_stop_settled_receipt_order,
            source_event_metadata=source.source_event_metadata,
            response_owner=context.response_owner,
            requester_id=context.requester_id,
            correlation_id=context.correlation_id,
            command_execution_started=command.command_execution_started,
            command_result_text=command.command_result_text,
            history_scope=context.history_scope,
            conversation_target=context.conversation_target,
            timestamp=_canonical_timestamp(timestamp),
        )

    @property
    def is_coalesced(self) -> bool:
        """Return whether the turn combines multiple source events."""
        return len(self.source_event_ids) > 1

    def revision_watermark(self, source_event_id: str) -> SourceEventRevision | None:
        """Keep stale callbacks stale after selecting an older surviving body."""
        source = self.prompt_source_event_id(source_event_id)
        revisions = [
            (value.timestamp_ms, event_id)
            for event_id, value in (self.revision_replay or {}).items()
            if value.source_event_id == source
        ]
        selected = (self.source_event_revisions or {}).get(source_event_id)
        if selected is not None:
            revisions.append(selected)
        return max(revisions, default=None)

    @property
    def invalidated_prompt_sources(self) -> frozenset[str]:
        """Return slots whose deleted revision still lacks a canonical refill."""
        prompts = self.source_event_prompts or {}
        return frozenset(
            value.source_event_id
            for value in (self.revision_replay or {}).values()
            if value.redacted
            and value.source_event_id not in prompts
            and value.source_event_id in self.replay_source_event_ids
        )

    @property
    def indexed_event_ids(self) -> tuple[str, ...]:
        """Return canonical source IDs followed by non-source discovery aliases."""
        return (*self.source_event_ids, *self.discovery_event_ids)

    def prompt_source_event_id(self, event_id: str) -> str:
        """Return the physical prompt owner for a source or discovery alias."""
        return _prompt_source_event_id(self.source_event_ids, self.source_event_metadata, event_id)

    def requester_id_for_source(self, event_id: str) -> str | None:
        """Return the exact requester for one source, or None when it cannot be proven."""
        if self.source_event_metadata is None:
            return self.requester_id if not self.is_coalesced else None
        metadata = self.source_event_metadata.get(self.prompt_source_event_id(event_id))
        return metadata.sender if metadata is not None else None

    def replay_sources_all_from_requester(self, requester_user_id: str) -> bool:
        """Return whether every replayable source is proven to belong to one requester."""
        replay_source_event_ids = self.replay_source_event_ids
        return bool(replay_source_event_ids) and all(
            self.requester_id_for_source(source_event_id) == requester_user_id
            for source_event_id in replay_source_event_ids
        )

    @property
    def replay_source_event_ids(self) -> tuple[str, ...]:
        """Return source IDs whose content remains eligible for replay or regeneration."""
        redacted_event_ids = {self.prompt_source_event_id(event_id) for event_id in self.redacted_source_event_ids}
        return tuple(event_id for event_id in self.source_event_ids if event_id not in redacted_event_ids)


class _TurnRecordChanges(typing.TypedDict, total=False):
    """Typed fields accepted by the explicit canonical update boundary."""

    source_event_ids: Sequence[str]
    discovery_event_ids: Sequence[str]
    redacted_source_event_ids: Sequence[str]
    pending_redaction_cleanup_event_ids: Sequence[str]
    anchor_event_id: str | None
    response_event_id: str | None
    completed: bool
    visible_echo_event_id: str | None
    visible_echo_is_fallback: bool | None
    source_event_prompts: Mapping[str, str] | None
    source_event_revisions: Mapping[str, object] | None
    revision_replay: Mapping[str, object] | None
    suppressed_source_event_revisions: Mapping[str, object] | None
    latest_edit_receipt_order: int | None
    user_stop_receipt_order: int | None
    user_stop_settled_receipt_order: int | None
    source_event_metadata: Mapping[str, object] | None
    response_owner: str | None
    requester_id: str | None
    correlation_id: str | None
    command_execution_started: bool
    command_result_text: str | None
    history_scope: HistoryScope | None
    conversation_target: MessageTarget | None
    timestamp: float


def canonicalize_turn_record(
    record: TurnRecord,
    **changes: typing.Unpack[_TurnRecordChanges],
) -> TurnRecord:
    """Return a canonical record after applying an explicit set of changes."""
    candidate = replace(record, **changes)
    return TurnRecord.create(
        candidate.source_event_ids,
        discovery_event_ids=candidate.discovery_event_ids,
        redacted_source_event_ids=candidate.redacted_source_event_ids,
        pending_redaction_cleanup_event_ids=candidate.pending_redaction_cleanup_event_ids,
        anchor_event_id=candidate.anchor_event_id,
        response_event_id=candidate.response_event_id,
        completed=candidate.completed,
        visible_echo_event_id=candidate.visible_echo_event_id,
        visible_echo_is_fallback=candidate.visible_echo_is_fallback,
        source_event_prompts=candidate.source_event_prompts,
        source_event_revisions=candidate.source_event_revisions,
        revision_replay=candidate.revision_replay,
        suppressed_source_event_revisions=candidate.suppressed_source_event_revisions,
        latest_edit_receipt_order=candidate.latest_edit_receipt_order,
        user_stop_receipt_order=candidate.user_stop_receipt_order,
        user_stop_settled_receipt_order=candidate.user_stop_settled_receipt_order,
        source_event_metadata=candidate.source_event_metadata,
        response_owner=candidate.response_owner,
        requester_id=candidate.requester_id,
        correlation_id=candidate.correlation_id,
        command_execution_started=candidate.command_execution_started,
        command_result_text=candidate.command_result_text,
        history_scope=candidate.history_scope,
        conversation_target=candidate.conversation_target,
        timestamp=candidate.timestamp,
    )


def _canonical_source_state(
    source_event_ids: Sequence[object],
    *,
    discovery_event_ids: Sequence[object],
    redacted_source_event_ids: Sequence[object],
    pending_redaction_cleanup_event_ids: Sequence[object],
    anchor_event_id: object,
    source_event_prompts: Mapping[str, str] | None,
    source_event_revisions: Mapping[str, object] | None,
    suppressed_source_event_revisions: Mapping[str, object] | None,
    source_event_metadata: Mapping[str, object] | None,
) -> _CanonicalSourceState:
    """Return canonical source identity and source-owned facts."""
    canonical_sources = canonical_source_event_ids(source_event_ids)
    source_ids = set(canonical_sources)
    canonical_discovery = tuple(
        event_id for event_id in canonical_source_event_ids(discovery_event_ids) if event_id not in source_ids
    )
    indexed_ids = {*canonical_sources, *canonical_discovery}
    canonical_redactions = tuple(
        event_id for event_id in canonical_source_event_ids(redacted_source_event_ids) if event_id in indexed_ids
    )
    redacted_ids = set(canonical_redactions)
    canonical_pending_cleanup = tuple(
        event_id
        for event_id in canonical_source_event_ids(pending_redaction_cleanup_event_ids)
        if event_id in redacted_ids
    )
    canonical_anchor = canonical_optional_string(anchor_event_id)
    if canonical_anchor is None and canonical_sources:
        canonical_anchor = canonical_sources[-1]
    canonical_metadata = _immutable_source_event_metadata(
        canonical_sources,
        source_event_metadata,
        excluded_event_ids=redacted_ids,
    )
    redacted_prompt_sources = {
        _prompt_source_event_id(canonical_sources, canonical_metadata, event_id) for event_id in canonical_redactions
    }
    return _CanonicalSourceState(
        source_event_ids=canonical_sources,
        discovery_event_ids=canonical_discovery,
        redacted_source_event_ids=canonical_redactions,
        pending_redaction_cleanup_event_ids=canonical_pending_cleanup,
        anchor_event_id=canonical_anchor,
        source_event_prompts=_immutable_prompt_map(
            canonical_sources,
            source_event_prompts,
            excluded_event_ids=redacted_prompt_sources,
        ),
        source_event_revisions=_immutable_source_event_revisions(
            (*canonical_sources, *canonical_discovery),
            source_event_revisions,
            excluded_event_ids=redacted_ids,
        ),
        suppressed_source_event_revisions=_immutable_source_event_revisions(
            (*canonical_sources, *canonical_discovery),
            suppressed_source_event_revisions,
            excluded_event_ids=redacted_ids,
        ),
        source_event_metadata=canonical_metadata,
    )


def _canonical_delivery_state(
    response_event_id: object,
    visible_echo_event_id: object,
    visible_echo_is_fallback: object,
) -> _CanonicalDeliveryState:
    """Return canonical response and visible-echo linkage."""
    canonical_echo_id = canonical_optional_string(visible_echo_event_id)
    return _CanonicalDeliveryState(
        response_event_id=canonical_optional_string(response_event_id),
        visible_echo_event_id=canonical_echo_id,
        visible_echo_is_fallback=(
            visible_echo_is_fallback
            if isinstance(visible_echo_is_fallback, bool) and canonical_echo_id is not None
            else None
        ),
    )


def _canonical_dispatch_state(
    latest_edit: object,
    user_stop: object,
    settled_user_stop: object,
) -> _CanonicalDispatchState:
    """Return canonical monotonic dispatch receipt orders."""
    latest_edit_order = _positive_int_or_none(latest_edit)
    user_stop_order = _positive_int_or_none(user_stop)
    settled_order = _positive_int_or_none(settled_user_stop)
    if user_stop_order is None or (settled_order is not None and settled_order > user_stop_order):
        settled_order = None
    return _CanonicalDispatchState(latest_edit_order, user_stop_order, settled_order)


def _canonical_command_state(started: object, result_text: object) -> _CanonicalCommandState:
    """Return a canonical command execution checkpoint."""
    canonical_result = canonical_optional_string(result_text)
    return _CanonicalCommandState(started is True or canonical_result is not None, canonical_result)


def _canonical_context_state(
    response_owner: object,
    requester_id: object,
    correlation_id: object,
    history_scope: object,
    conversation_target: object,
) -> _CanonicalContextState:
    """Return canonical requester and conversation context."""
    return _CanonicalContextState(
        response_owner=canonical_optional_string(response_owner),
        requester_id=canonical_optional_string(requester_id),
        correlation_id=canonical_optional_string(correlation_id),
        history_scope=history_scope if isinstance(history_scope, HistoryScope) else None,
        conversation_target=conversation_target if isinstance(conversation_target, MessageTarget) else None,
    )


def _canonical_timestamp(timestamp: object) -> float:
    """Return a canonical floating-point turn timestamp."""
    return float(timestamp) if isinstance(timestamp, int | float) and not isinstance(timestamp, bool) else 0.0


def _prompt_source_event_id(
    source_event_ids: tuple[str, ...],
    source_event_metadata: Mapping[str, SourceEventMetadata] | None,
    event_id: str,
) -> str:
    """Return the physical prompt owner for a source or discovery alias."""
    if event_id in source_event_ids:
        return event_id
    for source_id, metadata in (source_event_metadata or {}).items():
        if metadata.discovery_event_id == event_id:
            return source_id
    return event_id


def canonical_source_event_ids(source_event_ids: Sequence[object]) -> tuple[str, ...]:
    """Deduplicate non-empty source event IDs while preserving order."""
    normalized_event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    for event_id in source_event_ids:
        if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        normalized_event_ids.append(event_id)
    return tuple(normalized_event_ids)


def canonical_optional_string(value: object) -> str | None:
    """Return a non-empty string or None."""
    return value if isinstance(value, str) and value else None


def _positive_int_or_none(value: object) -> int | None:
    """Return one positive non-boolean integer or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _immutable_prompt_map(
    source_event_ids: tuple[str, ...],
    source_event_prompts: Mapping[str, str] | None,
    *,
    excluded_event_ids: set[str],
) -> Mapping[str, str] | None:
    """Freeze prompt entries that belong to the canonical source identity."""
    if not source_event_prompts:
        return None
    prompt_map = {
        event_id: prompt
        for event_id in source_event_ids
        if event_id not in excluded_event_ids
        if isinstance((prompt := source_event_prompts.get(event_id)), str)
    }
    return MappingProxyType(prompt_map) if prompt_map else None


def _immutable_source_event_revisions(
    indexed_event_ids: tuple[str, ...],
    source_event_revisions: Mapping[str, object] | None,
    *,
    excluded_event_ids: set[str],
) -> Mapping[str, SourceEventRevision] | None:
    """Normalize and freeze edit revisions belonging to canonical live sources."""
    if not source_event_revisions:
        return None
    revisions = {
        event_id: (raw_revision[0], raw_revision[1])
        for event_id in indexed_event_ids
        if event_id not in excluded_event_ids
        if isinstance((raw_revision := source_event_revisions.get(event_id)), tuple | list)
        and len(raw_revision) == 2
        and isinstance(raw_revision[0], int)
        and not isinstance(raw_revision[0], bool)
        and isinstance(raw_revision[1], str)
        and raw_revision[1]
    }
    return MappingProxyType(revisions) if revisions else None


def _immutable_source_event_metadata(
    source_event_ids: tuple[str, ...],
    source_event_metadata: Mapping[str, object] | None,
    *,
    excluded_event_ids: set[str],
) -> Mapping[str, SourceEventMetadata] | None:
    """Normalize and freeze source metadata belonging to the canonical identity."""
    if source_event_metadata is None:
        return None
    metadata: dict[str, SourceEventMetadata] = {}
    for event_id in source_event_ids:
        if event_id in excluded_event_ids:
            continue
        raw_metadata = source_event_metadata.get(event_id)
        normalized = (
            raw_metadata
            if isinstance(raw_metadata, SourceEventMetadata)
            else SourceEventMetadata._from_raw(raw_metadata)
        )
        if normalized is not None:
            metadata[event_id] = normalized
    return MappingProxyType(metadata)


def same_turn_identity(first: TurnRecord, second: TurnRecord) -> bool:
    """Return whether two records identify the same canonical source turn."""
    return first.source_event_ids == second.source_event_ids and first.anchor_event_id == second.anchor_event_id


def merge_edit_facts(ledger: TurnRecord, recovery: TurnRecord) -> tuple[dict[str, str], dict[str, SourceEventRevision]]:
    """Merge source prompts and revisions by canonical Matrix revision."""
    recovery = sanitize_revision_replay(recovery, authority=ledger)
    prompts = dict(ledger.source_event_prompts or {})
    prompts.update(recovery.source_event_prompts or {})
    revisions = {
        event_id: revision
        for event_id, revision in (recovery.source_event_revisions or {}).items()
        if recovery.prompt_source_event_id(event_id) in (recovery.source_event_prompts or {})
    }
    ledger_prompts = ledger.source_event_prompts or {}
    for event_id, revision in (ledger.source_event_revisions or {}).items():
        prompt_event_id = ledger.prompt_source_event_id(event_id)
        if revision >= revisions.get(event_id, revision) and prompt_event_id in ledger_prompts:
            revisions[event_id] = revision
            prompts[prompt_event_id] = ledger_prompts[prompt_event_id]
    return prompts, revisions


def completed_response_record(
    record: TurnRecord,
    response_event_id: str,
    *,
    consumed_revisions: tuple[SourceEventRevision, ...] | None = None,
) -> TurnRecord:
    """Attribute only this generated snapshot's selected revisions to its answer."""
    selected = (
        tuple((record.source_event_revisions or {}).values()) if consumed_revisions is None else consumed_revisions
    )
    record = sanitize_revision_replay(record)
    replay = dict(record.revision_replay or {})
    for _, revision_id in selected:
        revision = replay.get(revision_id)
        if revision is not None:
            replay[revision_id] = replace(revision, response_event_id=response_event_id)
    return canonicalize_turn_record(
        record,
        response_event_id=response_event_id,
        completed=True,
        revision_replay=replay,
    )


def merge_committed_response(
    current: TurnRecord | None,
    committed: TurnRecord,
    *,
    tombstoned_event_ids: typing.Collection[str] = (),
) -> TurnRecord | None:
    """Join delivered proof with current authority without inventing consumed revisions."""
    if current is None:
        return sanitize_revision_replay(committed, tombstoned_event_ids=tombstoned_event_ids)
    if not same_turn_identity(current, committed) or any(
        before is not None and after is not None and before != after
        for before, after in (
            (current.response_owner, committed.response_owner),
            (current.requester_id, committed.requester_id),
            (current.conversation_target, committed.conversation_target),
            (current.history_scope, committed.history_scope),
        )
    ):
        return None
    proofs = {
        event_id: revision.response_event_id
        for event_id, revision in (committed.revision_replay or {}).items()
        if revision.response_event_id is not None
    }
    committed = sanitize_revision_replay(
        committed,
        authority=current,
        tombstoned_event_ids=tombstoned_event_ids,
    )
    replay = dict(committed.revision_replay or {})
    for event_id, response_event_id in proofs.items():
        replay[event_id] = replace(
            replay[event_id],
            response_event_id=replay[event_id].response_event_id or response_event_id,
        )
    prompts, revisions = merge_edit_facts(current, committed)
    return sanitize_revision_replay(
        canonicalize_turn_record(
            current,
            response_event_id=current.response_event_id or committed.response_event_id,
            completed=True,
            source_event_prompts=prompts,
            source_event_revisions=revisions,
            revision_replay=replay,
            latest_edit_receipt_order=max(
                current.latest_edit_receipt_order or 0,
                committed.latest_edit_receipt_order or 0,
            )
            or None,
            timestamp=max(current.timestamp, committed.timestamp),
        ),
        tombstoned_event_ids=tombstoned_event_ids,
    )


def sanitize_revision_replay(  # noqa: C901
    candidate: TurnRecord,
    *,
    authority: TurnRecord | None = None,
    tombstoned_event_ids: typing.Collection[str] = (),
) -> TurnRecord:
    """Join monotonic invalidation before removing any candidate provenance.

    Acknowledgement is final for one physical revision: no future request may
    consume it. Only ledger records carry cleanup state; model runs cannot
    acknowledge debt or restore it after acknowledgement.
    """
    replay = dict(candidate.revision_replay or {})
    for source, (timestamp, revision_id) in (candidate.source_event_revisions or {}).items():
        prompt_source = candidate.prompt_source_event_id(source)
        if revision_id != prompt_source:
            replay.setdefault(revision_id, RevisionReplay(prompt_source, timestamp))
    for event_id, old in (authority.revision_replay or {}).items() if authority else ():
        new = replay.get(event_id)
        if new is None or (old.redacted and not new.redacted):
            replay[event_id] = old
        elif old.redacted and new.redacted:
            replay[event_id] = replace(
                old,
                cleanup_pending=old.cleanup_pending and new.cleanup_pending,
                response_event_id=old.response_event_id or new.response_event_id,
            )
        elif old.response_event_id is not None and new.response_event_id is None:
            replay[event_id] = replace(new, response_event_id=old.response_event_id)
        if old.legacy_summary_provenance:
            replay[event_id] = replace(replay[event_id], legacy_summary_provenance=True)
    for event_id in tombstoned_event_ids:
        value = replay.get(event_id)
        if value is not None and not value.redacted:
            replay[event_id] = replace(value, redacted=True, cleanup_pending=True)
    prompts = dict(candidate.source_event_prompts or {})
    revisions = dict(candidate.source_event_revisions or {})
    invalid_sources = {value.source_event_id for value in replay.values() if value.redacted}
    for source in invalid_sources:
        selected = next(
            (revision for key, revision in revisions.items() if candidate.prompt_source_event_id(key) == source),
            None,
        )
        if selected is None or (selected[1] in replay and replay[selected[1]].redacted):
            prompts.pop(source, None)
            revisions = {
                key: revision for key, revision in revisions.items() if candidate.prompt_source_event_id(key) != source
            }
    return canonicalize_turn_record(
        candidate,
        revision_replay=replay or None,
        source_event_prompts=prompts or None,
        source_event_revisions=revisions or None,
    )
