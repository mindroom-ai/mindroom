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
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from pathlib import Path

_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_OWNER = "@owner:example.org"


def _runtime_paths(tmp_path: Path, *, server_name: str = "example.org") -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"MATRIX_HOMESERVER": f"https://{server_name}", "MATRIX_SERVER_NAME": server_name},
    )


def _config(
    *,
    bot_accounts: list[str] | None = None,
    mindroom_user: dict[str, str] | None = None,
    **policy_overrides: object,
) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
            "agents": {"watcher": {"display_name": "Watcher", "model": "default", "rooms": ["lobby"]}},
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "external_trigger_policy": policy_overrides,
            "bot_accounts": bot_accounts or [],
            "mindroom_user": mindroom_user,
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


def test_store_accepts_federated_owner_with_managed_localpart(tmp_path: Path) -> None:
    """Human owners on other homeservers should not collide with local bot localparts."""
    runtime_paths = _runtime_paths(tmp_path, server_name="example.org")
    store = ExternalTriggerStore(runtime_paths)

    record = store.create_record(
        trigger_id="campground",
        owner_user_id="@mindroom_watcher:other.org",
        created_by_agent_name="watcher",
        created_in_room_id="!room:example.org",
        created_in_thread_id=None,
        target=_target(),
        public_key=_PUBLIC_KEY,
        config=_config(),
    )

    assert record.owner_user_id == "@mindroom_watcher:other.org"


def test_store_rejects_local_generated_managed_owner_before_account_exists(tmp_path: Path) -> None:
    """Predictable local managed-account IDs cannot own trigger records before state exists."""
    runtime_paths = _runtime_paths(tmp_path, server_name="example.org")
    store = ExternalTriggerStore(runtime_paths)

    with pytest.raises(ExternalTriggerStoreError, match="managed entity"):
        store.create_record(
            trigger_id="campground",
            owner_user_id="@mindroom_watcher:example.org",
            created_by_agent_name="watcher",
            created_in_room_id="!room:example.org",
            created_in_thread_id=None,
            target=_target(),
            public_key=_PUBLIC_KEY,
            config=_config(),
        )


def test_store_rejects_persisted_managed_account_owner(tmp_path: Path) -> None:
    """Persisted managed Matrix accounts cannot own trigger records."""
    runtime_paths = _runtime_paths(tmp_path, server_name="example.org")
    matrix_state = MatrixState()
    matrix_state.add_account(
        managed_account_key("watcher"),
        username="custom_watcher",
        password="secret",  # noqa: S106 - test Matrix state fixture only.
        domain="example.org",
    )
    matrix_state.save(runtime_paths)
    store = ExternalTriggerStore(runtime_paths)

    with pytest.raises(ExternalTriggerStoreError, match="managed entity"):
        store.create_record(
            trigger_id="campground",
            owner_user_id="@custom_watcher:example.org",
            created_by_agent_name="watcher",
            created_in_room_id="!room:example.org",
            created_in_thread_id=None,
            target=_target(),
            public_key=_PUBLIC_KEY,
            config=_config(),
        )


def test_store_rejects_configured_bot_account_owner(tmp_path: Path) -> None:
    """Configured bot accounts cannot own trigger records."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))

    with pytest.raises(ExternalTriggerStoreError, match="bot account"):
        store.create_record(
            trigger_id="campground",
            owner_user_id="@bridgebot:example.org",
            created_by_agent_name="watcher",
            created_in_room_id="!room:example.org",
            created_in_thread_id=None,
            target=_target(),
            public_key=_PUBLIC_KEY,
            config=_config(bot_accounts=["@bridgebot:example.org"]),
        )


def test_store_rejects_local_mindroom_user_but_allows_federated_same_localpart(tmp_path: Path) -> None:
    """Only the local MindRoom user localpart is reserved."""
    runtime_paths = _runtime_paths(tmp_path, server_name="example.org")
    config = _config(mindroom_user={"username": "mindroom_user"})
    store = ExternalTriggerStore(runtime_paths)

    with pytest.raises(ExternalTriggerStoreError, match="MindRoom user"):
        store.create_record(
            trigger_id="local",
            owner_user_id="@mindroom_user:example.org",
            created_by_agent_name="watcher",
            created_in_room_id="!room:example.org",
            created_in_thread_id=None,
            target=_target(),
            public_key=_PUBLIC_KEY,
            config=config,
        )

    record = store.create_record(
        trigger_id="federated",
        owner_user_id="@mindroom_user:other.org",
        created_by_agent_name="watcher",
        created_in_room_id="!room:example.org",
        created_in_thread_id=None,
        target=_target(),
        public_key=_PUBLIC_KEY,
        config=config,
    )

    assert record.owner_user_id == "@mindroom_user:other.org"


def test_record_store_write_fsync_failure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record writes must report success only after file contents are durable."""

    def raise_disk_full(_fd: int) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("os.fsync", raise_disk_full)
    store = ExternalTriggerStore(_runtime_paths(tmp_path))

    with pytest.raises(ExternalTriggerStoreError, match="unavailable"):
        _create(store, _config())


def test_corrupt_record_store_fails_closed(tmp_path: Path) -> None:
    """Corrupt record JSON should not be treated as an empty trigger store."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    store.store_path.parent.mkdir(parents=True, exist_ok=True)
    store.store_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ExternalTriggerStoreError, match="invalid"):
        store.list_records()


def test_record_store_read_oserror_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Store path stat failures should map to the store-unavailable boundary."""
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    original_exists = type(store.store_path).exists

    def raise_for_store_path(path: Path) -> bool:
        if path == store.store_path:
            msg = "permission denied"
            raise OSError(msg)
        return original_exists(path)

    monkeypatch.setattr(type(store.store_path), "exists", raise_for_store_path)

    with pytest.raises(ExternalTriggerStoreError, match="invalid"):
        store.list_records()


def test_non_owner_cannot_modify_trigger_but_admin_can(tmp_path: Path) -> None:
    """Record mutation requires trigger ownership or configured trigger admin."""
    config = _config(admin_users=["@admin:example.org"])
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    record = _create(store, config)

    with pytest.raises(ExternalTriggerStoreError, match="owner"):
        store.set_enabled(record.trigger_id, enabled=False, actor_user_id="@other:example.org", config=config)

    updated = store.set_enabled(record.trigger_id, enabled=False, actor_user_id="@admin:example.org", config=config)

    assert updated.enabled is False
