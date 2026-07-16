"""Primary-runtime store for tool-minted one-shot callbacks."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.validation import non_empty_stripped
from mindroom.durable_write import write_json_file_durable
from mindroom.external_triggers.store import (
    ExternalTriggerStoreError,
    validate_delivery_owner,
    validate_delivery_target,
)
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.state import resolve_room_id

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_CALLBACK_ID_PATTERN = r"^cb_[a-f0-9]{16}$"
_CALLBACK_RECORDS_VERSION = 1
_CALLBACK_STATE_DIR = "callbacks"
_CALLBACK_RECORDS_FILENAME = "records.json"
_CALLBACK_TOKEN_PREFIX = "mrcb_"  # noqa: S105 - token namespace prefix, not a secret
_MAX_LABEL_LENGTH = 200


class CallbackStoreError(RuntimeError):
    """Raised when callback records cannot be read, trusted, or changed."""


class CallbackNotFoundError(CallbackStoreError):
    """Raised when a callback record does not exist."""


class CallbackConsumedError(CallbackStoreError):
    """Raised when a callback has no uses left."""


class CallbackExpiredError(CallbackStoreError):
    """Raised when a callback is past its expiry."""


class CallbackRecordNotDeliverableError(CallbackStoreError):
    """Raised when a stored callback is no longer deliverable under current config."""


def _generate_callback_token() -> str:
    """Return one fresh bearer token; only its hash is ever stored."""
    return _CALLBACK_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _hash_callback_token(token: str) -> str:
    """Return the stored SHA-256 hex digest for one bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches_hash(token: str, token_hash: str) -> bool:
    """Compare one presented token against a stored hash in constant time."""
    return hmac.compare_digest(_hash_callback_token(token), token_hash)


def _generate_callback_id() -> str:
    return f"cb_{secrets.token_hex(8)}"


class CallbackRecord(BaseModel):
    """Durable one-shot callback record owned by one Matrix user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    callback_id: str = Field(pattern=_CALLBACK_ID_PATTERN)
    token_hash: str
    owner_user_id: str
    created_by_agent_name: str
    created_in_room_id: str
    created_in_thread_id: str | None = None
    target_room_id: str
    target_thread_id: str | None = None
    target_agent: str
    label: str
    on_expiry: Literal["notify", "silent"]
    max_uses: int = Field(ge=1)
    uses_left: int = Field(ge=0)
    consumed_at: int | None = None
    script_path: str | None = None
    created_at: int
    expires_at: int

    @field_validator("owner_user_id")
    @classmethod
    def validate_owner_user_id(cls, value: str) -> str:
        """Require a valid Matrix user ID."""
        owner_user_id = non_empty_stripped(value, field_name="owner_user_id")
        MatrixID.parse(owner_user_id)
        return owner_user_id

    @field_validator("token_hash", "created_by_agent_name", "created_in_room_id", "target_room_id", "target_agent")
    @classmethod
    def validate_required_record_text(cls, value: str) -> str:
        """Reject empty required record fields."""
        return non_empty_stripped(value, field_name="record")

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        """Require a short single-line label."""
        label = non_empty_stripped(value, field_name="label")
        if len(label) > _MAX_LABEL_LENGTH:
            msg = f"label must be at most {_MAX_LABEL_LENGTH} characters"
            raise ValueError(msg)
        if any(character in label for character in "\r\n"):
            msg = "label must be a single line"
            raise ValueError(msg)
        return label

    @model_validator(mode="after")
    def validate_uses(self) -> CallbackRecord:
        """Keep the uses counter within the minted budget."""
        if self.uses_left > self.max_uses:
            msg = "uses_left must not exceed max_uses"
            raise ValueError(msg)
        return self


class CallbackDeliverySnapshot(BaseModel):
    """Immutable delivery inputs for one accepted callback request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    callback_id: str
    token_hash: str
    owner_user_id: str
    label: str
    target_agent: str
    target_thread_id: str | None = None
    resolved_room_id: str
    uses_left: int
    expires_at: int
    on_expiry: Literal["notify", "silent"]
    config_generation: int


class _SerializedCallbackRecords(BaseModel):
    """On-disk callback records payload."""

    model_config = ConfigDict(extra="forbid")

    version: int = _CALLBACK_RECORDS_VERSION
    callbacks: dict[str, CallbackRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_record_keys(self) -> _SerializedCallbackRecords:
        """Keep map keys and embedded callback IDs aligned."""
        if self.version != _CALLBACK_RECORDS_VERSION:
            msg = "unsupported callback store version"
            raise ValueError(msg)
        for callback_id, record in self.callbacks.items():
            if callback_id != record.callback_id:
                msg = "callback record key does not match callback_id"
                raise ValueError(msg)
        return self


class CallbackStore:
    """JSON-backed callback record store under primary control state."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        """Bind the store to one primary runtime path set."""
        if runtime_paths.control_state_root is None:
            msg = "Callback store requires primary control state"
            raise CallbackStoreError(msg)
        self._runtime_paths = runtime_paths
        self._root = runtime_paths.control_state_root / _CALLBACK_STATE_DIR
        self._store_path = self._root / _CALLBACK_RECORDS_FILENAME
        self._lock_path = self._root / f"{_CALLBACK_RECORDS_FILENAME}.lock"

    @property
    def store_path(self) -> Path:
        """Return the on-disk callback store path."""
        return self._store_path

    def mint_record(
        self,
        *,
        owner_user_id: str,
        created_by_agent_name: str,
        created_in_room_id: str,
        created_in_thread_id: str | None,
        target_room_id: str,
        target_thread_id: str | None,
        target_agent: str,
        label: str,
        ttl_seconds: int | None,
        max_uses: int,
        on_expiry: Literal["notify", "silent"],
        config: Config,
    ) -> tuple[CallbackRecord, str]:
        """Create one callback record and return it with its raw bearer token."""
        policy = config.callback_policy
        if ttl_seconds is not None and ttl_seconds < 1:
            msg = "ttl_seconds must be positive"
            raise CallbackStoreError(msg)
        if max_uses < 1:
            msg = "max_uses must be at least 1"
            raise CallbackStoreError(msg)
        now = int(time.time())
        token = _generate_callback_token()
        record = CallbackRecord(
            callback_id=_generate_callback_id(),
            token_hash=_hash_callback_token(token),
            owner_user_id=owner_user_id,
            created_by_agent_name=created_by_agent_name,
            created_in_room_id=created_in_room_id,
            created_in_thread_id=created_in_thread_id,
            target_room_id=target_room_id,
            target_thread_id=target_thread_id,
            target_agent=target_agent,
            label=label,
            on_expiry=on_expiry,
            max_uses=min(max_uses, policy.max_uses_cap),
            uses_left=min(max_uses, policy.max_uses_cap),
            created_at=now,
            expires_at=now + min(ttl_seconds or policy.default_ttl_seconds, policy.max_ttl_seconds),
        )
        self._validate_record_against_config(record, config)
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            if record.callback_id in records.callbacks:
                msg = f"callback already exists: {record.callback_id}"
                raise CallbackStoreError(msg)
            active_owner_count = sum(
                1
                for existing in records.callbacks.values()
                if existing.owner_user_id == owner_user_id and existing.uses_left > 0 and existing.expires_at > now
            )
            if active_owner_count >= policy.max_active_per_owner:
                msg = "callback owner quota exceeded"
                raise CallbackStoreError(msg)
            records.callbacks[record.callback_id] = record
            self._write_records(records)
        return record, token

    def set_script_path(self, callback_id: str, script_path: str) -> CallbackRecord:
        """Attach the generated script path to one freshly minted record."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = records.callbacks.get(callback_id)
            if record is None:
                msg = f"callback not found: {callback_id}"
                raise CallbackNotFoundError(msg)
            updated = _validate_record_update(record.model_copy(update={"script_path": script_path}))
            records.callbacks[callback_id] = updated
            self._write_records(records)
            return updated

    def list_records(self, *, owner_user_id: str | None = None) -> list[CallbackRecord]:
        """Return callback records, optionally filtered by owner."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            records = list(self._read_records().callbacks.values())
        if owner_user_id is None:
            return records
        return [record for record in records if record.owner_user_id == owner_user_id]

    def get_record(self, callback_id: str) -> CallbackRecord | None:
        """Return one callback record if it exists."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            return self._read_records().callbacks.get(callback_id)

    def delete_record(self, callback_id: str, *, actor_user_id: str, config: Config) -> CallbackRecord:
        """Delete one callback record as its owner or a trigger-family admin."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = records.callbacks.get(callback_id)
            if record is None:
                msg = f"callback not found: {callback_id}"
                raise CallbackNotFoundError(msg)
            if (
                actor_user_id != record.owner_user_id
                and actor_user_id not in config.external_trigger_policy.admin_users
            ):
                msg = "callback can only be revoked by its owner or an external trigger admin"
                raise CallbackStoreError(msg)
            records.callbacks.pop(callback_id)
            self._write_records(records)
            return record

    def delete_record_unchecked(self, callback_id: str) -> None:
        """Delete one callback record without an actor check (expiry sweep only)."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            if records.callbacks.pop(callback_id, None) is None:
                return
            self._write_records(records)

    def claim_use(self, callback_id: str, *, now: int) -> int:
        """Atomically consume one use and return the remaining budget."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = records.callbacks.get(callback_id)
            if record is None:
                msg = f"callback not found: {callback_id}"
                raise CallbackNotFoundError(msg)
            if record.uses_left <= 0:
                msg = "callback has already been used"
                raise CallbackConsumedError(msg)
            if record.expires_at <= now:
                msg = "callback has expired"
                raise CallbackExpiredError(msg)
            uses_left = record.uses_left - 1
            updated = _validate_record_update(
                record.model_copy(
                    update={"uses_left": uses_left, "consumed_at": now if uses_left == 0 else None},
                ),
            )
            records.callbacks[callback_id] = updated
            self._write_records(records)
            return uses_left

    def release_use(self, callback_id: str) -> None:
        """Return one claimed use after a delivery failure, best effort."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = records.callbacks.get(callback_id)
            if record is None or record.uses_left >= record.max_uses:
                return
            updated = _validate_record_update(
                record.model_copy(update={"uses_left": record.uses_left + 1, "consumed_at": None}),
            )
            records.callbacks[callback_id] = updated
            self._write_records(records)

    def list_expired(self, *, now: int) -> list[CallbackRecord]:
        """Return every record whose expiry has passed."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            records = self._read_records()
        return [record for record in records.callbacks.values() if record.expires_at <= now]

    def delivery_snapshot(
        self,
        callback_id: str,
        *,
        config: Config,
        config_generation: int,
    ) -> CallbackDeliverySnapshot | None:
        """Return one delivery snapshot after revalidating against current config."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            record = self._read_records().callbacks.get(callback_id)
        if record is None:
            return None
        try:
            self._validate_record_against_config(record, config)
        except CallbackStoreError as exc:
            raise CallbackRecordNotDeliverableError(str(exc)) from exc
        return CallbackDeliverySnapshot(
            callback_id=record.callback_id,
            token_hash=record.token_hash,
            owner_user_id=record.owner_user_id,
            label=record.label,
            target_agent=record.target_agent,
            target_thread_id=record.target_thread_id,
            resolved_room_id=resolve_room_id(record.target_room_id, self._runtime_paths),
            uses_left=record.uses_left,
            expires_at=record.expires_at,
            on_expiry=record.on_expiry,
            config_generation=config_generation,
        )

    def _validate_record_against_config(self, record: CallbackRecord, config: Config) -> None:
        try:
            validate_delivery_owner(record.owner_user_id, config, self._runtime_paths, subject="callback")
            validate_delivery_target(
                target_agent=record.target_agent,
                target_room_id=record.target_room_id,
                created_by_agent_name=record.created_by_agent_name,
                created_in_room_id=record.created_in_room_id,
                config=config,
                runtime_paths=self._runtime_paths,
                subject="callback",
            )
        except ExternalTriggerStoreError as exc:
            raise CallbackStoreError(str(exc)) from exc

    def _read_records(self) -> _SerializedCallbackRecords:
        try:
            if not self._store_path.exists():
                return _SerializedCallbackRecords()
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
            return _SerializedCallbackRecords.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            msg = "invalid callback store"
            raise CallbackStoreError(msg) from exc

    def _write_records(self, records: _SerializedCallbackRecords) -> None:
        try:
            write_json_file_durable(
                self._store_path,
                records.model_dump(mode="json"),
                temp_dir=self._root,
                indent=2,
                sort_keys=True,
            )
        except OSError as exc:
            msg = "callback store is unavailable"
            raise CallbackStoreError(msg) from exc


def _validate_record_update(record: CallbackRecord) -> CallbackRecord:
    """Validate a record produced through ``model_copy(update=...)``."""
    return CallbackRecord.model_validate(record.model_dump(mode="json"))
