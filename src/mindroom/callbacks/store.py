"""Durable single-use callback records."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.validation import non_empty_stripped
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.identity import MatrixID

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_CALLBACK_TTL_SECONDS = 7 * 24 * 60 * 60
_CALLBACK_ID_PATTERN = r"^cb_[a-f0-9]{16}$"
_CALLBACK_RECORDS_VERSION = 1
_CALLBACK_STATE_DIR = "callbacks"
_CALLBACK_RECORDS_FILENAME = "records.json"
_CALLBACK_TOKEN_PREFIX = "mrcb_"  # noqa: S105 - token namespace prefix, not a secret
_MAX_LABEL_LENGTH = 200


class CallbackStoreError(RuntimeError):
    """Raised when callback state cannot be read or changed."""


class CallbackNotFoundError(CallbackStoreError):
    """Raised when a callback record does not exist."""


class CallbackClaimedError(CallbackStoreError):
    """Raised when another request has already claimed a callback."""


class CallbackExpiredError(CallbackStoreError):
    """Raised when a callback has expired."""


def _generate_callback_token() -> str:
    return _CALLBACK_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _hash_callback_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches_hash(token: str, token_hash: str) -> bool:
    """Compare one presented token against its stored hash."""
    return hmac.compare_digest(_hash_callback_token(token), token_hash)


class CallbackRecord(BaseModel):
    """One callback bound to the conversation that minted it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    callback_id: str = Field(pattern=_CALLBACK_ID_PATTERN)
    token_hash: str
    owner_user_id: str
    room_id: str
    thread_id: str | None = None
    agent_name: str
    label: str
    expires_at: int
    claimed: bool = False

    @field_validator("owner_user_id")
    @classmethod
    def validate_owner_user_id(cls, value: str) -> str:
        """Require a valid Matrix owner ID."""
        owner_user_id = non_empty_stripped(value, field_name="owner_user_id")
        MatrixID.parse(owner_user_id)
        return owner_user_id

    @field_validator("token_hash", "room_id", "agent_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """Reject empty delivery fields."""
        return non_empty_stripped(value, field_name="callback record")

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        """Require a short single-line task label."""
        label = non_empty_stripped(value, field_name="label")
        if len(label) > _MAX_LABEL_LENGTH:
            msg = f"label must be at most {_MAX_LABEL_LENGTH} characters"
            raise ValueError(msg)
        if any(character in label for character in "\r\n"):
            msg = "label must be a single line"
            raise ValueError(msg)
        return label


class _SerializedCallbackRecords(BaseModel):
    """On-disk callback records payload."""

    model_config = ConfigDict(extra="forbid")

    version: int = _CALLBACK_RECORDS_VERSION
    callbacks: dict[str, CallbackRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_record_keys(self) -> _SerializedCallbackRecords:
        if self.version != _CALLBACK_RECORDS_VERSION:
            msg = "unsupported callback store version"
            raise ValueError(msg)
        if any(callback_id != record.callback_id for callback_id, record in self.callbacks.items()):
            msg = "callback record key does not match callback_id"
            raise ValueError(msg)
        return self


class CallbackStore:
    """Small JSON-backed callback store under primary control state."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        if runtime_paths.control_state_root is None:
            msg = "Callback store requires primary control state"
            raise CallbackStoreError(msg)
        self._root = runtime_paths.control_state_root / _CALLBACK_STATE_DIR
        self._store_path = self._root / _CALLBACK_RECORDS_FILENAME
        self._lock_path = self._root / f"{_CALLBACK_RECORDS_FILENAME}.lock"

    @property
    def store_path(self) -> Path:
        """Return the records file path."""
        return self._store_path

    def mint_record(
        self,
        *,
        owner_user_id: str,
        room_id: str,
        thread_id: str | None,
        agent_name: str,
        label: str,
    ) -> tuple[CallbackRecord, str]:
        """Create one seven-day callback and return its raw bearer token once."""
        now = int(time.time())
        token = _generate_callback_token()
        record = CallbackRecord(
            callback_id=f"cb_{secrets.token_hex(8)}",
            token_hash=_hash_callback_token(token),
            owner_user_id=owner_user_id,
            room_id=room_id,
            thread_id=thread_id,
            agent_name=agent_name,
            label=label,
            expires_at=now + _CALLBACK_TTL_SECONDS,
        )
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            records.callbacks = {
                callback_id: existing
                for callback_id, existing in records.callbacks.items()
                if existing.expires_at > now
            }
            records.callbacks[record.callback_id] = record
            self._write_records(records)
        return record, token

    def get_record(self, callback_id: str) -> CallbackRecord | None:
        """Return one callback without changing it."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            return self._read_records().callbacks.get(callback_id)

    def claim(self, callback_id: str, *, now: int) -> CallbackRecord:
        """Atomically reserve one callback for delivery."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = records.callbacks.get(callback_id)
            if record is None:
                msg = "callback not found"
                raise CallbackNotFoundError(msg)
            if record.expires_at <= now:
                records.callbacks.pop(callback_id)
                self._write_records(records)
                msg = "callback has expired"
                raise CallbackExpiredError(msg)
            if record.claimed:
                msg = "callback has already been used"
                raise CallbackClaimedError(msg)
            claimed = CallbackRecord.model_validate(record.model_copy(update={"claimed": True}))
            records.callbacks[callback_id] = claimed
            self._write_records(records)
            return claimed

    def release(self, callback_id: str) -> None:
        """Release a claim after delivery failure so the script can retry."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = records.callbacks.get(callback_id)
            if record is None or not record.claimed:
                return
            records.callbacks[callback_id] = CallbackRecord.model_validate(
                record.model_copy(update={"claimed": False}),
            )
            self._write_records(records)

    def delete(self, callback_id: str) -> None:
        """Delete one callback after delivery or failed script creation."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            if records.callbacks.pop(callback_id, None) is not None:
                self._write_records(records)

    def _read_records(self) -> _SerializedCallbackRecords:
        try:
            if not self._store_path.exists():
                return _SerializedCallbackRecords()
            return _SerializedCallbackRecords.model_validate_json(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
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
