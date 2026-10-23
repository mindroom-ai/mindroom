"""Tests for the agent rename stopped-runtime guard and backend check."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_rename.guard import require_supported_backends, stopped_runtime
from mindroom.agent_rename.steps import RenameRefusedError
from mindroom.config.main import Config
from mindroom.constants import tracking_dir
from mindroom.event_journal.opening import event_journal_in_use_lock_path
from mindroom.file_locks import (
    acquire_shared_file_lock,
    release_file_lock,
    try_exclusive_file_lock,
)
from tests.agent_rename_helpers import tree_snapshot
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_refuses_when_runtime_lock_held(tmp_path: Path) -> None:
    """A running orchestrator holds runtime.lock, so the guard refuses and names it."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_lock_path = tracking_dir(runtime_paths) / "runtime.lock"
    runtime_lock = try_exclusive_file_lock(runtime_lock_path)
    assert runtime_lock is not None
    try:
        with pytest.raises(RenameRefusedError) as raised, stopped_runtime(runtime_paths):
            pytest.fail("the guard must not enter while runtime.lock is held")
    finally:
        release_file_lock(runtime_lock)

    assert str(raised.value) == f"Stop MindRoom first: another process holds {runtime_lock_path}."
    # A refused guard must not keep either lock.
    reacquired = try_exclusive_file_lock(runtime_lock_path)
    assert reacquired is not None
    release_file_lock(reacquired)


def test_refuses_when_journal_in_use(tmp_path: Path) -> None:
    """An open journal holds the in-use lock shared, so the guard refuses and frees runtime.lock again."""
    runtime_paths = test_runtime_paths(tmp_path)
    in_use_path = event_journal_in_use_lock_path(runtime_paths.storage_root)
    journal_claim = acquire_shared_file_lock(in_use_path)
    try:
        with pytest.raises(RenameRefusedError) as raised, stopped_runtime(runtime_paths):
            pytest.fail("the guard must not enter while the journal is in use")
    finally:
        release_file_lock(journal_claim)

    assert str(raised.value) == f"Stop MindRoom first: another process holds {in_use_path}."
    runtime_lock = try_exclusive_file_lock(tracking_dir(runtime_paths) / "runtime.lock")
    assert runtime_lock is not None
    release_file_lock(runtime_lock)


def test_holds_locks_until_exit(tmp_path: Path) -> None:
    """MindRoom cannot start while the guard is held and can start again once it exits."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_lock_path = tracking_dir(runtime_paths) / "runtime.lock"
    in_use_path = event_journal_in_use_lock_path(runtime_paths.storage_root)

    with stopped_runtime(runtime_paths):
        assert try_exclusive_file_lock(runtime_lock_path) is None
        # The journal opens under a shared lock, which an exclusive holder blocks.
        assert try_exclusive_file_lock(in_use_path) is None

    for path in (runtime_lock_path, in_use_path):
        lock = try_exclusive_file_lock(path)
        assert lock is not None, path
        release_file_lock(lock)


def test_releases_locks_when_the_body_raises(tmp_path: Path) -> None:
    """A failing rename must not leave MindRoom blocked."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_lock_path = tracking_dir(runtime_paths) / "runtime.lock"

    def fail_inside_guard() -> None:
        with stopped_runtime(runtime_paths):
            msg = "boom"
            raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        fail_inside_guard()

    lock = try_exclusive_file_lock(runtime_lock_path)
    assert lock is not None
    release_file_lock(lock)


def test_creates_only_the_tracking_directory_and_lock_files(tmp_path: Path) -> None:
    """A fresh install has no tracking directory; the guard adds it and its two lock files, nothing else."""
    runtime_paths = test_runtime_paths(tmp_path)
    before = tree_snapshot(tmp_path)
    assert "mindroom_data/tracking" not in before

    with stopped_runtime(runtime_paths):
        pass

    after = tree_snapshot(tmp_path)
    assert set(after) - set(before) == {
        "mindroom_data",
        "mindroom_data/tracking",
        "mindroom_data/tracking/runtime.lock",
        "mindroom_data/tracking/event_journal_store.lock",
    }
    assert after["mindroom_data/tracking/runtime.lock"] == b""
    assert after["mindroom_data/tracking/event_journal_store.lock"] == b""


def test_accepts_sqlite_journal_and_supported_workers(tmp_path: Path) -> None:
    """The default install and a docker worker install are both supported."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config()

    require_supported_backends(config, runtime_paths)
    require_supported_backends(
        config,
        replace(runtime_paths, process_env={**runtime_paths.process_env, "MINDROOM_WORKER_BACKEND": "docker"}),
    )
    require_supported_backends(
        config,
        replace(runtime_paths, process_env={**runtime_paths.process_env, "MINDROOM_WORKER_BACKEND": "static_runner"}),
    )


def test_refuses_postgres_journal(tmp_path: Path) -> None:
    """Journal rows in PostgreSQL are not rewritten yet."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.model_validate({"event_journal": {"backend": "postgres"}})

    with pytest.raises(RenameRefusedError, match=r"event_journal\.backend.*postgres.*not supported yet"):
        require_supported_backends(config, runtime_paths)


def test_refuses_kubernetes_workers(tmp_path: Path) -> None:
    """Worker state in Kubernetes volumes is not reachable from here."""
    runtime_paths = test_runtime_paths(tmp_path)
    kubernetes = replace(runtime_paths, process_env={**runtime_paths.process_env, "MINDROOM_WORKER_BACKEND": "k8s"})

    with pytest.raises(RenameRefusedError, match=r"worker backend.*kubernetes.*not supported yet"):
        require_supported_backends(Config(), kubernetes)


def test_unknown_worker_backend_surfaces_as_a_refusal(tmp_path: Path) -> None:
    """A mistyped MINDROOM_WORKER_BACKEND is reported to the operator, not as a traceback."""
    runtime_paths = test_runtime_paths(tmp_path)
    unknown = replace(runtime_paths, process_env={**runtime_paths.process_env, "MINDROOM_WORKER_BACKEND": "bogus"})

    with pytest.raises(RenameRefusedError, match="Unsupported worker backend: bogus"):
        require_supported_backends(Config(), unknown)
