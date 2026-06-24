"""Tests for tool-managed external trigger store."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.external_triggers.store import (
    ExternalTriggerRecord,
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    ExternalTriggerTarget,
)

if TYPE_CHECKING:
    from pathlib import Path

_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_OWNER = "@owner:example.org"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )


def _config(**policy_overrides: object) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
            "agents": {"watcher": {"display_name": "Watcher", "model": "default", "rooms": ["lobby"]}},
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "external_trigger_policy": policy_overrides,
            "authorization": {
                "global_users": [_OWNER],
                "agent_reply_permissions": {"*": [_OWNER]},
            },
        },
    )


def _target(room_id: str = "lobby", agent: str = "watcher") -> ExternalTriggerTarget:
    return ExternalTriggerTarget(room_id=room_id, agent=agent)


def _create(store: ExternalTriggerStore, config: Config, trigger_id: str = "campground") -> ExternalTriggerRecord:
    return store.create_record(
        trigger_id=trigger_id,
        owner_user_id=_OWNER,
        created_by_agent_name="watcher",
        created_in_room_id="!room:example.org",
        created_in_thread_id="$thread",
        target=_target(),
        public_key=_PUBLIC_KEY,
        key_id="default",
        description="campground watcher",
        allowed_kinds=["campground.availability"],
        config=config,
    )


def test_create_record_assigns_uid_version_and_auth_epoch(tmp_path: Path) -> None:
    """Created records get a stable uid and first auth scope."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))

    record = _create(store, _config())

    assert record.uid
    assert record.version == 1
    assert record.auth_epoch == 1
    assert record.public_key_fingerprint.startswith("sha256:")


def test_trigger_id_must_be_route_safe(tmp_path: Path) -> None:
    """Trigger ids must be safe path components."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))

    with pytest.raises(ExternalTriggerStoreError, match="trigger_id"):
        _create(store, _config(), trigger_id="../bad")


def test_quota_checked_under_lock(tmp_path: Path) -> None:
    """Owner quota is enforced during the locked write."""
    config = _config(max_triggers_per_owner=1)
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    _create(store, config, trigger_id="one")

    with pytest.raises(ExternalTriggerStoreError, match="quota"):
        _create(store, config, trigger_id="two")


def test_rotate_key_increments_auth_epoch(tmp_path: Path) -> None:
    """Key rotation advances both record version and auth epoch."""
    config = _config()
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    record = _create(store, config)

    rotated = store.rotate_key(
        record.trigger_id,
        public_key="AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
        key_id="rotated",
        actor_user_id=_OWNER,
        config=config,
    )

    assert rotated.version == record.version + 1
    assert rotated.auth_epoch == record.auth_epoch + 1
    assert rotated.key_id == "rotated"


def test_metadata_update_increments_version_not_auth_epoch(tmp_path: Path) -> None:
    """Metadata updates should not invalidate replay scope."""
    config = _config()
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    record = _create(store, config)

    disabled = store.set_enabled(record.trigger_id, enabled=False, actor_user_id=_OWNER, config=config)

    assert disabled.version == record.version + 1
    assert disabled.auth_epoch == record.auth_epoch
    assert disabled.enabled is False


def test_delete_recreate_gets_new_uid(tmp_path: Path) -> None:
    """Deleting and recreating a trigger id should produce a new uid."""
    config = _config()
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    first = _create(store, config)

    store.delete_record(first.trigger_id, actor_user_id=_OWNER, config=config)
    second = _create(store, config)

    assert second.uid != first.uid


def test_delivery_snapshot_freezes_record_and_config_generation(tmp_path: Path) -> None:
    """Delivery snapshots cap record limits against current policy."""
    config = _config(
        default_replay_window_seconds=120,
        max_replay_window_seconds=120,
        default_max_body_bytes=4096,
        max_body_bytes=4096,
    )
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    record = store.create_record(
        trigger_id="campground",
        owner_user_id=_OWNER,
        created_by_agent_name="watcher",
        created_in_room_id="!room:example.org",
        created_in_thread_id=None,
        target=_target(),
        public_key=_PUBLIC_KEY,
        replay_window_seconds=300,
        max_body_bytes=65536,
        config=config,
    )

    snapshot = store.delivery_snapshot(record.trigger_id, config=config, config_generation=42)

    assert snapshot is not None
    assert snapshot.config_generation == 42
    assert snapshot.replay_scope == f"{record.uid}:{record.auth_epoch}"
    assert snapshot.replay_window_seconds == 120
    assert snapshot.max_body_bytes == 4096
    assert snapshot.resolved_room_id == "lobby"


def test_store_rejects_unknown_target(tmp_path: Path) -> None:
    """Trigger targets must reference configured agents or teams."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))

    with pytest.raises(ExternalTriggerStoreError, match="unknown"):
        store.create_record(
            trigger_id="campground",
            owner_user_id=_OWNER,
            created_by_agent_name="watcher",
            created_in_room_id="!room:example.org",
            created_in_thread_id=None,
            target=_target(agent="missing"),
            public_key=_PUBLIC_KEY,
            config=_config(),
        )


def test_store_rejects_unconfigured_target_room(tmp_path: Path) -> None:
    """Trigger target rooms must already be configured for the target entity."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))

    with pytest.raises(ExternalTriggerStoreError, match="target room"):
        store.create_record(
            trigger_id="campground",
            owner_user_id=_OWNER,
            created_by_agent_name="watcher",
            created_in_room_id="!room:example.org",
            created_in_thread_id=None,
            target=_target(room_id="other"),
            public_key=_PUBLIC_KEY,
            config=_config(),
        )
