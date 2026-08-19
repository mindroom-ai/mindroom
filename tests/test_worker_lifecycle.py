"""Tests for backend-neutral worker lifecycle helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mindroom.script_runs.models import script_worker_key_for_run
from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import WorkerBackendError, effective_idle_status, filter_and_sort_worker_handles
from mindroom.workers.backends import _metadata_store as metadata_store_module
from mindroom.workers.backends import local as local_module
from mindroom.workers.backends._lifecycle import (
    WorkerLifecycleState,
    mark_worker_failed,
    mark_worker_idle,
    touch_worker_lifecycle,
)
from mindroom.workers.backends._metadata_store import save_worker_metadata
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerStatus


def _handle(worker_key: str, *, status: WorkerStatus, last_used_at: float) -> WorkerHandle:
    return WorkerHandle(
        worker_id=f"worker-{worker_key}",
        worker_key=worker_key,
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=None,
        status=status,
        backend_name="test",
        last_used_at=last_used_at,
        created_at=0.0,
    )


def test_effective_idle_status_only_marks_ready_workers_idle_at_timeout_boundary() -> None:
    """Idle timeout presentation should only affect ready workers at or beyond the timeout."""
    assert effective_idle_status("ready", 10.0, 5.0, 14.99) == "ready"
    assert effective_idle_status("ready", 10.0, 5.0, 15.0) == "idle"
    assert effective_idle_status("starting", 10.0, 5.0, 20.0) == "starting"
    assert effective_idle_status("failed", 10.0, 5.0, 20.0) == "failed"


def test_filter_and_sort_worker_handles_hides_idle_workers_and_orders_by_recent_use() -> None:
    """Worker lists should preserve existing idle filtering and newest-first ordering."""
    handles = [
        _handle("old-ready", status="ready", last_used_at=10.0),
        _handle("idle", status="idle", last_used_at=30.0),
        _handle("new-ready", status="ready", last_used_at=20.0),
    ]

    assert [handle.worker_key for handle in filter_and_sort_worker_handles(handles, True)] == [
        "idle",
        "new-ready",
        "old-ready",
    ]
    assert [handle.worker_key for handle in filter_and_sort_worker_handles(handles, False)] == [
        "new-ready",
        "old-ready",
    ]


def test_touch_revives_idle_worker_to_ready() -> None:
    """Touching an idle worker brings it back to ready and refreshes last-used."""
    state = WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="idle")
    revived = touch_worker_lifecycle(state, now=50.0)
    assert revived.status == "ready"
    assert revived.last_used_at == 50.0


def test_touch_clears_stale_failure_reason_when_not_failed() -> None:
    """Reviving an idle worker clears a stale failure reason but keeps the count."""
    state = WorkerLifecycleState(
        created_at=1.0,
        last_used_at=1.0,
        status="idle",
        failure_count=2,
        failure_reason="boom",
    )
    revived = touch_worker_lifecycle(state, now=50.0)
    assert revived.status == "ready"
    assert revived.failure_reason is None
    assert revived.failure_count == 2


def test_touch_keeps_failed_status_and_reason() -> None:
    """A failed worker is not revived by a touch."""
    state = WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="failed", failure_reason="boom")
    touched = touch_worker_lifecycle(state, now=50.0)
    assert touched.status == "failed"
    assert touched.failure_reason == "boom"


def test_mark_idle_clears_failure_reason() -> None:
    """Idling a worker clears any leftover failure reason."""
    state = WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="ready", failure_reason="boom")
    idled = mark_worker_idle(state)
    assert idled.status == "idle"
    assert idled.failure_reason is None


def test_mark_failed_increments_count_and_records_reason() -> None:
    """Failing a worker records the reason and increments the failure count."""
    state = WorkerLifecycleState(created_at=1.0, last_used_at=1.0, status="ready", failure_count=1)
    failed = mark_worker_failed(state, now=9.0, failure_reason="kaboom")
    assert failed.status == "failed"
    assert failed.failure_count == 2
    assert failed.failure_reason == "kaboom"
    assert failed.last_used_at == 9.0


def test_static_backend_touch_revives_idle_worker() -> None:
    """The static backend adopts the helper: a touch revives an idled worker."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://runner",
        auth_token="tok",  # noqa: S106
        idle_timeout_seconds=10.0,
    )
    backend.ensure_worker(WorkerSpec(worker_key="v1:t:shared:a"), now=0.0)

    idled = backend.cleanup_idle_workers(now=11.0)[0]
    assert idled.status == "idle"

    touched = backend.touch_worker("v1:t:shared:a", now=12.0)
    assert touched is not None
    assert touched.status == "ready"


def test_static_backend_touch_does_not_revive_failed_worker() -> None:
    """Touching a failed static worker keeps it failed (only idle revives)."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://runner",
        auth_token="tok",  # noqa: S106
        idle_timeout_seconds=10.0,
    )
    backend.ensure_worker(WorkerSpec(worker_key="v1:t:shared:a"), now=0.0)
    backend.record_failure("v1:t:shared:a", "boom", now=0.0)

    touched = backend.touch_worker("v1:t:shared:a", now=1.0)
    assert touched is not None
    assert touched.status == "failed"
    assert touched.failure_reason == "boom"


def test_local_backend_touch_revives_idle_worker_and_clears_failure(tmp_path: Path) -> None:
    """The local backend adopts the helper: a touch revives idle and clears stale failure."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    worker_key = "v1:t:shared:a"
    paths = local_module._local_worker_state_paths(worker_key, worker_root=backend.worker_root)
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id="w",
            worker_key=worker_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
            failure_reason="boom",
        ),
    )

    touched = backend.touch_worker(worker_key, now=5.0)
    assert touched is not None
    assert touched.status == "ready"
    assert touched.failure_reason is None


def test_static_backend_retires_only_one_exact_run_worker_idempotently() -> None:
    """Static retirement forgets one harmless local handle without touching ordinary entries."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://runner",
        auth_token="tok",  # noqa: S106
    )
    base_key = "v1:t:user_agent:alice:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'a' * 32}")
    ordinary_keys = (
        "v1:t:shared:watcher",
        "v1:t:user:alice",
        base_key,
    )
    for worker_key in (*ordinary_keys, run_key):
        backend.ensure_worker(WorkerSpec(worker_key), now=1.0)

    backend.retire_worker(run_key)
    backend.retire_worker(run_key)

    assert {handle.worker_key for handle in backend.list_workers()} == set(ordinary_keys)


def test_local_backend_retires_only_one_exact_run_worker_root_idempotently(tmp_path: Path) -> None:
    """Local retirement removes one run root while preserving an ordinary user-agent root."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    base_key = "v1:t:user_agent:alice:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'b' * 32}")
    for worker_key in (base_key, run_key):
        paths = local_module._local_worker_state_paths(worker_key, worker_root=backend.worker_root)
        paths.workspace.mkdir(parents=True)
        save_worker_metadata(
            paths,
            local_module._LocalWorkerMetadata(
                worker_id=worker_dir_name(worker_key),
                worker_key=worker_key,
                endpoint="/api/sandbox-runner/execute",
                backend_name=backend.backend_name,
                created_at=0.0,
                last_used_at=0.0,
                status="idle",
            ),
        )

    backend.retire_worker(run_key)
    backend.retire_worker(run_key)

    assert not local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root).root.exists()
    assert local_module._local_worker_state_paths(base_key, worker_root=backend.worker_root).root.is_dir()
    assert [handle.worker_key for handle in backend.list_workers()] == [base_key]


def test_local_backend_fsyncs_staged_retirement_identity_before_canonical_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh retirement must sync the exact identity inode before removing its canonical name."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'8' * 32}")
    paths = local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root)
    paths.workspace.mkdir(parents=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id=paths.root.name,
            worker_key=run_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
        ),
    )
    events: list[str] = []
    original_fsync = metadata_store_module.os.fsync
    original_unlink = metadata_store_module.os.unlink

    def track_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            events.append("identity_fsync")
        original_fsync(descriptor)

    def track_unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if path == paths.metadata_file.name:
            events.append("canonical_unlink")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(metadata_store_module.os, "fsync", track_fsync)
    monkeypatch.setattr(metadata_store_module.os, "unlink", track_unlink)

    backend.retire_worker(run_key)

    assert events.index("identity_fsync") < events.index("canonical_unlink")


def test_local_backend_fsyncs_adopted_retirement_identity_before_canonical_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry must sync a pre-existing retirement identity before removing its canonical hard link."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'9' * 32}")
    paths = local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root)
    paths.workspace.mkdir(parents=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id=paths.root.name,
            worker_key=run_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
        ),
    )
    original_unlink = metadata_store_module.os.unlink

    def interrupt_after_staging(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if path == paths.metadata_file.name:
            msg = "injected interruption after retirement identity staging"
            raise OSError(msg)
        original_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as staging_fault:
        staging_fault.setattr(metadata_store_module.os, "unlink", interrupt_after_staging)
        with pytest.raises(WorkerBackendError, match="injected interruption"):
            backend.retire_worker(run_key)

    assert paths.metadata_file.is_file()
    assert len(tuple(paths.root.parent.glob(f".{paths.root.name}.retirement-identity.*"))) == 1

    events: list[str] = []
    original_fsync = metadata_store_module.os.fsync

    def track_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            events.append("identity_fsync")
        original_fsync(descriptor)

    def track_unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if path == paths.metadata_file.name:
            events.append("canonical_unlink")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(metadata_store_module.os, "fsync", track_fsync)
    monkeypatch.setattr(metadata_store_module.os, "unlink", track_unlink)
    retry_backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )

    retry_backend.retire_worker(run_key)

    assert events.index("identity_fsync") < events.index("canonical_unlink")


def test_local_backend_fsyncs_adopted_retirement_identity_name_before_canonical_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry must commit an uncommitted sidecar name before removing its canonical hard link."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'7' * 32}")
    paths = local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root)
    paths.workspace.mkdir(parents=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id=paths.root.name,
            worker_key=run_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
        ),
    )
    worker_parent = paths.root.parent.stat()
    original_fsync = metadata_store_module.os.fsync
    fail_parent_sync = True

    def interrupt_first_parent_sync(descriptor: int) -> None:
        nonlocal fail_parent_sync
        metadata = os.fstat(descriptor)
        if (
            fail_parent_sync
            and stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == worker_parent.st_dev
            and metadata.st_ino == worker_parent.st_ino
        ):
            fail_parent_sync = False
            msg = "injected worker-parent sync failure"
            raise OSError(msg)
        original_fsync(descriptor)

    with monkeypatch.context() as staging_fault:
        staging_fault.setattr(metadata_store_module.os, "fsync", interrupt_first_parent_sync)
        with pytest.raises(WorkerBackendError, match="worker-parent sync failure"):
            backend.retire_worker(run_key)

    assert fail_parent_sync is False
    assert paths.metadata_file.is_file()
    assert len(tuple(paths.root.parent.glob(f".{paths.root.name}.retirement-identity.*"))) == 1

    events: list[str] = []
    original_unlink = metadata_store_module.os.unlink

    def track_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == worker_parent.st_dev
            and metadata.st_ino == worker_parent.st_ino
        ):
            events.append("worker_parent_fsync")
        original_fsync(descriptor)

    def track_unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if path == paths.metadata_file.name:
            events.append("canonical_unlink")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(metadata_store_module.os, "fsync", track_fsync)
    monkeypatch.setattr(metadata_store_module.os, "unlink", track_unlink)
    retry_backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )

    retry_backend.retire_worker(run_key)

    assert events.index("worker_parent_fsync") < events.index("canonical_unlink")


def test_local_backend_binds_adopted_identity_validation_and_sync_for_fresh_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-inode overwrite at adoption cannot poison the sidecar retained by a late failure."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'6' * 32}")
    paths = local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root)
    paths.workspace.mkdir(parents=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id=paths.root.name,
            worker_key=run_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
        ),
    )
    original_unlink = metadata_store_module.os.unlink

    def interrupt_after_staging(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if path == paths.metadata_file.name:
            msg = "injected interruption after retirement identity staging"
            raise OSError(msg)
        original_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as staging_fault:
        staging_fault.setattr(metadata_store_module.os, "unlink", interrupt_after_staging)
        with pytest.raises(WorkerBackendError, match="injected interruption"):
            backend.retire_worker(run_key)

    sidecar = next(paths.root.parent.glob(f".{paths.root.name}.retirement-identity.*"))
    original_open = metadata_store_module.os.open
    original_rmdir = metadata_store_module.os.rmdir
    sidecar_opens = 0
    fail_root_once = True

    def mutate_same_inode_before_second_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal sidecar_opens
        if path == sidecar.name:
            sidecar_opens += 1
            if sidecar_opens == 2:
                before = sidecar.stat()
                sidecar.write_text('{"worker_key":"substituted"}', encoding="utf-8")
                after = sidecar.stat()
                assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def fail_first_worker_root_rmdir(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal fail_root_once
        if path == paths.root.name and fail_root_once:
            fail_root_once = False
            msg = "injected final worker-root removal failure"
            raise OSError(msg)
        original_rmdir(path, dir_fd=dir_fd)

    with monkeypatch.context() as retirement_faults:
        retirement_faults.setattr(metadata_store_module.os, "open", mutate_same_inode_before_second_open)
        retirement_faults.setattr(metadata_store_module.os, "rmdir", fail_first_worker_root_rmdir)
        with pytest.raises(WorkerBackendError, match="final worker-root removal failure"):
            backend.retire_worker(run_key)

    assert fail_root_once is False
    retry_backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )

    retry_backend.retire_worker(run_key)

    assert paths.root.exists() is False
    assert tuple(paths.root.parent.iterdir()) == ()


def test_local_backend_refuses_symlinked_run_root_with_malformed_target_metadata(tmp_path: Path) -> None:
    """Malformed metadata cannot turn a run-key symlink into authority to delete another worker root."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    base_key = "v1:t:user_agent:alice:watcher"
    run_key = script_worker_key_for_run(base_key, f"script-{'0' * 32}")
    target_root = backend.worker_root / worker_dir_name(base_key)
    (target_root / "metadata").mkdir(parents=True)
    (target_root / "metadata" / "worker.json").write_text("{malformed", encoding="utf-8")
    sentinel = target_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (backend.worker_root / worker_dir_name(run_key)).symlink_to(target_root, target_is_directory=True)

    with pytest.raises(WorkerBackendError, match="symbolic link"):
        backend.retire_worker(run_key)

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("metadata_contents", [None, "{malformed"])
def test_local_backend_refuses_existing_run_root_without_exact_identity(
    tmp_path: Path,
    metadata_contents: str | None,
) -> None:
    """An existing local state root needs readable exact-key metadata before recursive deletion."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'2' * 32}")
    state_root = backend.worker_root / worker_dir_name(run_key)
    state_root.mkdir()
    sentinel = state_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    if metadata_contents is not None:
        metadata_file = state_root / "metadata" / "worker.json"
        metadata_file.parent.mkdir()
        metadata_file.write_text(metadata_contents, encoding="utf-8")

    with pytest.raises(WorkerBackendError, match="identity metadata"):
        backend.retire_worker(run_key)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_local_backend_refuses_worker_root_swapped_after_identity_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement directory cannot be recursively deleted after the exact root was validated."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'3' * 32}")
    paths = local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root)
    paths.workspace.mkdir(parents=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id=worker_dir_name(run_key),
            worker_key=run_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
        ),
    )
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    sentinel = replacement_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    retired_root = tmp_path / "retired-original"
    swapped = False
    original_stat = metadata_store_module.os.stat

    def stat_after_swap(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> object:
        nonlocal swapped
        if path == paths.root.name and dir_fd is not None and not swapped:
            descriptor_root = Path(f"/proc/self/fd/{dir_fd}").resolve()
            if descriptor_root == backend.worker_root:
                paths.root.rename(retired_root)
                replacement_root.rename(paths.root)
                replacement_root.symlink_to(paths.root, target_is_directory=True)
                swapped = True
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(metadata_store_module.os, "stat", stat_after_swap)

    with pytest.raises(WorkerBackendError, match="changed during retirement"):
        backend.retire_worker(run_key)

    assert swapped is True
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("failure_point", ["binding", "validation"])
def test_local_backend_closes_trusted_root_fd_when_initial_binding_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    """Repeated initial trust failures cannot leak the successfully opened root descriptor."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'5' * 32}")

    def fail_trusted_root(*_args: object, **_kwargs: object) -> object:
        msg = f"injected trusted-root {failure_point} failure"
        raise ValueError(msg)

    monkeypatch.setattr(
        metadata_store_module,
        "_trusted_root_binding" if failure_point == "binding" else "_validate_trusted_root",
        fail_trusted_root,
    )
    fd_root = Path("/proc/self/fd")
    descriptors_before = len(tuple(fd_root.iterdir()))

    for _attempt in range(20):
        with pytest.raises(WorkerBackendError, match=f"injected trusted-root {failure_point} failure"):
            backend.retire_worker(run_key)

    assert len(tuple(fd_root.iterdir())) == descriptors_before


def test_local_backend_closes_worker_fd_when_open_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-open worker-directory failure cannot leak the not-yet-returned descriptor."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'7' * 32}")
    state_root = backend.worker_root / worker_dir_name(run_key)
    state_root.mkdir()
    original_fstat = metadata_store_module.os.fstat

    def fail_worker_fstat(descriptor: int) -> object:
        descriptor_root = Path(f"/proc/self/fd/{descriptor}").resolve()
        if descriptor_root == state_root:
            msg = "injected worker-directory validation failure"
            raise OSError(msg)
        return original_fstat(descriptor)

    monkeypatch.setattr(metadata_store_module.os, "fstat", fail_worker_fstat)
    fd_root = Path("/proc/self/fd")
    descriptors_before = len(tuple(fd_root.iterdir()))

    for _attempt in range(20):
        with pytest.raises(WorkerBackendError, match="injected worker-directory validation failure"):
            backend.retire_worker(run_key)

    assert len(tuple(fd_root.iterdir())) == descriptors_before


def test_local_backend_normalizes_retirement_recursion_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A depth failure crosses the Local backend boundary as a typed retryable error."""
    backend = local_module._LocalWorkerBackend(
        worker_root=tmp_path / "workers",
        api_root="/api/sandbox-runner",
        idle_timeout_seconds=1800.0,
    )
    run_key = script_worker_key_for_run("v1:t:user_agent:alice:watcher", f"script-{'6' * 32}")
    paths = local_module._local_worker_state_paths(run_key, worker_root=backend.worker_root)
    paths.workspace.mkdir(parents=True)
    save_worker_metadata(
        paths,
        local_module._LocalWorkerMetadata(
            worker_id=paths.root.name,
            worker_key=run_key,
            endpoint="/api/sandbox-runner/execute",
            backend_name=backend.backend_name,
            created_at=0.0,
            last_used_at=0.0,
            status="idle",
        ),
    )
    exact_identity = paths.metadata_file.read_bytes()
    original_listdir = metadata_store_module.os.listdir

    def fail_worker_traversal(path: int) -> list[str]:
        descriptor_root = Path(f"/proc/self/fd/{path}").resolve()
        if descriptor_root == paths.root:
            msg = "injected retirement depth failure"
            raise RecursionError(msg)
        return original_listdir(path)

    monkeypatch.setattr(metadata_store_module.os, "listdir", fail_worker_traversal)

    with pytest.raises(WorkerBackendError, match="injected retirement depth failure"):
        backend.retire_worker(run_key)

    assert paths.metadata_file.read_bytes() == exact_identity
