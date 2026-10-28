"""Canonical durable facts for one Matrix turn."""

from __future__ import annotations

import json
import typing
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.model_selection import CommandResultContent, freeze_command_result_content
from mindroom.timestamp_formatting import normalize_timestamp_ms

if typing.TYPE_CHECKING:
    from collections.abc import Collection, Sequence


@dataclass(frozen=True)
class PreparedVoiceSource:
    """Immutable, short-lived normalization checkpoint for one physical voice source."""

    body: str
    content_json: str
    thread_id: str | None = None
    coalescing_thread_id: str | None = None

    def __post_init__(self) -> None:
        """Validate direct construction so every snapshot can restore Matrix content."""
        content = json.loads(self.content_json)
        if (
            not isinstance(self.body, str)
            or not isinstance(content, dict)
            or (self.thread_id is not None and not isinstance(self.thread_id, str))
            or (self.coalescing_thread_id is not None and not isinstance(self.coalescing_thread_id, str))
        ):
            msg = "Prepared voice requires text, object content, and an optional thread ID"
            raise ValueError(msg)
        object.__setattr__(self, "content_json", json.dumps(content, allow_nan=False))

    @classmethod
    def from_content(
        cls,
        body: str,
        content: Mapping[str, object],
        *,
        thread_id: str | None = None,
        coalescing_thread_id: str | None = None,
    ) -> PreparedVoiceSource:
        """Freeze nested Matrix content without retaining live ingress objects."""
        return cls(body, json.dumps(dict(content), allow_nan=False), thread_id, coalescing_thread_id)

    def to_content(self) -> dict[str, typing.Any]:
        """Return an independent mutable content tree for this dispatch attempt."""
        return typing.cast("dict[str, typing.Any]", json.loads(self.content_json))

    def to_record(self) -> dict[str, object]:
        """Encode the checkpoint only in the handled-turn ledger."""
        return {
            "body": self.body,
            "content_json": self.content_json,
            "thread_id": self.thread_id,
            "coalescing_thread_id": self.coalescing_thread_id,
        }

    @classmethod
    def _from_raw(cls, raw: object) -> PreparedVoiceSource | None:
        """Discard malformed checkpoints independently of their turn and siblings."""
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, Mapping):
            return None
        raw = typing.cast("Mapping[str, object]", raw)
        body, content_json, thread_id = raw.get("body"), raw.get("content_json"), raw.get("thread_id")
        if not isinstance(body, str) or not isinstance(content_json, str):
            return None
        coalescing_thread_id = raw.get("coalescing_thread_id")
        if (thread_id is not None and not isinstance(thread_id, str)) or (
            coalescing_thread_id is not None and not isinstance(coalescing_thread_id, str)
        ):
            return None
        try:
            return cls(body, content_json, thread_id, coalescing_thread_id)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class SourceEventMetadata:
    """Durable model-facing metadata for one source Matrix event."""

    # The requester that owns this source.
    sender: str
    timestamp_ms: float | None = None
    discovery_event_id: str | None = None
    # The entity that wrote this source for ``sender``; marks an entity-written source that edits never regenerate.
    speaker: str | None = None

    def __post_init__(self) -> None:
        """Normalize the timestamp once for every physical representation."""
        object.__setattr__(self, "timestamp_ms", normalize_timestamp_ms(self.timestamp_ms))

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
        return cls(
            sender,
            timestamp_ms,
            canonical_optional_string(metadata.get("discovery_event_id")),
            canonical_optional_string(metadata.get("speaker")),
        )


SourceEventRevision = tuple[int, str]


@dataclass(frozen=True)
class SourceVisibleEcho:
    """The visible echo published for one voice source, and how its replacement ended."""

    event_id: str
    # None until a replacement lands; True when a fallback replaced it, False for a transcript.
    is_fallback: bool | None = None


class RevisionSnapshotChangedError(RuntimeError):
    """A stale request must retain its callback and retry canonical preparation."""


@dataclass(frozen=True)
class RevisionReplay:
    """Physical edit provenance, independent of source completion and selection."""

    source_event_id: str
    timestamp_ms: int
    # The response that consumed this revision, once one did.
    response_event_id: str | None = None


@dataclass(frozen=True)
class _CanonicalSourceState:
    """Canonical source identity and source-owned facts."""

    source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...]
    anchor_event_id: str | None
    source_event_prompts: Mapping[str, str] | None
    source_event_revisions: Mapping[str, SourceEventRevision] | None
    source_event_metadata: Mapping[str, SourceEventMetadata] | None
    prepared_voice_sources: Mapping[str, PreparedVoiceSource] | None


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
    history_scope: HistoryScope | None
    conversation_target: MessageTarget | None


@dataclass(frozen=True)
class TurnRecord:
    """Canonical immutable identity, outcome, and regeneration facts for one turn."""

    source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...] = ()
    anchor_event_id: str | None = None
    response_event_id: str | None = None
    completed: bool = True
    source_event_prompts: Mapping[str, str] | None = None
    source_event_revisions: Mapping[str, SourceEventRevision] | None = None
    revision_replay: Mapping[str, RevisionReplay] | None = None
    source_event_metadata: Mapping[str, SourceEventMetadata] | None = None
    prepared_voice_sources: Mapping[str, PreparedVoiceSource] | None = None
    response_owner: str | None = None
    requester_id: str | None = None
    command_execution_started: bool = False
    command_result_text: str | None = None
    command_result_extra_content: CommandResultContent | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None
    # The immutable identity of the stored turn this value was read from, if any.
    turn_key: str | None = None
    # The echo published for each voice source, a publication fact apart from the turn's answer.
    visible_echoes: Mapping[str, SourceVisibleEcho] | None = None

    @classmethod
    def create(
        cls,
        source_event_ids: Sequence[str],
        *,
        discovery_event_ids: Sequence[str] = (),
        anchor_event_id: str | None = None,
        response_event_id: str | None = None,
        completed: bool = True,
        source_event_prompts: Mapping[str, str] | None = None,
        source_event_revisions: Mapping[str, object] | None = None,
        revision_replay: Mapping[str, RevisionReplay] | None = None,
        source_event_metadata: Mapping[str, object] | None = None,
        prepared_voice_sources: Mapping[str, object] | None = None,
        response_owner: str | None = None,
        requester_id: str | None = None,
        command_execution_started: bool = False,
        command_result_text: str | None = None,
        command_result_extra_content: CommandResultContent | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
        turn_key: str | None = None,
        visible_echoes: Mapping[str, SourceVisibleEcho] | None = None,
    ) -> TurnRecord:
        """Create a canonical record from permissive runtime inputs."""
        source = _canonical_source_state(
            source_event_ids,
            discovery_event_ids=discovery_event_ids,
            anchor_event_id=anchor_event_id,
            source_event_prompts=source_event_prompts,
            source_event_revisions=source_event_revisions,
            source_event_metadata=source_event_metadata,
            prepared_voice_sources=prepared_voice_sources if not completed else None,
        )
        command = _canonical_command_state(command_execution_started, command_result_text)
        context = _canonical_context_state(
            response_owner,
            requester_id,
            history_scope,
            conversation_target,
        )
        return cls(
            source_event_ids=source.source_event_ids,
            discovery_event_ids=source.discovery_event_ids,
            anchor_event_id=source.anchor_event_id,
            response_event_id=canonical_optional_string(response_event_id),
            completed=completed,
            source_event_prompts=source.source_event_prompts,
            source_event_revisions=source.source_event_revisions,
            revision_replay=_revision_replay_map(revision_replay),
            source_event_metadata=source.source_event_metadata,
            prepared_voice_sources=source.prepared_voice_sources,
            response_owner=context.response_owner,
            requester_id=context.requester_id,
            command_execution_started=command.command_execution_started,
            command_result_text=command.command_result_text,
            command_result_extra_content=(
                freeze_command_result_content(command_result_extra_content)
                if command.command_result_text is not None
                else None
            ),
            history_scope=context.history_scope,
            conversation_target=context.conversation_target,
            turn_key=canonical_optional_string(turn_key),
            visible_echoes=_canonical_visible_echoes(source.source_event_ids, visible_echoes),
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

    def sources_all_from_requester(
        self,
        requester_user_id: str,
        *,
        deleted: Collection[str] = (),
    ) -> bool:
        """Return whether every source not ``deleted`` is proven to belong to one requester."""
        sources = [source_event_id for source_event_id in self.source_event_ids if source_event_id not in deleted]
        return bool(sources) and all(
            self.requester_id_for_source(source_event_id) == requester_user_id for source_event_id in sources
        )


class _TurnRecordChanges(typing.TypedDict, total=False):
    """Typed fields accepted by the explicit canonical update boundary."""

    source_event_ids: Sequence[str]
    discovery_event_ids: Sequence[str]
    anchor_event_id: str | None
    response_event_id: str | None
    completed: bool
    source_event_prompts: Mapping[str, str] | None
    source_event_revisions: Mapping[str, object] | None
    revision_replay: Mapping[str, RevisionReplay] | None
    source_event_metadata: Mapping[str, object] | None
    prepared_voice_sources: Mapping[str, object] | None
    response_owner: str | None
    requester_id: str | None
    command_execution_started: bool
    command_result_text: str | None
    command_result_extra_content: CommandResultContent | None
    history_scope: HistoryScope | None
    conversation_target: MessageTarget | None
    turn_key: str | None
    visible_echoes: Mapping[str, SourceVisibleEcho] | None


def canonicalize_turn_record(
    record: TurnRecord,
    **changes: typing.Unpack[_TurnRecordChanges],
) -> TurnRecord:
    """Return a canonical record after applying an explicit set of changes."""
    candidate = replace(record, **changes)
    return TurnRecord.create(
        candidate.source_event_ids,
        discovery_event_ids=candidate.discovery_event_ids,
        anchor_event_id=candidate.anchor_event_id,
        response_event_id=candidate.response_event_id,
        completed=candidate.completed,
        source_event_prompts=candidate.source_event_prompts,
        source_event_revisions=candidate.source_event_revisions,
        revision_replay=candidate.revision_replay,
        source_event_metadata=candidate.source_event_metadata,
        prepared_voice_sources=candidate.prepared_voice_sources,
        response_owner=candidate.response_owner,
        requester_id=candidate.requester_id,
        command_execution_started=candidate.command_execution_started,
        command_result_text=candidate.command_result_text,
        command_result_extra_content=candidate.command_result_extra_content,
        history_scope=candidate.history_scope,
        conversation_target=candidate.conversation_target,
        turn_key=candidate.turn_key,
        visible_echoes=candidate.visible_echoes,
    )


def _revision_replay_map(raw: Mapping[str, RevisionReplay] | None) -> Mapping[str, RevisionReplay] | None:
    """Freeze edit provenance."""
    replay = {event_id: value for event_id, value in (raw or {}).items() if isinstance(value, RevisionReplay)}
    return MappingProxyType(replay) if replay else None


def _canonical_source_state(
    source_event_ids: Sequence[object],
    *,
    discovery_event_ids: Sequence[object],
    anchor_event_id: object,
    source_event_prompts: Mapping[str, str] | None,
    source_event_revisions: Mapping[str, object] | None,
    source_event_metadata: Mapping[str, object] | None,
    prepared_voice_sources: Mapping[str, object] | None,
) -> _CanonicalSourceState:
    """Return canonical source identity and source-owned facts."""
    canonical_sources = canonical_source_event_ids(source_event_ids)
    source_ids = set(canonical_sources)
    canonical_discovery = tuple(
        event_id for event_id in canonical_source_event_ids(discovery_event_ids) if event_id not in source_ids
    )
    canonical_anchor = canonical_optional_string(anchor_event_id)
    if canonical_anchor is None and canonical_sources:
        canonical_anchor = canonical_sources[-1]
    return _CanonicalSourceState(
        source_event_ids=canonical_sources,
        discovery_event_ids=canonical_discovery,
        anchor_event_id=canonical_anchor,
        source_event_prompts=_immutable_prompt_map(canonical_sources, source_event_prompts),
        source_event_revisions=_immutable_source_event_revisions(
            (*canonical_sources, *canonical_discovery),
            source_event_revisions,
        ),
        source_event_metadata=_immutable_source_event_metadata(canonical_sources, source_event_metadata),
        prepared_voice_sources=MappingProxyType(
            {
                event_id: prepared
                for event_id, raw in (prepared_voice_sources or {}).items()
                if event_id in source_ids and (prepared := PreparedVoiceSource._from_raw(raw)) is not None
            },
        )
        or None,
    )


def _canonical_visible_echoes(
    source_event_ids: tuple[str, ...],
    visible_echoes: Mapping[str, SourceVisibleEcho] | None,
) -> Mapping[str, SourceVisibleEcho] | None:
    """Keep the echoes of physical sources only."""
    echoes = {
        event_id: echo
        for event_id in source_event_ids
        if isinstance((echo := (visible_echoes or {}).get(event_id)), SourceVisibleEcho)
    }
    return MappingProxyType(echoes) if echoes else None


def _canonical_command_state(started: object, result_text: object) -> _CanonicalCommandState:
    """Return a canonical command execution checkpoint."""
    canonical_result = canonical_optional_string(result_text)
    return _CanonicalCommandState(started is True or canonical_result is not None, canonical_result)


def _canonical_context_state(
    response_owner: object,
    requester_id: object,
    history_scope: object,
    conversation_target: object,
) -> _CanonicalContextState:
    """Return canonical requester and conversation context."""
    return _CanonicalContextState(
        response_owner=canonical_optional_string(response_owner),
        requester_id=canonical_optional_string(requester_id),
        history_scope=history_scope if isinstance(history_scope, HistoryScope) else None,
        conversation_target=conversation_target if isinstance(conversation_target, MessageTarget) else None,
    )


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


def _immutable_prompt_map(
    source_event_ids: tuple[str, ...],
    source_event_prompts: Mapping[str, str] | None,
) -> Mapping[str, str] | None:
    """Freeze prompt entries that belong to the canonical source identity."""
    if not source_event_prompts:
        return None
    prompt_map = {
        event_id: prompt
        for event_id in source_event_ids
        if isinstance((prompt := source_event_prompts.get(event_id)), str)
    }
    return MappingProxyType(prompt_map) if prompt_map else None


def _immutable_source_event_revisions(
    indexed_event_ids: tuple[str, ...],
    source_event_revisions: Mapping[str, object] | None,
) -> Mapping[str, SourceEventRevision] | None:
    """Normalize and freeze edit revisions belonging to canonical sources and aliases."""
    if not source_event_revisions:
        return None
    revisions = {
        event_id: (raw_revision[0], raw_revision[1])
        for event_id in indexed_event_ids
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
) -> Mapping[str, SourceEventMetadata] | None:
    """Normalize and freeze source metadata belonging to the canonical identity."""
    if source_event_metadata is None:
        return None
    metadata: dict[str, SourceEventMetadata] = {}
    for event_id in source_event_ids:
        raw_metadata = source_event_metadata.get(event_id)
        normalized = (
            raw_metadata
            if isinstance(raw_metadata, SourceEventMetadata)
            else SourceEventMetadata._from_raw(raw_metadata)
        )
        if normalized is not None:
            metadata[event_id] = normalized
    return MappingProxyType(metadata)
