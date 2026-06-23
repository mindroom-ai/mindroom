"""Durable replay tracking for external triggers."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict, TypeGuard, cast


class ExternalTriggerEventClaim(StrEnum):
    """State returned when claiming an external trigger event id."""

    FRESH = "fresh"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"


class ExternalTriggerReplayStoreError(RuntimeError):
    """Raised when durable replay state cannot be trusted."""


class _SerializedNonce(TypedDict):
    expires_at: int


class _SerializedEvent(TypedDict):
    state: Literal["in_progress", "delivered"]
    expires_at: int
    delivered_at: int | None


class _SerializedReplayStore(TypedDict):
    nonces: dict[str, dict[str, _SerializedNonce]]
    events: dict[str, dict[str, _SerializedEvent]]


@dataclass
class _ReplayStoreState:
    lock: threading.RLock = field(default_factory=threading.RLock)


_STORE_STATES: dict[str, _ReplayStoreState] = {}
_STORE_STATES_LOCK = threading.Lock()


def _shared_store_state(store_path: Path) -> _ReplayStoreState:
    key = str(store_path.absolute())
    with _STORE_STATES_LOCK:
        state = _STORE_STATES.get(key)
        if state is None:
            state = _ReplayStoreState()
            _STORE_STATES[key] = state
        return state


@dataclass
class ExternalTriggerReplayStore:
    """JSON-backed replay store for external trigger nonces and event ids."""

    tracking_root: Path
    _store_path: Path = field(init=False)
    _state: _ReplayStoreState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind this store to its path-wide process lock."""
        self._store_path = self.tracking_root / "external_triggers.json"
        self._state = _shared_store_state(self._store_path)

    def claim_nonce(self, trigger_id: str, nonce: str, *, now: int, ttl_seconds: int) -> bool:
        """Return True only for the first unexpired nonce claim."""
        with self._state.lock:
            store = self._read_store()
            _prune_expired(store, now=now)
            trigger_nonces = store["nonces"].setdefault(trigger_id, {})
            if nonce in trigger_nonces:
                return False
            trigger_nonces[nonce] = {"expires_at": now + ttl_seconds}
            self._write_store(store)
            return True

    def claim_event_id(
        self,
        trigger_id: str,
        event_id: str,
        *,
        now: int,
        ttl_seconds: int,
    ) -> ExternalTriggerEventClaim:
        """Claim one external event id and return its replay state."""
        with self._state.lock:
            store = self._read_store()
            _prune_expired(store, now=now)
            trigger_events = store["events"].setdefault(trigger_id, {})
            event = trigger_events.get(event_id)
            if event is not None:
                if event["state"] == "delivered":
                    return ExternalTriggerEventClaim.DELIVERED
                return ExternalTriggerEventClaim.IN_PROGRESS
            trigger_events[event_id] = {
                "state": ExternalTriggerEventClaim.IN_PROGRESS.value,
                "expires_at": now + ttl_seconds,
                "delivered_at": None,
            }
            self._write_store(store)
            return ExternalTriggerEventClaim.FRESH

    def event_id_is_delivered(self, trigger_id: str, event_id: str, *, now: int) -> bool:
        """Return whether one unexpired external event id was already delivered."""
        with self._state.lock:
            store = self._read_store()
            _prune_expired(store, now=now)
            event = store["events"].get(trigger_id, {}).get(event_id)
            return event is not None and event["state"] == ExternalTriggerEventClaim.DELIVERED.value

    def mark_event_delivered(self, trigger_id: str, event_id: str, *, now: int, ttl_seconds: int) -> None:
        """Record that one external event id reached Matrix delivery."""
        with self._state.lock:
            store = self._read_store()
            _prune_expired(store, now=now)
            trigger_events = store["events"].setdefault(trigger_id, {})
            trigger_events[event_id] = {
                "state": ExternalTriggerEventClaim.DELIVERED.value,
                "expires_at": now + ttl_seconds,
                "delivered_at": now,
            }
            self._write_store(store)

    def release_event_id(self, trigger_id: str, event_id: str) -> None:
        """Remove an event id claim after delivery failure."""
        with self._state.lock:
            store = self._read_store()
            trigger_events = store["events"].get(trigger_id)
            if trigger_events is None:
                return
            trigger_events.pop(event_id, None)
            if not trigger_events:
                store["events"].pop(trigger_id, None)
            self._write_store(store)

    def _read_store(self) -> _SerializedReplayStore:
        if not self._store_path.exists():
            return _empty_store()
        try:
            raw_store = json.loads(self._store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = "invalid external trigger replay store JSON"
            raise ExternalTriggerReplayStoreError(msg) from exc
        return _normalize_store(raw_store)

    def _write_store(self, store: _SerializedReplayStore) -> None:
        self.tracking_root.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.tracking_root,
                prefix=f"{self._store_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = self.tracking_root / Path(temp_file.name).name
                json.dump(store, temp_file, indent=2, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            temp_path.replace(self._store_path)
            _fsync_directory(self.tracking_root)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def _empty_store() -> _SerializedReplayStore:
    return {"nonces": {}, "events": {}}


def _normalize_store(raw_store: object) -> _SerializedReplayStore:
    if not isinstance(raw_store, Mapping):
        raise _invalid_store_structure()
    store_mapping = cast("Mapping[object, object]", raw_store)
    if "nonces" not in store_mapping or "events" not in store_mapping:
        raise _invalid_store_structure()
    raw_nonces = store_mapping["nonces"]
    raw_events = store_mapping["events"]
    if not isinstance(raw_nonces, Mapping) or not isinstance(raw_events, Mapping):
        raise _invalid_store_structure()
    return {
        "nonces": _normalize_nonces(cast("Mapping[object, object]", raw_nonces)),
        "events": _normalize_events(cast("Mapping[object, object]", raw_events)),
    }


def _invalid_store_structure() -> ExternalTriggerReplayStoreError:
    return ExternalTriggerReplayStoreError("invalid external trigger replay store structure")


def _is_json_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_nonces(raw_nonces: Mapping[object, object]) -> dict[str, dict[str, _SerializedNonce]]:
    nonces: dict[str, dict[str, _SerializedNonce]] = {}
    for trigger_id, trigger_nonces in raw_nonces.items():
        if not isinstance(trigger_id, str) or not isinstance(trigger_nonces, Mapping):
            raise _invalid_store_structure()
        trigger_nonce_mapping = cast("Mapping[object, object]", trigger_nonces)
        normalized_trigger_nonces: dict[str, _SerializedNonce] = {}
        for nonce, record in trigger_nonce_mapping.items():
            if not isinstance(nonce, str) or not isinstance(record, Mapping):
                raise _invalid_store_structure()
            record_mapping = cast("Mapping[object, object]", record)
            expires_at = record_mapping.get("expires_at")
            if not _is_json_int(expires_at):
                raise _invalid_store_structure()
            normalized_trigger_nonces[nonce] = {"expires_at": expires_at}
        if normalized_trigger_nonces:
            nonces[trigger_id] = normalized_trigger_nonces
    return nonces


def _normalize_events(raw_events: Mapping[object, object]) -> dict[str, dict[str, _SerializedEvent]]:
    events: dict[str, dict[str, _SerializedEvent]] = {}
    for trigger_id, trigger_events in raw_events.items():
        if not isinstance(trigger_id, str) or not isinstance(trigger_events, Mapping):
            raise _invalid_store_structure()
        trigger_event_mapping = cast("Mapping[object, object]", trigger_events)
        normalized_trigger_events: dict[str, _SerializedEvent] = {}
        for event_id, record in trigger_event_mapping.items():
            if not isinstance(event_id, str) or not isinstance(record, Mapping):
                raise _invalid_store_structure()
            record_mapping = cast("Mapping[object, object]", record)
            if "delivered_at" not in record_mapping:
                raise _invalid_store_structure()
            state = record_mapping.get("state")
            expires_at = record_mapping.get("expires_at")
            delivered_at = record_mapping.get("delivered_at")
            if state not in {"in_progress", "delivered"} or not _is_json_int(expires_at):
                raise _invalid_store_structure()
            if delivered_at is not None and not _is_json_int(delivered_at):
                raise _invalid_store_structure()
            event_state = cast("Literal['in_progress', 'delivered']", state)
            normalized_trigger_events[event_id] = {
                "state": event_state,
                "expires_at": expires_at,
                "delivered_at": delivered_at,
            }
        if normalized_trigger_events:
            events[trigger_id] = normalized_trigger_events
    return events


def _prune_expired(store: _SerializedReplayStore, *, now: int) -> None:
    for trigger_id, trigger_nonces in list(store["nonces"].items()):
        store["nonces"][trigger_id] = {
            nonce: record for nonce, record in trigger_nonces.items() if record["expires_at"] >= now
        }
        if not store["nonces"][trigger_id]:
            store["nonces"].pop(trigger_id)

    for trigger_id, trigger_events in list(store["events"].items()):
        store["events"][trigger_id] = {
            event_id: record for event_id, record in trigger_events.items() if record["expires_at"] >= now
        }
        if not store["events"][trigger_id]:
            store["events"].pop(trigger_id)


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
