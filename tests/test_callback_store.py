"""Tests for durable single-use callback records."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.callbacks.store import (
    CallbackClaimedError,
    CallbackExpiredError,
    CallbackRecord,
    CallbackStore,
    token_matches_hash,
)
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )


def _mint(store: CallbackStore, *, label: str = "issue-042 implementer") -> tuple[CallbackRecord, str]:
    return store.mint_record(
        owner_user_id="@owner:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        agent_name="coder",
        label=label,
    )


def test_mint_stores_only_token_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minting persists a token hash and fixed expiry, never the token."""
    monkeypatch.setattr("mindroom.callbacks.store.time.time", lambda: 1000.0)
    store = CallbackStore(_runtime_paths(tmp_path))

    record, token = _mint(store)

    assert record.expires_at == 1000 + 7 * 24 * 60 * 60
    assert token.startswith("mrcb_")
    assert token not in store.store_path.read_text(encoding="utf-8")
    assert token_matches_hash(token, record.token_hash)
    assert not token_matches_hash("mrcb_wrong", record.token_hash)
    assert store.get_record(record.callback_id) == record


def test_claim_is_single_use_and_release_allows_retry(tmp_path: Path) -> None:
    """A claim excludes another fire until delivery releases it."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store)

    claimed = store.claim(record.callback_id, now=record.expires_at - 1)
    assert claimed.claimed is True
    with pytest.raises(CallbackClaimedError):
        store.claim(record.callback_id, now=record.expires_at - 1)

    store.release(record.callback_id)
    assert store.claim(record.callback_id, now=record.expires_at - 1).claimed is True


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    """The file lock permits only one concurrent delivery claim."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store)

    def claim() -> bool:
        try:
            store.claim(record.callback_id, now=record.expires_at - 1)
        except CallbackClaimedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _index: claim(), range(2))) == [False, True]


def test_expired_claim_deletes_record(tmp_path: Path) -> None:
    """Claiming an expired callback removes its stale record."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store)

    with pytest.raises(CallbackExpiredError):
        store.claim(record.callback_id, now=record.expires_at)

    assert store.get_record(record.callback_id) is None


def test_mint_prunes_expired_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minting opportunistically removes older expired callbacks."""
    store = CallbackStore(_runtime_paths(tmp_path))
    monkeypatch.setattr("mindroom.callbacks.store.time.time", lambda: 1000.0)
    expired, _token = _mint(store, label="old")
    monkeypatch.setattr("mindroom.callbacks.store.time.time", lambda: 1000.0 + 7 * 24 * 60 * 60)

    current, _token = _mint(store, label="new")

    assert store.get_record(expired.callback_id) is None
    assert store.get_record(current.callback_id) == current


def test_delete_removes_record(tmp_path: Path) -> None:
    """Delivered or rolled-back callbacks can be deleted."""
    store = CallbackStore(_runtime_paths(tmp_path))
    record, _token = _mint(store)

    store.delete(record.callback_id)

    assert store.get_record(record.callback_id) is None


def test_label_is_short_and_single_line(tmp_path: Path) -> None:
    """Labels remain bounded human-readable tags."""
    store = CallbackStore(_runtime_paths(tmp_path))

    with pytest.raises(ValidationError, match="single line"):
        _mint(store, label="two\nlines")
    with pytest.raises(ValidationError, match="at most"):
        _mint(store, label="x" * 201)
