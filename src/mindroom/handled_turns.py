"""Track handled turn outcomes for one agent."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, NotRequired, TypedDict
from uuid import uuid4

from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget

logger = get_logger(__name__)


class _SerializedHistoryScope(TypedDict):
    """JSON-safe persisted history-scope identity."""

    kind: Literal["agent", "team"]
    scope_id: str


class _SerializedConversationTarget(TypedDict):
    """JSON-safe persisted conversation target."""

    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    reply_to_event_id: str | None
    session_id: str


class _SerializedHandledTurnRecord(TypedDict):
    """Record of one handled source event persisted to disk."""

    timestamp: float
    response_event_id: str | None
    completed: NotRequired[bool]
    anchor_event_id: NotRequired[str]
    response_transaction_id: NotRequired[str]
    visible_echo_transaction_id: NotRequired[str]
    visible_echo_event_id: NotRequired[str | None]
    pending_response_text: NotRequired[str | None]
    source_event_ids: NotRequired[list[str]]
    source_event_prompts: NotRequired[dict[str, str] | None]
    response_owner: NotRequired[str | None]
    history_scope: NotRequired[_SerializedHistoryScope | None]
    conversation_target: NotRequired[_SerializedConversationTarget | None]


type _SerializedHandledTurnRecordLike = _SerializedHandledTurnRecord | dict[str, Any]


@dataclass(frozen=True)
class HandledTurnState:
    """Typed handled-turn facts carried through normal bot runtime flow."""

    source_event_ids: tuple[str, ...]
    response_event_id: str | None = None
    visible_echo_event_id: str | None = None
    source_event_prompts: dict[str, str] | None = None
    response_owner: str | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None

    @classmethod
    def create(
        cls,
        source_event_ids: typing.Sequence[str],
        *,
        response_event_id: str | None = None,
        visible_echo_event_id: str | None = None,
        source_event_prompts: typing.Mapping[str, str] | None = None,
        response_owner: str | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
    ) -> HandledTurnState:
        """Normalize one handled-turn state carrier."""
        normalized_source_event_ids = _normalize_source_event_ids(source_event_ids)
        return cls(
            source_event_ids=normalized_source_event_ids,
            response_event_id=_normalized_event_id(response_event_id),
            visible_echo_event_id=_normalized_event_id(visible_echo_event_id),
            source_event_prompts=_explicit_prompt_map_for_sources(
                normalized_source_event_ids,
                source_event_prompts,
            ),
            response_owner=_normalized_response_owner(response_owner),
            history_scope=_normalized_history_scope(history_scope),
            conversation_target=_normalized_conversation_target(conversation_target),
        )

    @classmethod
    def from_source_event_id(
        cls,
        source_event_id: str,
        *,
        response_event_id: str | None = None,
        visible_echo_event_id: str | None = None,
        source_event_prompts: typing.Mapping[str, str] | None = None,
        response_owner: str | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
    ) -> HandledTurnState:
        """Build handled-turn state for one source event."""
        return cls.create(
            [source_event_id],
            response_event_id=response_event_id,
            visible_echo_event_id=visible_echo_event_id,
            source_event_prompts=source_event_prompts,
            response_owner=response_owner,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )

    @property
    def anchor_event_id(self) -> str:
        """Return the event this turn anchors replies and regeneration to."""
        return self.source_event_ids[-1]

    @property
    def is_coalesced(self) -> bool:
        """Return whether the turn combines multiple source events."""
        return len(self.source_event_ids) > 1

    def with_response_event_id(self, response_event_id: str | None) -> HandledTurnState:
        """Return a copy with updated response linkage."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            response_owner=self.response_owner,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_visible_echo_event_id(self, visible_echo_event_id: str | None) -> HandledTurnState:
        """Return a copy with updated visible-echo linkage."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            response_owner=self.response_owner,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_source_event_prompts(
        self,
        source_event_prompts: typing.Mapping[str, str] | None,
    ) -> HandledTurnState:
        """Return a copy with updated coalesced prompt metadata."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=source_event_prompts,
            response_owner=self.response_owner,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_response_context(
        self,
        *,
        response_owner: str | None,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget | None,
    ) -> HandledTurnState:
        """Return a copy with persisted regeneration context attached."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            response_owner=response_owner,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )


@dataclass(frozen=True)
class HandledTurnRecord:
    """Immutable record for one handled turn."""

    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    response_event_id: str | None = None
    response_transaction_id: str | None = None
    visible_echo_transaction_id: str | None = None
    completed: bool = True
    visible_echo_event_id: str | None = None
    pending_response_text: str | None = None
    source_event_prompts: dict[str, str] | None = None
    response_owner: str | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None
    timestamp: float = 0.0

    @property
    def is_coalesced(self) -> bool:
        """Return whether the turn combined multiple source events."""
        return len(self.source_event_ids) > 1


@dataclass
class HandledTurnLedger:
    """Track handled source events for one runtime entity."""

    agent_name: str
    base_path: Path
    _responses: dict[str, _SerializedHandledTurnRecord] = field(default_factory=dict, init=False)
    _responses_file: Path = field(init=False)
    _responses_lock_file: Path = field(init=False)
    _thread_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize paths and load existing handled turns."""
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._responses_file = _responses_file_path(self.base_path, self.agent_name)
        self._responses_lock_file = self._responses_file.with_suffix(f"{self._responses_file.suffix}.lock")
        self._load_responses()
        self._cleanup_old_events()

    def record_handled_turn(self, handled_turn: HandledTurnState) -> None:
        """Record one handled-turn state as a terminal outcome."""
        normalized_source_event_ids = handled_turn.source_event_ids
        if not normalized_source_event_ids:
            return

        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            self._persist_handled_turn_locked(
                source_event_ids=normalized_source_event_ids,
                response_event_id=handled_turn.response_event_id,
                response_transaction_id=None,
                visible_echo_transaction_id=None,
                completed=True,
                visible_echo_event_id=handled_turn.visible_echo_event_id,
                source_event_prompts=handled_turn.source_event_prompts,
                response_owner=handled_turn.response_owner,
                history_scope=handled_turn.history_scope,
                conversation_target=handled_turn.conversation_target,
            )
            self._save_responses_locked()
        logger.debug("handled_turn_recorded", source_event_count=len(normalized_source_event_ids))

    def record_handled_turn_record(self, turn_record: HandledTurnRecord) -> None:
        """Record one exact handled-turn record without losing its explicit anchor."""
        normalized_source_event_ids = _normalize_source_event_ids(turn_record.source_event_ids)
        if not normalized_source_event_ids:
            return

        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            self._persist_handled_turn_locked(
                normalized_source_event_ids,
                response_event_id=turn_record.response_event_id,
                response_transaction_id=turn_record.response_transaction_id,
                visible_echo_transaction_id=turn_record.visible_echo_transaction_id,
                completed=turn_record.completed,
                visible_echo_event_id=turn_record.visible_echo_event_id,
                pending_response_text=turn_record.pending_response_text,
                source_event_prompts=turn_record.source_event_prompts,
                response_owner=turn_record.response_owner,
                history_scope=turn_record.history_scope,
                conversation_target=turn_record.conversation_target,
                anchor_event_id=turn_record.anchor_event_id,
            )
            self._save_responses_locked()
        logger.debug("handled_turn_recorded", source_event_count=len(normalized_source_event_ids))

    def reserve_response_transaction_id(self, handled_turn: HandledTurnState) -> str:
        """Persist and return one stable outbound transaction id for a pending turn."""
        normalized_source_event_ids = handled_turn.source_event_ids
        if not normalized_source_event_ids:
            msg = "Cannot reserve a response transaction id without source events"
            raise ValueError(msg)

        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            for event_id in normalized_source_event_ids:
                existing_transaction_id = _response_transaction_id_for_record(self._responses.get(event_id))
                if existing_transaction_id is not None:
                    return existing_transaction_id

            transaction_id = f"reply_{uuid4().hex}"
            self._persist_handled_turn_locked(
                source_event_ids=normalized_source_event_ids,
                response_event_id=handled_turn.response_event_id,
                response_transaction_id=transaction_id,
                visible_echo_transaction_id=None,
                completed=False,
                visible_echo_event_id=handled_turn.visible_echo_event_id,
                source_event_prompts=handled_turn.source_event_prompts,
                response_owner=handled_turn.response_owner,
                history_scope=handled_turn.history_scope,
                conversation_target=handled_turn.conversation_target,
                anchor_event_id=handled_turn.anchor_event_id,
            )
            self._save_responses_locked()
        logger.debug(
            "response_transaction_id_reserved",
            agent=self.agent_name,
            response_transaction_id=transaction_id,
            source_event_count=len(normalized_source_event_ids),
        )
        return transaction_id

    def record_pending_response_event(self, handled_turn: HandledTurnState) -> None:
        """Persist the first visible response event without marking the turn complete."""
        normalized_source_event_ids = handled_turn.source_event_ids
        if not normalized_source_event_ids or handled_turn.response_event_id is None:
            return

        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            completed = any(
                _completed_for_record(self._responses.get(event_id)) for event_id in normalized_source_event_ids
            )
            self._persist_handled_turn_locked(
                source_event_ids=normalized_source_event_ids,
                response_event_id=handled_turn.response_event_id,
                response_transaction_id=None,
                visible_echo_transaction_id=None,
                completed=completed,
                visible_echo_event_id=handled_turn.visible_echo_event_id,
                source_event_prompts=handled_turn.source_event_prompts,
                response_owner=handled_turn.response_owner,
                history_scope=handled_turn.history_scope,
                conversation_target=handled_turn.conversation_target,
                anchor_event_id=handled_turn.anchor_event_id,
            )
            self._save_responses_locked()
        logger.debug(
            "pending_response_event_recorded",
            agent=self.agent_name,
            response_event_id=handled_turn.response_event_id,
            source_event_count=len(normalized_source_event_ids),
        )

    def record_pending_command_reply(
        self,
        handled_turn: HandledTurnState,
        *,
        response_text: str,
        response_transaction_id: str,
    ) -> None:
        """Persist a command reply snapshot after the side effect succeeded but before the send."""
        normalized_source_event_ids = handled_turn.source_event_ids
        if not normalized_source_event_ids:
            return

        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            completed = any(
                _completed_for_record(self._responses.get(event_id)) for event_id in normalized_source_event_ids
            )
            self._persist_handled_turn_locked(
                source_event_ids=normalized_source_event_ids,
                response_event_id=handled_turn.response_event_id,
                response_transaction_id=response_transaction_id,
                visible_echo_transaction_id=None,
                completed=completed,
                visible_echo_event_id=handled_turn.visible_echo_event_id,
                pending_response_text=response_text,
                source_event_prompts=handled_turn.source_event_prompts,
                response_owner=handled_turn.response_owner,
                history_scope=handled_turn.history_scope,
                conversation_target=handled_turn.conversation_target,
                anchor_event_id=handled_turn.anchor_event_id,
            )
            self._save_responses_locked()
        logger.debug(
            "pending_command_reply_recorded",
            agent=self.agent_name,
            source_event_count=len(normalized_source_event_ids),
        )

    def reserve_visible_echo_transaction_id(self, handled_turn: HandledTurnState) -> str:
        """Persist and return one stable outbound tx-id for a pending visible echo."""
        normalized_source_event_ids = handled_turn.source_event_ids
        if not normalized_source_event_ids:
            msg = "Cannot reserve a visible echo transaction id without source events"
            raise ValueError(msg)

        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            for event_id in normalized_source_event_ids:
                existing_transaction_id = _visible_echo_transaction_id_for_record(self._responses.get(event_id))
                if existing_transaction_id is not None:
                    return existing_transaction_id

            transaction_id = f"echo_{uuid4().hex}"
            self._persist_handled_turn_locked(
                source_event_ids=normalized_source_event_ids,
                response_event_id=handled_turn.response_event_id,
                response_transaction_id=None,
                visible_echo_transaction_id=transaction_id,
                completed=False,
                visible_echo_event_id=handled_turn.visible_echo_event_id,
                source_event_prompts=handled_turn.source_event_prompts,
                response_owner=handled_turn.response_owner,
                history_scope=handled_turn.history_scope,
                conversation_target=handled_turn.conversation_target,
                anchor_event_id=handled_turn.anchor_event_id,
            )
            self._save_responses_locked()
        logger.debug(
            "visible_echo_transaction_id_reserved",
            agent=self.agent_name,
            visible_echo_transaction_id=transaction_id,
            source_event_count=len(normalized_source_event_ids),
        )
        return transaction_id

    def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo without marking the turn terminally handled."""
        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()
            existing_record = self._responses.get(source_event_id)
            source_event_ids = _source_event_ids_for_record(source_event_id, existing_record)
            self._persist_handled_turn_locked(
                source_event_ids=source_event_ids,
                response_event_id=_response_event_id_for_record(existing_record),
                response_transaction_id=_response_transaction_id_for_record(existing_record),
                visible_echo_transaction_id=_visible_echo_transaction_id_for_record(existing_record),
                completed=_completed_for_record(existing_record),
                visible_echo_event_id=echo_event_id,
                source_event_prompts=_prompt_map_for_record(source_event_ids, existing_record),
                response_owner=_response_owner_for_record(existing_record),
                history_scope=_history_scope_for_record(existing_record),
                conversation_target=_conversation_target_for_record(existing_record),
                anchor_event_id=_anchor_event_id_for_record(source_event_ids, existing_record),
            )
            self._save_responses_locked()
        logger.debug(
            "visible_echo_tracked",
            agent=self.agent_name,
            event_id=source_event_id,
            visible_echo_event_id=echo_event_id,
        )

    def has_responded(self, event_id: str) -> bool:
        """Return whether the source event has a terminal recorded outcome."""
        with self._thread_lock, self._file_lock(exclusive=False):
            self._responses = self._read_responses_file_locked(repair_corrupt_file=False)
            record = self._responses.get(event_id)
            return bool(record and record.get("completed", True))

    def get_response_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked response event ID for one source event."""
        with self._thread_lock, self._file_lock(exclusive=False):
            self._responses = self._read_responses_file_locked(repair_corrupt_file=False)
            return _response_event_id_for_record(self._responses.get(source_event_id))

    def get_visible_echo_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo event ID for one source event."""
        with self._thread_lock, self._file_lock(exclusive=False):
            self._responses = self._read_responses_file_locked(repair_corrupt_file=False)
            return _visible_echo_event_id_for_record(self._responses.get(source_event_id))

    def visible_echo_event_id_for_sources(self, source_event_ids: typing.Sequence[str]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        normalized_source_event_ids = _normalize_source_event_ids(source_event_ids)
        if not normalized_source_event_ids:
            return None
        with self._thread_lock, self._file_lock(exclusive=False):
            self._responses = self._read_responses_file_locked(repair_corrupt_file=False)
            return self._visible_echo_for_sources(normalized_source_event_ids)

    def get_turn_record(self, source_event_id: str) -> HandledTurnRecord | None:
        """Return the handled-turn record for one source event."""
        with self._thread_lock, self._file_lock(exclusive=False):
            self._responses = self._read_responses_file_locked(repair_corrupt_file=False)
            record = self._responses.get(source_event_id)
            if record is None:
                return None
            source_event_ids = _source_event_ids_for_record(source_event_id, record)
            return HandledTurnRecord(
                anchor_event_id=_anchor_event_id_for_record(source_event_ids, record),
                source_event_ids=source_event_ids,
                response_event_id=_response_event_id_for_record(record),
                response_transaction_id=_response_transaction_id_for_record(record),
                visible_echo_transaction_id=_visible_echo_transaction_id_for_record(record),
                completed=_completed_for_record(record),
                visible_echo_event_id=_visible_echo_event_id_for_record(record),
                pending_response_text=_pending_response_text_for_record(record),
                source_event_prompts=_prompt_map_for_record(source_event_ids, record),
                response_owner=_response_owner_for_record(record),
                history_scope=_history_scope_for_record(record),
                conversation_target=_conversation_target_for_record(record),
                timestamp=record["timestamp"],
            )

    def _load_responses(self) -> None:
        """Load handled turns from disk."""
        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = self._read_responses_file_locked()

    def _save_responses_locked(self) -> None:
        """Persist handled turns while the thread and file locks are held."""
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.base_path,
                prefix=f"{self._responses_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = self.base_path / Path(temp_file.name).name
                json.dump(self._responses, temp_file, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            temp_path.replace(self._responses_file)
            self._fsync_base_path()
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _cleanup_old_events(self, max_events: int = 10000, max_age_days: int = 30) -> None:
        """Remove old handled-turn records based on age and count."""
        with self._thread_lock, self._file_lock(exclusive=True):
            self._responses = _cleaned_responses(
                self._read_responses_file_locked(),
                max_events=max_events,
                max_age_days=max_age_days,
            )
            self._save_responses_locked()
        logger.info(
            "handled_turn_cleanup_completed",
            agent=self.agent_name,
            kept_event_count=len(self._responses),
        )

    @contextmanager
    def _file_lock(self, *, exclusive: bool) -> typing.Iterator[None]:
        """Lock the ledger for cross-instance readers and writers."""
        with self._responses_lock_file.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_responses_file_locked(
        self,
        *,
        repair_corrupt_file: bool = True,
    ) -> dict[str, _SerializedHandledTurnRecord]:
        """Read and normalize persisted responses while the file lock is held."""
        if not self._responses_file.exists():
            return {}
        try:
            with self._responses_file.open(encoding="utf-8") as response_file:
                data = json.load(response_file)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if repair_corrupt_file:
                quarantined_file = self._quarantine_corrupt_responses_file_locked()
                logger.warning(
                    "Quarantined malformed handled-turn ledger file",
                    agent=self.agent_name,
                    responses_file=str(self._responses_file),
                    quarantined_file=str(quarantined_file or self._responses_file),
                )
            else:
                logger.warning(
                    "Detected malformed handled-turn ledger file during shared read",
                    agent=self.agent_name,
                    responses_file=str(self._responses_file),
                )
            return {}
        if not isinstance(data, dict):
            if repair_corrupt_file:
                quarantined_file = self._quarantine_corrupt_responses_file_locked()
                logger.warning(
                    "Quarantined structurally invalid handled-turn ledger file",
                    agent=self.agent_name,
                    responses_file=str(self._responses_file),
                    quarantined_file=str(quarantined_file or self._responses_file),
                    payload_type=type(data).__name__,
                )
            else:
                logger.warning(
                    "Detected structurally invalid handled-turn ledger file during shared read",
                    agent=self.agent_name,
                    responses_file=str(self._responses_file),
                    payload_type=type(data).__name__,
                )
            return {}
        normalized_records: dict[str, _SerializedHandledTurnRecord] = {}
        invalid_event_ids: list[str] = []
        for event_id, record in data.items():
            if not isinstance(event_id, str) or not isinstance(record, dict):
                invalid_event_ids.append(event_id if isinstance(event_id, str) else repr(event_id))
                continue
            normalized_records[event_id] = _normalize_serialized_record(event_id, record)

        if invalid_event_ids:
            if repair_corrupt_file:
                quarantined_file = self._quarantine_corrupt_responses_file_locked()
                logger.warning(
                    "Quarantined handled-turn ledger file with invalid event entries",
                    agent=self.agent_name,
                    responses_file=str(self._responses_file),
                    quarantined_file=str(quarantined_file or self._responses_file),
                    invalid_event_ids=invalid_event_ids,
                )
            else:
                logger.warning(
                    "Detected handled-turn ledger file with invalid event entries during shared read",
                    agent=self.agent_name,
                    responses_file=str(self._responses_file),
                    invalid_event_ids=invalid_event_ids,
                )
        return normalized_records

    def _quarantine_corrupt_responses_file_locked(self) -> Path | None:
        """Move a corrupt responses file aside while the file lock is held."""
        quarantined_file = self.base_path / f"{self._responses_file.name}.corrupt-{time.time_ns()}"
        try:
            self._responses_file.replace(quarantined_file)
        except FileNotFoundError:
            return None
        return quarantined_file

    def _fsync_base_path(self) -> None:
        """Flush the tracking directory so atomic replacements are durable."""
        base_dir_fd = os.open(self.base_path, os.O_RDONLY)
        try:
            os.fsync(base_dir_fd)
        finally:
            os.close(base_dir_fd)

    def _visible_echo_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo already tracked for one turn."""
        for event_id in source_event_ids:
            visible_echo_event_id = _visible_echo_event_id_for_record(self._responses.get(event_id))
            if visible_echo_event_id is not None:
                return visible_echo_event_id
        return None

    def _persist_handled_turn_locked(
        self,
        source_event_ids: tuple[str, ...],
        *,
        response_event_id: str | None,
        response_transaction_id: str | None,
        visible_echo_transaction_id: str | None,
        completed: bool,
        visible_echo_event_id: str | None,
        source_event_prompts: typing.Mapping[str, str] | None,
        response_owner: str | None,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget | None,
        pending_response_text: str | None = None,
        anchor_event_id: str | None = None,
    ) -> None:
        """Persist one handled turn while the thread and file locks are already held."""
        visible_echo_event_id = visible_echo_event_id or self._visible_echo_for_sources(source_event_ids)
        response_transaction_id = self._normalized_response_transaction_id(source_event_ids, response_transaction_id)
        visible_echo_transaction_id = self._normalized_visible_echo_transaction_id(
            source_event_ids,
            visible_echo_transaction_id,
        )
        prompt_map = self._normalized_prompt_map(source_event_ids, source_event_prompts)
        response_owner = self._normalized_response_owner(source_event_ids, response_owner)
        history_scope = self._normalized_history_scope(source_event_ids, history_scope)
        conversation_target = self._normalized_conversation_target(source_event_ids, conversation_target)
        anchor_event_id = self._normalized_anchor_event_id(source_event_ids, anchor_event_id)
        timestamp = time.time()
        for event_id in source_event_ids:
            self._responses[event_id] = _serialized_record(
                timestamp=timestamp,
                response_event_id=response_event_id,
                response_transaction_id=response_transaction_id,
                visible_echo_transaction_id=visible_echo_transaction_id,
                completed=completed,
                anchor_event_id=anchor_event_id,
                source_event_ids=source_event_ids,
                visible_echo_event_id=visible_echo_event_id,
                pending_response_text=pending_response_text,
                source_event_prompts=prompt_map,
                response_owner=response_owner,
                history_scope=history_scope,
                conversation_target=conversation_target,
            )

    def _normalized_prompt_map(
        self,
        source_event_ids: tuple[str, ...],
        source_event_prompts: typing.Mapping[str, str] | None,
    ) -> dict[str, str] | None:
        """Return the explicit prompt map or preserve an existing one."""
        if normalized_prompt_map := _explicit_prompt_map_for_sources(source_event_ids, source_event_prompts):
            return normalized_prompt_map
        for event_id in source_event_ids:
            existing_prompt_map = _prompt_map_for_record(source_event_ids, self._responses.get(event_id))
            if existing_prompt_map is not None:
                return existing_prompt_map
        return None

    def _normalized_response_transaction_id(
        self,
        source_event_ids: tuple[str, ...],
        response_transaction_id: str | None,
    ) -> str | None:
        """Return the explicit response transaction id or preserve an existing one."""
        normalized_response_transaction_id = _normalized_transaction_id(response_transaction_id)
        if normalized_response_transaction_id is not None:
            return normalized_response_transaction_id
        for event_id in source_event_ids:
            existing_response_transaction_id = _response_transaction_id_for_record(self._responses.get(event_id))
            if existing_response_transaction_id is not None:
                return existing_response_transaction_id
        return None

    def _normalized_visible_echo_transaction_id(
        self,
        source_event_ids: tuple[str, ...],
        visible_echo_transaction_id: str | None,
    ) -> str | None:
        """Return the explicit visible-echo tx-id or preserve an existing one."""
        normalized_visible_echo_transaction_id = _normalized_transaction_id(visible_echo_transaction_id)
        if normalized_visible_echo_transaction_id is not None:
            return normalized_visible_echo_transaction_id
        for event_id in source_event_ids:
            existing_visible_echo_transaction_id = _visible_echo_transaction_id_for_record(
                self._responses.get(event_id),
            )
            if existing_visible_echo_transaction_id is not None:
                return existing_visible_echo_transaction_id
        return None

    def _normalized_response_owner(
        self,
        source_event_ids: tuple[str, ...],
        response_owner: str | None,
    ) -> str | None:
        """Return the explicit response owner or preserve an existing one."""
        normalized_response_owner = _normalized_response_owner(response_owner)
        if normalized_response_owner is not None:
            return normalized_response_owner
        for event_id in source_event_ids:
            existing_response_owner = _response_owner_for_record(self._responses.get(event_id))
            if existing_response_owner is not None:
                return existing_response_owner
        return None

    def _normalized_history_scope(
        self,
        source_event_ids: tuple[str, ...],
        history_scope: HistoryScope | None,
    ) -> HistoryScope | None:
        """Return the explicit history scope or preserve an existing one."""
        normalized_history_scope = _normalized_history_scope(history_scope)
        if normalized_history_scope is not None:
            return normalized_history_scope
        for event_id in source_event_ids:
            existing_history_scope = _history_scope_for_record(self._responses.get(event_id))
            if existing_history_scope is not None:
                return existing_history_scope
        return None

    def _normalized_conversation_target(
        self,
        source_event_ids: tuple[str, ...],
        conversation_target: MessageTarget | None,
    ) -> MessageTarget | None:
        """Return the explicit conversation target or preserve an existing one."""
        normalized_conversation_target = _normalized_conversation_target(conversation_target)
        if normalized_conversation_target is not None:
            return normalized_conversation_target
        for event_id in source_event_ids:
            existing_conversation_target = _conversation_target_for_record(self._responses.get(event_id))
            if existing_conversation_target is not None:
                return existing_conversation_target
        return None

    def _normalized_anchor_event_id(
        self,
        source_event_ids: tuple[str, ...],
        anchor_event_id: str | None,
    ) -> str:
        """Return the explicit anchor event ID or preserve an existing one."""
        normalized_anchor_event_id = _normalized_event_id(anchor_event_id)
        if normalized_anchor_event_id is not None:
            return normalized_anchor_event_id
        for event_id in source_event_ids:
            existing_record = self._responses.get(event_id)
            if existing_record is not None:
                return _anchor_event_id_for_record(source_event_ids, existing_record)
        return source_event_ids[-1]


def _normalize_source_event_ids(source_event_ids: typing.Sequence[str]) -> tuple[str, ...]:
    """Deduplicate source event IDs while preserving order."""
    normalized_event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    for event_id in source_event_ids:
        if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        normalized_event_ids.append(event_id)
    return tuple(normalized_event_ids)


def _normalized_event_id(event_id: str | None) -> str | None:
    """Return a non-empty Matrix event ID or None."""
    return event_id if isinstance(event_id, str) and event_id else None


def _normalized_transaction_id(transaction_id: str | None) -> str | None:
    """Return a non-empty Matrix transaction id or None."""
    return transaction_id if isinstance(transaction_id, str) and transaction_id else None


def _normalized_response_owner(response_owner: str | None) -> str | None:
    """Return a non-empty response owner or None."""
    return response_owner if isinstance(response_owner, str) and response_owner else None


def _normalized_history_scope(history_scope: HistoryScope | None) -> HistoryScope | None:
    """Return one normalized persisted history scope."""
    if not isinstance(history_scope, HistoryScope):
        return None
    if history_scope.kind not in {"agent", "team"}:
        return None
    if not isinstance(history_scope.scope_id, str) or not history_scope.scope_id:
        return None
    return HistoryScope(kind=history_scope.kind, scope_id=history_scope.scope_id)


def _normalized_conversation_target(conversation_target: MessageTarget | None) -> MessageTarget | None:
    """Return one normalized persisted conversation target."""
    if not isinstance(conversation_target, MessageTarget):
        return None
    if not isinstance(conversation_target.room_id, str) or not conversation_target.room_id:
        return None
    session_id = (
        conversation_target.session_id
        if isinstance(conversation_target.session_id, str) and conversation_target.session_id
        else MessageTarget._build_session_id(
            conversation_target.room_id,
            conversation_target.resolved_thread_id,
        )
    )
    return MessageTarget(
        room_id=conversation_target.room_id,
        source_thread_id=_normalized_event_id(conversation_target.source_thread_id),
        resolved_thread_id=_normalized_event_id(conversation_target.resolved_thread_id),
        reply_to_event_id=_normalized_event_id(conversation_target.reply_to_event_id),
        session_id=session_id,
    )


def _explicit_prompt_map_for_sources(
    source_event_ids: tuple[str, ...],
    source_event_prompts: typing.Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Return only prompt entries that match the tracked source event IDs."""
    if not source_event_prompts:
        return None
    normalized_prompt_map = {
        event_id: prompt
        for event_id in source_event_ids
        if isinstance((prompt := source_event_prompts.get(event_id)), str)
    }
    return normalized_prompt_map or None


def _serialized_record(
    *,
    timestamp: float,
    response_event_id: str | None,
    response_transaction_id: str | None,
    visible_echo_transaction_id: str | None,
    completed: bool,
    anchor_event_id: str | None,
    source_event_ids: tuple[str, ...],
    visible_echo_event_id: str | None = None,
    pending_response_text: str | None = None,
    source_event_prompts: typing.Mapping[str, str] | None = None,
    response_owner: str | None = None,
    history_scope: HistoryScope | None = None,
    conversation_target: MessageTarget | None = None,
) -> _SerializedHandledTurnRecord:
    """Build one persisted handled-turn record from normalized fields."""
    record: _SerializedHandledTurnRecord = {
        "timestamp": timestamp,
        "response_event_id": response_event_id,
        "completed": completed,
        "source_event_ids": list(source_event_ids),
    }
    if anchor_event_id is not None and anchor_event_id != source_event_ids[-1]:
        record["anchor_event_id"] = anchor_event_id
    if response_transaction_id is not None:
        record["response_transaction_id"] = response_transaction_id
    if visible_echo_transaction_id is not None:
        record["visible_echo_transaction_id"] = visible_echo_transaction_id
    if visible_echo_event_id is not None:
        record["visible_echo_event_id"] = visible_echo_event_id
    if pending_response_text is not None:
        record["pending_response_text"] = pending_response_text
    if source_event_prompts is not None:
        record["source_event_prompts"] = dict(source_event_prompts)
    if response_owner is not None:
        record["response_owner"] = response_owner
    if history_scope is not None:
        record["history_scope"] = {
            "kind": history_scope.kind,
            "scope_id": history_scope.scope_id,
        }
    if conversation_target is not None:
        record["conversation_target"] = {
            "room_id": conversation_target.room_id,
            "source_thread_id": conversation_target.source_thread_id,
            "resolved_thread_id": conversation_target.resolved_thread_id,
            "reply_to_event_id": conversation_target.reply_to_event_id,
            "session_id": conversation_target.session_id,
        }
    return record


def _responses_file_path(base_path: Path, agent_name: str) -> Path:
    """Return the validated ledger path for one agent."""
    if not agent_name or ".." in agent_name or "/" in agent_name or "\\" in agent_name:
        message = f"Invalid handled-turn ledger agent name: {agent_name!r}"
        raise ValueError(message)
    responses_file = base_path / f"{agent_name}_responded.json"
    if responses_file.resolve().parent != base_path.resolve():
        message = f"Invalid handled-turn ledger path for agent: {agent_name!r}"
        raise ValueError(message)
    return responses_file


def _cleaned_responses(
    responses: dict[str, _SerializedHandledTurnRecord],
    *,
    max_events: int,
    max_age_days: int,
) -> dict[str, _SerializedHandledTurnRecord]:
    """Remove stale turn groups while keeping coalesced groups intact."""
    current_time = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    response_groups = _response_groups(responses)
    fresh_groups = [group for group in response_groups if current_time - group.timestamp < max_age_seconds]
    if len(fresh_groups) > max_events:
        fresh_groups = fresh_groups[-max_events:]
    cleaned_responses: dict[str, _SerializedHandledTurnRecord] = {}
    for group in fresh_groups:
        cleaned_responses.update(group.records)
    return cleaned_responses


@dataclass(frozen=True)
class _ResponseGroup:
    """Logical handled-turn group keyed by coalesced source IDs."""

    source_event_ids: tuple[str, ...]
    timestamp: float
    records: dict[str, _SerializedHandledTurnRecord]


def _response_groups(
    responses: dict[str, _SerializedHandledTurnRecord],
) -> list[_ResponseGroup]:
    """Return handled turns grouped by shared source-event identity."""
    grouped_records: dict[tuple[str, ...], dict[str, _SerializedHandledTurnRecord]] = {}
    grouped_timestamps: dict[tuple[str, ...], float] = {}
    for event_id, record in responses.items():
        source_event_ids = _source_event_ids_for_record(event_id, record)
        grouped_records.setdefault(source_event_ids, {})[event_id] = record
        grouped_timestamps[source_event_ids] = max(grouped_timestamps.get(source_event_ids, 0.0), record["timestamp"])
    return sorted(
        (
            _ResponseGroup(
                source_event_ids=source_event_ids,
                timestamp=grouped_timestamps[source_event_ids],
                records=records,
            )
            for source_event_ids, records in grouped_records.items()
        ),
        key=lambda group: group.timestamp,
    )


def _normalize_serialized_record(
    event_id: str,
    raw_record: _SerializedHandledTurnRecordLike,
) -> _SerializedHandledTurnRecord:
    """Normalize old and new on-disk record shapes into one schema."""
    response_event_id = raw_record.get("response_event_id")
    if not isinstance(response_event_id, str):
        response_event_id = raw_record.get("response_id")
    visible_echo_event_id = raw_record.get("visible_echo_event_id")
    if not isinstance(visible_echo_event_id, str):
        visible_echo_event_id = raw_record.get("visible_echo_response_id")
    response_transaction_id = _normalized_transaction_id(raw_record.get("response_transaction_id"))
    visible_echo_transaction_id = _normalized_transaction_id(raw_record.get("visible_echo_transaction_id"))
    timestamp = raw_record.get("timestamp")
    raw_source_event_ids = raw_record.get("source_event_ids")
    normalized_source_event_ids = (
        _normalize_source_event_ids(raw_source_event_ids)
        if isinstance(
            raw_source_event_ids,
            list,
        )
        else (event_id,)
    )
    if not normalized_source_event_ids:
        normalized_source_event_ids = (event_id,)
    anchor_event_id = _normalized_event_id(raw_record.get("anchor_event_id"))
    prompt_map = _prompt_map_for_record(normalized_source_event_ids, raw_record)
    response_owner = _response_owner_for_record(raw_record)
    history_scope = _history_scope_for_record(raw_record)
    conversation_target = _conversation_target_for_record(raw_record)
    normalized_record: _SerializedHandledTurnRecord = {
        "timestamp": float(timestamp) if isinstance(timestamp, int | float) else 0.0,
        "response_event_id": response_event_id if isinstance(response_event_id, str) else None,
        "completed": bool(raw_record.get("completed", True)),
        "source_event_ids": list(normalized_source_event_ids),
    }
    return _with_optional_serialized_record_fields(
        normalized_record,
        anchor_event_id=anchor_event_id if anchor_event_id != normalized_source_event_ids[-1] else None,
        response_transaction_id=response_transaction_id,
        visible_echo_transaction_id=visible_echo_transaction_id,
        visible_echo_event_id=visible_echo_event_id if isinstance(visible_echo_event_id, str) else None,
        pending_response_text=_pending_response_text_for_record(raw_record),
        prompt_map=prompt_map,
        response_owner=response_owner,
        history_scope=history_scope,
        conversation_target=conversation_target,
    )


def _with_optional_serialized_record_fields(
    record: _SerializedHandledTurnRecord,
    *,
    anchor_event_id: str | None,
    response_transaction_id: str | None,
    visible_echo_transaction_id: str | None,
    visible_echo_event_id: str | None,
    pending_response_text: str | None,
    prompt_map: dict[str, str] | None,
    response_owner: str | None,
    history_scope: HistoryScope | None,
    conversation_target: MessageTarget | None,
) -> _SerializedHandledTurnRecord:
    """Attach optional normalized fields to one serialized handled-turn record."""
    if anchor_event_id is not None:
        record["anchor_event_id"] = anchor_event_id
    if response_transaction_id is not None:
        record["response_transaction_id"] = response_transaction_id
    if visible_echo_transaction_id is not None:
        record["visible_echo_transaction_id"] = visible_echo_transaction_id
    if visible_echo_event_id is not None:
        record["visible_echo_event_id"] = visible_echo_event_id
    if pending_response_text is not None:
        record["pending_response_text"] = pending_response_text
    if prompt_map is not None:
        record["source_event_prompts"] = prompt_map
    if response_owner is not None:
        record["response_owner"] = response_owner
    if history_scope is not None:
        record["history_scope"] = {
            "kind": history_scope.kind,
            "scope_id": history_scope.scope_id,
        }
    if conversation_target is not None:
        record["conversation_target"] = {
            "room_id": conversation_target.room_id,
            "source_thread_id": conversation_target.source_thread_id,
            "resolved_thread_id": conversation_target.resolved_thread_id,
            "reply_to_event_id": conversation_target.reply_to_event_id,
            "session_id": conversation_target.session_id,
        }
    return record


def _source_event_ids_for_record(
    event_id: str,
    record: _SerializedHandledTurnRecordLike | None,
) -> tuple[str, ...]:
    """Return the normalized source event IDs for one record."""
    if record is None:
        return (event_id,)
    raw_source_event_ids = record.get("source_event_ids")
    if isinstance(raw_source_event_ids, list):
        normalized_source_event_ids = _normalize_source_event_ids(raw_source_event_ids)
        if normalized_source_event_ids:
            return normalized_source_event_ids
    return (event_id,)


def _prompt_map_for_record(
    source_event_ids: tuple[str, ...],
    record: _SerializedHandledTurnRecordLike | None,
) -> dict[str, str] | None:
    """Return the prompt map for one record if present."""
    if record is None:
        return None
    raw_prompt_map = record.get("source_event_prompts")
    if not isinstance(raw_prompt_map, dict):
        return None
    normalized_prompt_map = {
        event_id: prompt for event_id in source_event_ids if isinstance((prompt := raw_prompt_map.get(event_id)), str)
    }
    return normalized_prompt_map or None


def _anchor_event_id_for_record(
    source_event_ids: tuple[str, ...],
    record: _SerializedHandledTurnRecordLike | None,
) -> str:
    """Return the normalized anchor event ID for one record."""
    if record is None:
        return source_event_ids[-1]
    anchor_event_id = _normalized_event_id(record.get("anchor_event_id"))
    return anchor_event_id if anchor_event_id is not None else source_event_ids[-1]


def _response_owner_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized response owner for one record."""
    if record is None:
        return None
    return _normalized_response_owner(record.get("response_owner"))


def _history_scope_for_record(record: _SerializedHandledTurnRecordLike | None) -> HistoryScope | None:
    """Return the normalized history scope for one record."""
    if record is None:
        return None
    raw_history_scope = record.get("history_scope")
    if not isinstance(raw_history_scope, dict):
        return None
    kind = raw_history_scope.get("kind")
    scope_id = raw_history_scope.get("scope_id")
    if kind not in {"agent", "team"} or not isinstance(scope_id, str) or not scope_id:
        return None
    return HistoryScope(kind=kind, scope_id=scope_id)


def _conversation_target_for_record(record: _SerializedHandledTurnRecordLike | None) -> MessageTarget | None:
    """Return the normalized conversation target for one record."""
    if record is None:
        return None
    raw_target = record.get("conversation_target")
    if not isinstance(raw_target, dict):
        return None
    room_id = raw_target.get("room_id")
    session_id = raw_target.get("session_id")
    if not isinstance(room_id, str) or not room_id:
        return None
    resolved_thread_id = _normalized_event_id(raw_target.get("resolved_thread_id"))
    normalized_target = MessageTarget(
        room_id=room_id,
        source_thread_id=_normalized_event_id(raw_target.get("source_thread_id") or raw_target.get("thread_id")),
        resolved_thread_id=resolved_thread_id,
        reply_to_event_id=_normalized_event_id(raw_target.get("reply_to_event_id")),
        session_id=(
            session_id
            if isinstance(session_id, str) and session_id
            else MessageTarget._build_session_id(room_id, resolved_thread_id)
        ),
    )
    return _normalized_conversation_target(normalized_target)


def _response_event_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized response event ID for one record."""
    if record is None:
        return None
    response_event_id = record.get("response_event_id")
    if isinstance(response_event_id, str):
        return response_event_id
    legacy_response_id = record.get("response_id")
    return legacy_response_id if isinstance(legacy_response_id, str) else None


def _response_transaction_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized response transaction id for one record."""
    if record is None:
        return None
    return _normalized_transaction_id(record.get("response_transaction_id"))


def _visible_echo_transaction_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized visible-echo transaction id for one record."""
    if record is None:
        return None
    return _normalized_transaction_id(record.get("visible_echo_transaction_id"))


def _visible_echo_event_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized visible echo event ID for one record."""
    if record is None:
        return None
    visible_echo_event_id = record.get("visible_echo_event_id")
    if isinstance(visible_echo_event_id, str):
        return visible_echo_event_id
    legacy_visible_echo_id = record.get("visible_echo_response_id")
    return legacy_visible_echo_id if isinstance(legacy_visible_echo_id, str) else None


def _pending_response_text_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the persisted pending response text for one record."""
    if record is None:
        return None
    pending_response_text = record.get("pending_response_text")
    return pending_response_text if isinstance(pending_response_text, str) else None


def _completed_for_record(record: _SerializedHandledTurnRecordLike | None) -> bool:
    """Return the normalized terminal-completion flag for one record."""
    return bool(record.get("completed", True)) if record is not None else False
