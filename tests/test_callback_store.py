"""Tests for the tool-minted one-shot callback store."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import ValidationError

from mindroom.callbacks.store import (
    CallbackConsumedError,
    CallbackExpiredError,
    CallbackNotFoundError,
    CallbackRecord,
    CallbackRecordNotDeliverableError,
    CallbackStore,
    CallbackStoreError,
    token_matches_hash,
)
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "@owner:example.org"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"},
    )


def _config(**policy_overrides: object) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
            "agents": {"coder": {"display_name": "Coder", "model": "default", "rooms": ["lobby"]}},
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "callback_policy": policy_overrides,
            "authorization": {
                "global_users": [_OWNER],
                "agent_reply_permissions": {"*": [_OWNER]},
            },
        },
    )


def _mint(
    store: CallbackStore,
    config: Config,
    *,
    owner: str = _OWNER,
    label: str = "issue-042 implementer",
    ttl_seconds: int | None = 3600,
    max_uses: int = 1,
    on_expiry: Literal["notify", "silent"] = "notify",
) -> tuple[CallbackRecord, str]:
    return store.mint_record(
        owner_user_id=owner,
        created_by_agent_name="coder",
        created_in_room_id="lobby",
        created_in_thread_id="$thread",
        target_room_id="lobby",
        target_thread_id="$thread",
        target_agent="coder",
        label=label,
        ttl_seconds=ttl_seconds,
        max_uses=max_uses,
        on_expiry=on_expiry,
        config=config,
    )


def _stored_record(store: CallbackStore, callback_id: str) -> CallbackRecord | None:
    return next((record for record in store.list_records() if record.callback_id == callback_id), None)


def test_mint_record_stores_hash_only_and_returns_raw_token(tmp_path: Path) -> None:
    """The raw token is returned once and only its hash is persisted."""
    store = CallbackStore(_runtime_paths(tmp_path))

    record, token = _mint(store, _config())

    assert token.startswith("mrcb_")
    assert token not in store.store_path.read_text(encoding="utf-8")
    assert token_matches_hash(token, record.token_hash)
    assert not token_matches_hash("mrcb_wrong", record.token_hash)
    assert record.callback_id.startswith("cb_")
    assert record.uses_left == 1
    assert record.on_expiry == "notify"


def test_mint_record_caps_ttl_and_uses_by_policy(tmp_path: Path) -> None:
    """Requested TTL and uses are silently capped by callback policy."""
    config = _config(default_ttl_seconds=120, max_ttl_seconds=120, max_uses_cap=2)
    store = CallbackStore(_runtime_paths(tmp_path))

    record, _token = _mint(store, config, ttl_seconds=99999, max_uses=50)

    assert record.expires_at - record.created_at == 120
    assert record.max_uses == 2
    assert record.uses_left == 2


def test_mint_record_uses_default_ttl_when_unset(tmp_path: Path) -> None:
    """Omitted TTL falls back to the policy default."""
    config = _config(default_ttl_seconds=300)
    store = CallbackStore(_runtime_paths(tmp_path))

    record, _token = _mint(store, config, ttl_seconds=None)

    assert record.expires_at - record.created_at == 300


def test_mint_record_enforces_active_per_owner_quota(tmp_path: Path) -> None:
    """The per-owner quota counts only live, unconsumed records."""
    config = _config(max_active_per_owner=1)
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store, config)

    with pytest.raises(CallbackStoreError, match="quota"):
        _mint(store, config)

    store.claim_use(record.callback_id, now=int(time.time()))
    consumed_then_minted, _token = _mint(store, config)
    assert consumed_then_minted.callback_id != record.callback_id


def test_mint_record_rejects_unknown_target_agent(tmp_path: Path) -> None:
    """Minting validates the bound target against current config."""
    config = _config()
    store = CallbackStore(_runtime_paths(tmp_path))

    with pytest.raises(CallbackStoreError, match="unknown agent or team"):
        store.mint_record(
            owner_user_id=_OWNER,
            created_by_agent_name="ghost",
            created_in_room_id="lobby",
            created_in_thread_id=None,
            target_room_id="lobby",
            target_thread_id=None,
            target_agent="ghost",
            label="ghost callback",
            ttl_seconds=60,
            max_uses=1,
            on_expiry="silent",
            config=config,
        )


def test_mint_record_rejects_bot_owner(tmp_path: Path) -> None:
    """Managed and bot identities cannot own callbacks."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
            "agents": {"coder": {"display_name": "Coder", "model": "default", "rooms": ["lobby"]}},
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "bot_accounts": ["@bot:example.org"],
            "authorization": {"global_users": [_OWNER], "agent_reply_permissions": {"*": [_OWNER]}},
        },
    )
    store = CallbackStore(_runtime_paths(tmp_path))

    with pytest.raises(CallbackStoreError, match="owner must not be a configured bot account"):
        _mint(store, config, owner="@bot:example.org")


def test_claim_use_decrements_and_marks_consumed(tmp_path: Path) -> None:
    """Claiming the last use tombstones the record instead of deleting it."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store, _config(), max_uses=2)
    now = int(time.time())

    assert store.claim_use(record.callback_id, now=now) == 1
    assert store.claim_use(record.callback_id, now=now) == 0

    tombstone = _stored_record(store, record.callback_id)
    assert tombstone is not None
    assert tombstone.uses_left == 0
    assert tombstone.consumed_at == now
    with pytest.raises(CallbackConsumedError):
        store.claim_use(record.callback_id, now=now)


def test_claim_use_rejects_expired_and_missing(tmp_path: Path) -> None:
    """Expired and unknown callbacks cannot claim a use."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store, _config(), ttl_seconds=60)

    with pytest.raises(CallbackExpiredError):
        store.claim_use(record.callback_id, now=record.expires_at)
    with pytest.raises(CallbackNotFoundError):
        store.claim_use("cb_0000000000000000", now=int(time.time()))


def test_release_use_restores_budget_and_clears_tombstone(tmp_path: Path) -> None:
    """A released use undoes the claim after delivery failure."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store, _config())
    now = int(time.time())
    store.claim_use(record.callback_id, now=now)

    store.release_use(record.callback_id)

    restored = _stored_record(store, record.callback_id)
    assert restored is not None
    assert restored.uses_left == 1
    assert restored.consumed_at is None


def test_delete_record_requires_owner_or_admin(tmp_path: Path) -> None:
    """Revocation is restricted to the owner or a trigger-family admin."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
            "agents": {"coder": {"display_name": "Coder", "model": "default", "rooms": ["lobby"]}},
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "external_trigger_policy": {"admin_users": ["@admin:example.org"]},
            "authorization": {"global_users": [_OWNER], "agent_reply_permissions": {"*": [_OWNER]}},
        },
    )
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store, config)

    with pytest.raises(CallbackStoreError, match="owner or an external trigger admin"):
        store.delete_record(record.callback_id, actor_user_id="@stranger:example.org", config=config)

    store.delete_record(record.callback_id, actor_user_id="@admin:example.org", config=config)
    assert _stored_record(store, record.callback_id) is None


def test_list_expired_returns_only_past_expiry(tmp_path: Path) -> None:
    """Expiry listing splits records on the expiry boundary."""
    store = CallbackStore(_runtime_paths(tmp_path))
    short, _token = _mint(store, _config(), ttl_seconds=60)
    _long, _token = _mint(store, _config(), ttl_seconds=3600)

    expired = store.list_expired(now=short.expires_at + 1)

    assert [record.callback_id for record in expired] == [short.callback_id]


def test_delivery_snapshot_revalidates_against_current_config(tmp_path: Path) -> None:
    """A snapshot fails closed when the stored target no longer resolves."""
    config = _config()
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store, config)

    snapshot = store.delivery_snapshot(record.callback_id, config=config, config_generation=7)
    assert snapshot is not None
    assert snapshot.callback_id == record.callback_id
    assert snapshot.config_generation == 7
    assert snapshot.target_agent == "coder"

    stale_config = _config()
    stale_config.agents["coder"].rooms = []
    with pytest.raises(CallbackRecordNotDeliverableError):
        store.delivery_snapshot(record.callback_id, config=stale_config, config_generation=8)


def test_label_must_be_short_single_line(tmp_path: Path) -> None:
    """Labels are bounded, single-line human tags."""
    store = CallbackStore(_runtime_paths(tmp_path))

    with pytest.raises(ValidationError, match="single line"):
        _mint(store, _config(), label="two\nlines")
    with pytest.raises(ValidationError, match="at most"):
        _mint(store, _config(), label="x" * 201)
