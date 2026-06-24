"""Primary-runtime store for tool-managed external triggers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom import constants
from mindroom.config.validation import non_empty_stripped
from mindroom.durable_write import write_json_file_durable
from mindroom.entity_resolution import (
    MissingManagedEntityAccountError,
    configured_routable_entity_names_for_room,
    entity_identity_registry,
)
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import matrix_state_for_runtime, resolve_room_id
from mindroom.matrix_identifiers import agent_username_localpart, extract_server_name_from_homeserver

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_TRIGGER_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"
_TRIGGER_RECORDS_VERSION = 1
_ED25519_PUBLIC_KEY_BYTES = 32
_EXTERNAL_TRIGGER_STATE_DIR = "external_triggers"
_TRIGGER_RECORDS_FILENAME = "triggers.json"


class ExternalTriggerStoreError(RuntimeError):
    """Raised when trigger records cannot be read or trusted."""


class ExternalTriggerRecordNotDeliverableError(ExternalTriggerStoreError):
    """Raised when a stored trigger is no longer deliverable under current config."""


class ExternalTriggerTarget(BaseModel):
    """Resolved target for one external trigger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    room_id: str = Field(description="Configured room key, room alias, or Matrix room ID")
    thread_id: str | None = Field(default=None, description="Optional Matrix thread event ID")
    agent: str = Field(description="Agent or team name to mention")
    new_thread: bool = Field(default=False, description="Whether the trigger starts a new thread")

    @field_validator("room_id", "agent")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """Reject empty target fields."""
        return non_empty_stripped(value, field_name="target")

    @field_validator("thread_id")
    @classmethod
    def validate_thread_id(cls, value: str | None) -> str | None:
        """Reject empty thread IDs."""
        if value is None:
            return None
        return non_empty_stripped(value, field_name="thread_id")


class ExternalTriggerRecord(BaseModel):
    """Durable trigger record owned by one Matrix user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_id: str = Field(pattern=_TRIGGER_ID_PATTERN)
    uid: str
    version: int = Field(ge=1)
    auth_epoch: int = Field(ge=1)
    enabled: bool
    description: str = ""
    owner_user_id: str
    created_by_agent_name: str
    created_in_room_id: str
    created_in_thread_id: str | None = None
    target: ExternalTriggerTarget
    auth: Literal["ed25519"] = "ed25519"
    key_id: str
    public_key: str
    public_key_fingerprint: str
    allowed_kinds: tuple[str, ...] = ()
    replay_window_seconds: int = Field(ge=30, le=3600)
    max_body_bytes: int = Field(ge=1024, le=262144)
    created_at: int
    updated_at: int

    @field_validator("owner_user_id")
    @classmethod
    def validate_owner_user_id(cls, value: str) -> str:
        """Require a valid Matrix user ID."""
        owner_user_id = non_empty_stripped(value, field_name="owner_user_id")
        MatrixID.parse(owner_user_id)
        return owner_user_id

    @field_validator("uid", "created_by_agent_name", "created_in_room_id", "key_id", "public_key")
    @classmethod
    def validate_required_record_text(cls, value: str) -> str:
        """Reject empty required record fields."""
        return non_empty_stripped(value, field_name="record")

    @field_validator("allowed_kinds", mode="before")
    @classmethod
    def normalize_allowed_kinds(cls, value: object) -> tuple[str, ...]:
        """Normalize allowed kinds to a duplicate-free tuple."""
        if value is None:
            return ()
        if not isinstance(value, Iterable) or isinstance(value, str):
            msg = "allowed_kinds must be a list of strings"
            raise TypeError(msg)
        kinds: list[str] = []
        for item in value:
            if not isinstance(item, str):
                msg = "allowed_kinds must be a list of strings"
                raise TypeError(msg)
            kind = non_empty_stripped(item, field_name="allowed_kinds")
            if kind not in kinds:
                kinds.append(kind)
        return tuple(kinds)

    @model_validator(mode="after")
    def validate_key_fingerprint(self) -> ExternalTriggerRecord:
        """Ensure key material and fingerprint match."""
        fingerprint = public_key_fingerprint(self.public_key)
        if self.public_key_fingerprint != fingerprint:
            msg = "public_key_fingerprint does not match public_key"
            raise ValueError(msg)
        return self


class TriggerDeliverySnapshot(BaseModel):
    """Immutable delivery inputs for one accepted trigger request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_id: str
    uid: str
    version: int
    auth_epoch: int
    config_generation: int
    enabled: bool
    description: str
    owner_user_id: str
    created_by_agent_name: str
    created_in_room_id: str
    created_in_thread_id: str | None = None
    target: ExternalTriggerTarget
    resolved_room_id: str
    auth: Literal["ed25519"]
    key_id: str
    public_key: str
    public_key_fingerprint: str
    allowed_kinds: tuple[str, ...]
    replay_window_seconds: int
    max_body_bytes: int
    replay_scope: str


class _SerializedTriggerRecords(BaseModel):
    """On-disk trigger records payload."""

    model_config = ConfigDict(extra="forbid")

    version: int = _TRIGGER_RECORDS_VERSION
    triggers: dict[str, ExternalTriggerRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_record_keys(self) -> _SerializedTriggerRecords:
        """Keep map keys and embedded trigger IDs aligned."""
        if self.version != _TRIGGER_RECORDS_VERSION:
            msg = "unsupported external trigger store version"
            raise ValueError(msg)
        for trigger_id, record in self.triggers.items():
            if trigger_id != record.trigger_id:
                msg = "external trigger record key does not match trigger_id"
                raise ValueError(msg)
        return self


class ExternalTriggerStore:
    """JSON-backed trigger record store under primary control state."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        """Bind the store to one primary runtime path set."""
        if runtime_paths.control_state_root is None:
            msg = "External trigger store requires primary control state"
            raise ExternalTriggerStoreError(msg)
        self._runtime_paths = runtime_paths
        self._root = runtime_paths.control_state_root / _EXTERNAL_TRIGGER_STATE_DIR
        self._store_path = self._root / _TRIGGER_RECORDS_FILENAME
        self._lock_path = self._root / f"{_TRIGGER_RECORDS_FILENAME}.lock"

    @property
    def store_path(self) -> Path:
        """Return the on-disk trigger store path."""
        return self._store_path

    def list_records(self, *, owner_user_id: str | None = None) -> list[ExternalTriggerRecord]:
        """Return trigger records, optionally filtered by owner."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            records = list(self._read_records().triggers.values())
        if owner_user_id is None:
            return records
        return [record for record in records if record.owner_user_id == owner_user_id]

    def create_record(
        self,
        *,
        trigger_id: str,
        owner_user_id: str,
        created_by_agent_name: str,
        created_in_room_id: str,
        created_in_thread_id: str | None,
        target: ExternalTriggerTarget,
        public_key: str,
        key_id: str = "default",
        description: str = "",
        allowed_kinds: Iterable[str] = (),
        replay_window_seconds: int | None = None,
        max_body_bytes: int | None = None,
        enabled: bool = True,
        config: Config,
    ) -> ExternalTriggerRecord:
        """Create one trigger record after validating current config policy."""
        _validate_trigger_id(trigger_id)
        policy = config.external_trigger_policy
        now = int(time.time())
        normalized_public_key = _normalize_public_key(public_key)
        record = ExternalTriggerRecord(
            trigger_id=trigger_id,
            uid=uuid.uuid4().hex,
            version=1,
            auth_epoch=1,
            enabled=enabled,
            description=description,
            owner_user_id=owner_user_id,
            created_by_agent_name=created_by_agent_name,
            created_in_room_id=created_in_room_id,
            created_in_thread_id=created_in_thread_id,
            target=target,
            key_id=key_id,
            public_key=normalized_public_key,
            public_key_fingerprint=public_key_fingerprint(normalized_public_key),
            allowed_kinds=tuple(allowed_kinds),
            replay_window_seconds=min(
                replay_window_seconds or policy.default_replay_window_seconds,
                policy.max_replay_window_seconds,
            ),
            max_body_bytes=min(max_body_bytes or policy.default_max_body_bytes, policy.max_body_bytes),
            created_at=now,
            updated_at=now,
        )
        self._validate_record_against_config(record, config)
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            if trigger_id in records.triggers:
                msg = f"external trigger already exists: {trigger_id}"
                raise ExternalTriggerStoreError(msg)
            owner_count = sum(1 for existing in records.triggers.values() if existing.owner_user_id == owner_user_id)
            if owner_count >= policy.max_triggers_per_owner:
                msg = "external trigger owner quota exceeded"
                raise ExternalTriggerStoreError(msg)
            records.triggers[trigger_id] = record
            self._write_records(records)
        return record

    def set_enabled(
        self,
        trigger_id: str,
        *,
        enabled: bool,
        actor_user_id: str,
        config: Config,
    ) -> ExternalTriggerRecord:
        """Enable or disable one trigger record."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = self._require_owned_record(records, trigger_id, actor_user_id, config)
            updated = _validate_record_update(
                record.model_copy(
                    update={"enabled": enabled, "version": record.version + 1, "updated_at": int(time.time())},
                ),
            )
            records.triggers[trigger_id] = updated
            self._write_records(records)
            return updated

    def rotate_key(
        self,
        trigger_id: str,
        *,
        public_key: str,
        key_id: str,
        actor_user_id: str,
        config: Config,
    ) -> ExternalTriggerRecord:
        """Rotate one trigger public key and advance the replay scope."""
        normalized_public_key = _normalize_public_key(public_key)
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            record = self._require_owned_record(records, trigger_id, actor_user_id, config)
            updated = _validate_record_update(
                record.model_copy(
                    update={
                        "auth_epoch": record.auth_epoch + 1,
                        "key_id": non_empty_stripped(key_id, field_name="key_id"),
                        "public_key": normalized_public_key,
                        "public_key_fingerprint": public_key_fingerprint(normalized_public_key),
                        "version": record.version + 1,
                        "updated_at": int(time.time()),
                    },
                ),
            )
            records.triggers[trigger_id] = updated
            self._write_records(records)
            return updated

    def delete_record(self, trigger_id: str, *, actor_user_id: str, config: Config) -> None:
        """Delete one trigger record."""
        with advisory_file_lock(self._lock_path):
            records = self._read_records()
            self._require_owned_record(records, trigger_id, actor_user_id, config)
            records.triggers.pop(trigger_id)
            self._write_records(records)

    def delivery_snapshot(
        self,
        trigger_id: str,
        *,
        config: Config,
        config_generation: int,
    ) -> TriggerDeliverySnapshot | None:
        """Return one delivery snapshot after revalidating against current config."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            record = self._read_records().triggers.get(trigger_id)
        if record is None:
            return None
        try:
            self._validate_record_against_config(record, config)
        except ExternalTriggerStoreError as exc:
            raise ExternalTriggerRecordNotDeliverableError(str(exc)) from exc
        policy = config.external_trigger_policy
        resolved_room_id = resolve_room_id(record.target.room_id, self._runtime_paths)
        return TriggerDeliverySnapshot(
            trigger_id=record.trigger_id,
            uid=record.uid,
            version=record.version,
            auth_epoch=record.auth_epoch,
            config_generation=config_generation,
            enabled=record.enabled,
            description=record.description,
            owner_user_id=record.owner_user_id,
            created_by_agent_name=record.created_by_agent_name,
            created_in_room_id=record.created_in_room_id,
            created_in_thread_id=record.created_in_thread_id,
            target=record.target,
            resolved_room_id=resolved_room_id,
            auth=record.auth,
            key_id=record.key_id,
            public_key=record.public_key,
            public_key_fingerprint=record.public_key_fingerprint,
            allowed_kinds=record.allowed_kinds,
            replay_window_seconds=min(record.replay_window_seconds, policy.max_replay_window_seconds),
            max_body_bytes=min(record.max_body_bytes, policy.max_body_bytes),
            replay_scope=f"{record.uid}:{record.auth_epoch}",
        )

    def _require_owned_record(
        self,
        records: _SerializedTriggerRecords,
        trigger_id: str,
        actor_user_id: str,
        config: Config,
    ) -> ExternalTriggerRecord:
        record = records.triggers.get(trigger_id)
        if record is None:
            msg = f"external trigger not found: {trigger_id}"
            raise ExternalTriggerStoreError(msg)
        if actor_user_id != record.owner_user_id and actor_user_id not in config.external_trigger_policy.admin_users:
            msg = "external trigger can only be changed by its owner or an external trigger admin"
            raise ExternalTriggerStoreError(msg)
        return record

    def _validate_record_against_config(self, record: ExternalTriggerRecord, config: Config) -> None:
        _validate_owner(record.owner_user_id, config, self._runtime_paths)
        _validate_target(record.target, config, self._runtime_paths)

    def _read_records(self) -> _SerializedTriggerRecords:
        if not self._store_path.exists():
            return _SerializedTriggerRecords()
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
            return _SerializedTriggerRecords.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            msg = "invalid external trigger store"
            raise ExternalTriggerStoreError(msg) from exc

    def _write_records(self, records: _SerializedTriggerRecords) -> None:
        try:
            write_json_file_durable(
                self._store_path,
                records.model_dump(mode="json"),
                temp_dir=self._root,
                indent=2,
                sort_keys=True,
            )
        except OSError as exc:
            msg = "external trigger store is unavailable"
            raise ExternalTriggerStoreError(msg) from exc


def _validate_trigger_id(trigger_id: str) -> str:
    """Validate one route-safe trigger id."""
    normalized = non_empty_stripped(trigger_id, field_name="trigger_id")
    if re.fullmatch(_TRIGGER_ID_PATTERN, normalized) is None:
        msg = "trigger_id must contain only ASCII letters, digits, underscore, or hyphen"
        raise ExternalTriggerStoreError(msg)
    return normalized


def _normalize_public_key(public_key: str) -> str:
    """Validate and normalize one base64 Ed25519 public key."""
    normalized = non_empty_stripped(public_key, field_name="public_key")
    try:
        public_key_bytes = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "public_key must be strict base64-encoded Ed25519 public key bytes"
        raise ExternalTriggerStoreError(msg) from exc
    if len(public_key_bytes) != _ED25519_PUBLIC_KEY_BYTES:
        msg = "public_key must decode to 32 raw Ed25519 public key bytes"
        raise ExternalTriggerStoreError(msg)
    return normalized


def public_key_fingerprint(public_key: str) -> str:
    """Return the stable fingerprint for one base64 Ed25519 public key."""
    normalized = _normalize_public_key(public_key)
    public_key_bytes = base64.b64decode(normalized, validate=True)
    return f"sha256:{hashlib.sha256(public_key_bytes).hexdigest()}"


def _validate_owner(owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> None:
    """Require an external human owner, not a managed bot identity."""
    parsed_owner = MatrixID.parse(owner_user_id)
    if owner_user_id in config.bot_accounts:
        msg = "external trigger owner must not be a configured bot account"
        raise ExternalTriggerStoreError(msg)
    local_domain = extract_server_name_from_homeserver(
        constants.runtime_matrix_homeserver(runtime_paths),
        runtime_paths,
    )
    if (
        config.mindroom_user
        and parsed_owner.domain == local_domain
        and config.mindroom_user.username == parsed_owner.username
    ):
        msg = "external trigger owner must not be the MindRoom user"
        raise ExternalTriggerStoreError(msg)
    configured_entities = [constants.ROUTER_AGENT_NAME, *config.agents, *config.teams]
    matrix_state = matrix_state_for_runtime(runtime_paths)
    for entity_name in configured_entities:
        account = matrix_state.get_account(managed_account_key(entity_name))
        if account is None:
            continue
        managed_id = MatrixID.from_username(account.username, account.domain or local_domain).full_id
        if owner_user_id == managed_id:
            msg = "external trigger owner must not be a managed entity account"
            raise ExternalTriggerStoreError(msg)
    try:
        managed_account_ids = {
            identity.full_id for identity in entity_identity_registry(config, runtime_paths).current_ids.values()
        }
    except MissingManagedEntityAccountError:
        managed_account_ids = set()
    if owner_user_id in managed_account_ids:
        msg = "external trigger owner must not be a managed entity account"
        raise ExternalTriggerStoreError(msg)
    managed_localparts = {agent_username_localpart(entity_name, runtime_paths) for entity_name in configured_entities}
    if parsed_owner.domain == local_domain and parsed_owner.username in managed_localparts:
        msg = "external trigger owner must not be a managed entity account"
        raise ExternalTriggerStoreError(msg)


def _validate_target(target: ExternalTriggerTarget, config: Config, runtime_paths: RuntimePaths) -> None:
    """Require a configured entity and an already configured target room."""
    if target.agent not in config.agents and target.agent not in config.teams:
        msg = f"external trigger target references unknown agent or team: {target.agent}"
        raise ExternalTriggerStoreError(msg)
    if target.room_id in _configured_room_refs_for_entity(target.agent, config):
        return
    resolved_room_id = resolve_room_id(target.room_id, runtime_paths)
    configured_entities = configured_routable_entity_names_for_room(
        config,
        resolved_room_id,
        runtime_paths,
        room_aliases=(target.room_id,),
    )
    if target.agent not in configured_entities:
        msg = "external trigger target room must already be configured for the target entity"
        raise ExternalTriggerStoreError(msg)


def _configured_room_refs_for_entity(entity_name: str, config: Config) -> list[str]:
    if entity_name in config.agents:
        return config.agents[entity_name].rooms
    if entity_name in config.teams:
        return config.teams[entity_name].rooms
    return []


def _validate_record_update(record: ExternalTriggerRecord) -> ExternalTriggerRecord:
    """Validate a record produced through ``model_copy(update=...)``."""
    return ExternalTriggerRecord.model_validate(record.model_dump(mode="json"))
