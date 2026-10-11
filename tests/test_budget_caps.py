"""Per-user budget caps persisted outside config."""
# ruff: noqa: D103

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.budgets.caps import BudgetCaps, UserCap, budget_caps_path
from mindroom.constants import resolve_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ALICE = "@alice:example.test"
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "budgets" / "user_caps.json"


def test_caps_live_under_the_storage_root(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    assert budget_caps_path(paths) == paths.storage_root / "budgets" / "user_caps.json"


def test_missing_file_is_an_empty_store(tmp_path: Path) -> None:
    assert BudgetCaps.load(_path(tmp_path)).entries() == {}


def test_set_persists_and_reloads(tmp_path: Path) -> None:
    caps = BudgetCaps.load(_path(tmp_path))

    cap = caps.set(ALICE, 100, updated_by="budget-service", now=NOW)

    assert cap == UserCap(limit_usd=100.0, updated_at=NOW, updated_by="budget-service")
    assert caps.get(ALICE) == cap
    assert BudgetCaps.load(_path(tmp_path)).get(ALICE) == cap


def test_set_replaces_an_existing_cap(tmp_path: Path) -> None:
    caps = BudgetCaps.load(_path(tmp_path))
    caps.set(ALICE, 100, updated_by="budget-service", now=NOW)

    caps.set(ALICE, 50, updated_by="@admin:example.test", now=NOW)

    assert BudgetCaps.load(_path(tmp_path)).entries() == {
        ALICE: UserCap(limit_usd=50.0, updated_at=NOW, updated_by="@admin:example.test"),
    }


def test_delete_removes_a_cap_and_is_idempotent(tmp_path: Path) -> None:
    caps = BudgetCaps.load(_path(tmp_path))
    caps.set(ALICE, 100, updated_by="budget-service", now=NOW)

    caps.delete(ALICE)
    caps.delete(ALICE)

    assert caps.get(ALICE) is None
    assert BudgetCaps.load(_path(tmp_path)).entries() == {}


def test_concurrent_sets_for_different_users_all_persist(tmp_path: Path) -> None:
    caps = BudgetCaps.load(_path(tmp_path))
    user_ids = [f"@user{index}:example.test" for index in range(20)]
    start = threading.Barrier(len(user_ids))

    def set_cap(user_id: str) -> None:
        start.wait()
        caps.set(user_id, 50, updated_by="budget-service", now=NOW)

    threads = [threading.Thread(target=set_cap, args=(user_id,)) for user_id in user_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(caps.entries()) == set(user_ids)
    assert set(BudgetCaps.load(_path(tmp_path)).entries()) == set(user_ids)


def test_invalid_file_raises_with_its_path(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"user_caps\.json"):
        BudgetCaps.load(path)
