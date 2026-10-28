"""Durable replay tracking for inbound deliveries, one scope per file."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypedDict, TypeGuard, cast

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

# Every claim rewrites its scope's replay file, so one scope must not grow it without bound;
# the limit applies separately to its nonces, event ids, and thread keys.
_MAX_LIVE_CLAIMS_PER_SCOPE = 10_000


class DeliveryEventClaim(StrEnum):
    """State returned when claiming a delivery event id."""

    FRESH = "fresh"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"


class DeliveryThreadKeyClaim(StrEnum):
    """State returned when claiming a delivery thread key."""

    FRESH = "fresh"
    PENDING = "pending"
    BOUND = "bound"


class DeliveryReplayStoreError(RuntimeError):
    """Raised when durable replay state cannot be trusted."""


class DeliveryReplayScopeFullError(RuntimeError):
    """Raised when one replay scope already holds the maximum number of live claims."""


class _SerializedNonce(TypedDict):
    expires_at: int


class _SerializedEvent(TypedDict):
    state: Literal["in_progress", "delivered"]
    expires_at: int


class _SerializedThread(TypedDict):
    room_id: str
    thread_event_id: str | None
    reservation: str | None
    expires_at: int


class _SerializedReplayStore(TypedDict):
    nonces: dict[str, _SerializedNonce]
    events: dict[str, _SerializedEvent]
    threads: dict[str, _SerializedThread]


@dataclass
class DeliveryReplayStore:
    """JSON-backed replay store keeping each replay scope in its own file under its own lock."""

    root: Path
    legacy_shared_file: Path | None = None

    def claim_nonce(self, replay_scope: str, nonce: str, *, now: int, ttl_seconds: int) -> bool:
        """Return True only for the first unexpired nonce claim."""
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            _prune_expired(store, now=now)
            replay_nonces = store["nonces"]
            if nonce in replay_nonces:
                return False
            _require_room_for_claim(replay_nonces)
            replay_nonces[nonce] = {"expires_at": now + ttl_seconds}
            self._write_store(path, store)
            return True

    def claim_event_id(
        self,
        replay_scope: str,
        event_id: str,
        *,
        now: int,
        ttl_seconds: int,
    ) -> DeliveryEventClaim:
        """Claim one delivery event id and return its replay state."""
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            _prune_expired(store, now=now)
            replay_events = store["events"]
            event = replay_events.get(event_id)
            if event is not None:
                if event["state"] == "delivered":
                    return DeliveryEventClaim.DELIVERED
                return DeliveryEventClaim.IN_PROGRESS
            _require_room_for_claim(replay_events)
            replay_events[event_id] = {
                "state": DeliveryEventClaim.IN_PROGRESS.value,
                "expires_at": now + ttl_seconds,
            }
            self._write_store(path, store)
            return DeliveryEventClaim.FRESH

    def mark_event_delivered(self, replay_scope: str, event_id: str, *, now: int, ttl_seconds: int) -> None:
        """Record that one delivery event id reached Matrix delivery."""
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            _prune_expired(store, now=now)
            replay_events = store["events"]
            replay_events[event_id] = {
                "state": DeliveryEventClaim.DELIVERED.value,
                "expires_at": now + ttl_seconds,
            }
            self._write_store(path, store)

    def claim_thread_key(
        self,
        replay_scope: str,
        thread_key: str,
        *,
        room_id: str,
        now: int,
        pending_ttl_seconds: int,
    ) -> tuple[DeliveryThreadKeyClaim, str | None, str | None]:
        """Atomically resolve or reserve one thread key.

        Returns ``(BOUND, root, None)`` when an earlier delivery already opened
        the thread in this room, ``(PENDING, None, None)`` when another delivery
        is opening it right now, and ``(FRESH, None, reservation)`` after
        reserving the key for the caller. The reservation token fences the
        follow-up ``bind_thread_root`` and ``release_thread_key`` calls so a
        delivery that outlived its lease cannot disturb a newer owner. A record
        bound to another room counts as absent: the scope's configured room
        may have been re-pointed since it was made.
        """
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            _prune_expired(store, now=now)
            replay_threads = store["threads"]
            record = replay_threads.get(thread_key)
            if record is not None and record["room_id"] == room_id:
                if record["thread_event_id"] is not None:
                    return DeliveryThreadKeyClaim.BOUND, record["thread_event_id"], None
                return DeliveryThreadKeyClaim.PENDING, None, None
            if record is None:
                _require_room_for_claim(replay_threads)
            reservation = secrets.token_hex(16)
            replay_threads[thread_key] = {
                "room_id": room_id,
                "thread_event_id": None,
                "reservation": reservation,
                "expires_at": now + pending_ttl_seconds,
            }
            self._write_store(path, store)
            return DeliveryThreadKeyClaim.FRESH, None, reservation

    def bind_thread_root(
        self,
        replay_scope: str,
        thread_key: str,
        thread_event_id: str,
        *,
        room_id: str,
        reservation: str | None,
        now: int,
        ttl_seconds: int,
    ) -> str | None:
        """Bind one thread key to its Matrix thread root, refreshing retention.

        Returns the root the key is bound to afterwards, or ``None`` when the
        caller lost its claim: another delivery reserved the key after the
        caller's lease expired and has not finished yet, or the expired key no
        longer fits in its full scope. A root already bound in the same room is
        kept and returned, whoever calls.
        """
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            _prune_expired(store, now=now)
            replay_threads = store["threads"]
            record = replay_threads.get(thread_key)
            if record is not None and record["room_id"] == room_id:
                if record["thread_event_id"] is not None:
                    thread_event_id = record["thread_event_id"]
                elif record["reservation"] != reservation:
                    return None
            elif record is not None:
                # The key now belongs to a delivery for another room (the scope's
                # room was re-pointed mid-flight); leave that record alone.
                return None
            elif len(replay_threads) >= _MAX_LIVE_CLAIMS_PER_SCOPE:
                # The message is already posted, so refusing here would only stop
                # the caller from marking its event delivered.
                return None
            replay_threads[thread_key] = {
                "room_id": room_id,
                "thread_event_id": thread_event_id,
                "reservation": None,
                "expires_at": now + ttl_seconds,
            }
            self._write_store(path, store)
            return thread_event_id

    def release_thread_key(self, replay_scope: str, thread_key: str, *, reservation: str) -> None:
        """Drop the caller's own pending reservation after its first delivery failed.

        Bound keys and reservations owned by another delivery are left alone.
        """
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            record = store["threads"].get(thread_key)
            if record is None or record["thread_event_id"] is not None or record["reservation"] != reservation:
                return
            store["threads"].pop(thread_key)
            self._write_store(path, store)

    def release_event_id(self, replay_scope: str, event_id: str) -> None:
        """Remove an event id claim after delivery failure."""
        with self._scope_lock(replay_scope) as path:
            store = self._read_store(path)
            if store["events"].pop(event_id, None) is not None:
                self._write_store(path, store)

    def retain_scopes(self, live_scopes: Iterable[str]) -> None:
        """Delete the replay records of every scope outside ``live_scopes``.

        A scope stops authenticating deliveries once its owner (an external trigger, say) is consumed,
        deleted, or re-keyed, and a delivery refuses every claim it made in a
        scope that is no longer current, so the scope's records and lock protect
        nothing and must not keep occupying storage outside the owner's limits.
        """
        live_file_stems = {_scope_file_stem(scope) for scope in live_scopes}
        if not self.root.is_dir():
            return
        for path in self.root.iterdir():
            # Lock and temporary files share their scope file's stem.
            if path.name.partition(".")[0] not in live_file_stems:
                path.unlink(missing_ok=True)

    @contextmanager
    def _scope_lock(self, replay_scope: str) -> Iterator[Path]:
        """Hold one scope's lock and yield its file, so no call reads another scope's records."""
        self._split_legacy_store()
        path = self._scope_path(replay_scope)
        with advisory_file_lock(path.with_name(f"{path.name}.lock")):
            yield path

    def _scope_path(self, replay_scope: str) -> Path:
        # Hashing makes every scope one fixed-length name that is valid on every filesystem.
        return self.root / f"{_scope_file_stem(replay_scope)}.json"

    def _split_legacy_store(self) -> None:
        legacy_path = self.legacy_shared_file
        if legacy_path is None or not legacy_path.exists():
            return
        # Deferred because importing any external_triggers module runs its package, which imports this module.
        from mindroom.external_triggers.legacy_replay_store import split_shared_replay_store  # noqa: PLC0415

        legacy_lock_path = legacy_path.with_name(f"{legacy_path.name}.lock")
        with advisory_file_lock(legacy_lock_path):
            try:
                raw_store = _read_json(legacy_path)
            except FileNotFoundError:
                return
            scope_sections = split_shared_replay_store(raw_store)
            if scope_sections is None:
                raise _invalid_store_structure()
            scope_stores = {scope: _normalize_store(sections) for scope, sections in scope_sections.items()}
            for scope, store in scope_stores.items():
                self._write_store(self._scope_path(scope), store)
            legacy_path.unlink()
            legacy_lock_path.unlink(missing_ok=True)

    def _read_store(self, path: Path) -> _SerializedReplayStore:
        try:
            raw_store = _read_json(path)
        except FileNotFoundError:
            return _empty_store()
        return _normalize_store(raw_store)

    def _write_store(self, path: Path, store: _SerializedReplayStore) -> None:
        try:
            write_json_file_durable(path, store, indent=2, sort_keys=True)
        except OSError as exc:
            msg = "delivery replay store is unavailable"
            raise DeliveryReplayStoreError(msg) from exc


def _scope_file_stem(replay_scope: str) -> str:
    return hashlib.sha256(replay_scope.encode()).hexdigest()


def _read_json(path: Path) -> object:
    """Return the file's parsed JSON, raising FileNotFoundError so a missing file is never mistaken for JSON null."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        msg = "delivery replay store is unavailable"
        raise DeliveryReplayStoreError(msg) from exc
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        msg = "invalid delivery replay store JSON"
        raise DeliveryReplayStoreError(msg) from exc


def _require_room_for_claim(scope_claims: Mapping[str, object]) -> None:
    if len(scope_claims) >= _MAX_LIVE_CLAIMS_PER_SCOPE:
        msg = "delivery replay scope is full"
        raise DeliveryReplayScopeFullError(msg)


def _empty_store() -> _SerializedReplayStore:
    return {"nonces": {}, "events": {}, "threads": {}}


def _normalize_store(raw_store: object) -> _SerializedReplayStore:
    if not isinstance(raw_store, Mapping):
        raise _invalid_store_structure()
    store_mapping = cast("Mapping[object, object]", raw_store)
    raw_nonces = store_mapping.get("nonces")
    raw_events = store_mapping.get("events")
    raw_threads = store_mapping.get("threads")
    if (
        not isinstance(raw_nonces, Mapping)
        or not isinstance(raw_events, Mapping)
        or not isinstance(raw_threads, Mapping)
    ):
        raise _invalid_store_structure()
    return {
        "nonces": _normalize_nonces(cast("Mapping[object, object]", raw_nonces)),
        "events": _normalize_events(cast("Mapping[object, object]", raw_events)),
        "threads": _normalize_threads(cast("Mapping[object, object]", raw_threads)),
    }


def _invalid_store_structure() -> DeliveryReplayStoreError:
    return DeliveryReplayStoreError("invalid delivery replay store structure")


def _is_json_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_nonces(raw_nonces: Mapping[object, object]) -> dict[str, _SerializedNonce]:
    nonces: dict[str, _SerializedNonce] = {}
    for nonce, record in raw_nonces.items():
        if not isinstance(nonce, str) or not isinstance(record, Mapping):
            raise _invalid_store_structure()
        expires_at = cast("Mapping[object, object]", record).get("expires_at")
        if not _is_json_int(expires_at):
            raise _invalid_store_structure()
        nonces[nonce] = {"expires_at": expires_at}
    return nonces


def _normalize_events(raw_events: Mapping[object, object]) -> dict[str, _SerializedEvent]:
    events: dict[str, _SerializedEvent] = {}
    for event_id, record in raw_events.items():
        if not isinstance(event_id, str) or not isinstance(record, Mapping):
            raise _invalid_store_structure()
        record_mapping = cast("Mapping[object, object]", record)
        state = record_mapping.get("state")
        expires_at = record_mapping.get("expires_at")
        if state not in {"in_progress", "delivered"} or not _is_json_int(expires_at):
            raise _invalid_store_structure()
        events[event_id] = {
            "state": cast("Literal['in_progress', 'delivered']", state),
            "expires_at": expires_at,
        }
    return events


def _normalize_threads(raw_threads: Mapping[object, object]) -> dict[str, _SerializedThread]:
    threads: dict[str, _SerializedThread] = {}
    for thread_key, record in raw_threads.items():
        if not isinstance(thread_key, str) or not isinstance(record, Mapping):
            raise _invalid_store_structure()
        record_mapping = cast("Mapping[object, object]", record)
        if "thread_event_id" not in record_mapping or "reservation" not in record_mapping:
            raise _invalid_store_structure()
        room_id = record_mapping.get("room_id")
        thread_event_id = record_mapping["thread_event_id"]
        reservation = record_mapping["reservation"]
        expires_at = record_mapping.get("expires_at")
        if not isinstance(room_id, str) or not room_id or not _is_json_int(expires_at):
            raise _invalid_store_structure()
        is_bound = isinstance(thread_event_id, str) and bool(thread_event_id) and reservation is None
        is_pending = thread_event_id is None and isinstance(reservation, str) and bool(reservation)
        if not (is_bound or is_pending):
            raise _invalid_store_structure()
        threads[thread_key] = {
            "room_id": room_id,
            "thread_event_id": cast("str | None", thread_event_id),
            "reservation": cast("str | None", reservation),
            "expires_at": expires_at,
        }
    return threads


def _prune_expired(store: _SerializedReplayStore, *, now: int) -> None:
    store["nonces"] = {nonce: record for nonce, record in store["nonces"].items() if record["expires_at"] >= now}
    store["events"] = {event_id: record for event_id, record in store["events"].items() if record["expires_at"] >= now}
    store["threads"] = {
        thread_key: record for thread_key, record in store["threads"].items() if record["expires_at"] >= now
    }
