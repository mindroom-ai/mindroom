"""Focused tests for atomic Matrix join-fence persistence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.durable_write import write_json_file_durable
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore

if TYPE_CHECKING:
    from pathlib import Path

_PERSISTED_RECORD_BYTES = (
    '{"pending_join_decrypt_fences": ["!pending:localhost"], "revision": 1, "version": "mindroom-sync-continuity-v4"}\n'
)

# Actual records from the earlier checkpoint formats. The generation key
# changed between them, but their pending join fences have the same meaning.
_LEGACY_RECORD_BYTES = (
    '{"checkpoint": {"cache_generation": "store-generation", "token": "s_saved"}, '
    '"pending_join_decrypt_fences": ["!pending:localhost"], '
    '"revision": 2, "version": "mindroom-sync-continuity-v2"}\n',
    '{"checkpoint": {"store_generation": "store-generation", "token": "s_saved"}, '
    '"pending_join_decrypt_fences": ["!pending:localhost"], '
    '"revision": 2, "version": "mindroom-sync-continuity-v3"}\n',
)


def test_join_fences_round_trip_without_checkpoint_metadata(tmp_path: Path) -> None:
    """Fence persistence survives restart without writing transport state."""
    store = SyncContinuityStore(tmp_path, "code")

    store.update_join_fences(add={"!pending:localhost"})

    written = (tmp_path / "sync_continuity" / "code.json").read_text(encoding="utf-8")
    assert written == _PERSISTED_RECORD_BYTES
    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord(
        revision=1,
        pending_join_decrypt_fences=frozenset({"!pending:localhost"}),
    )


@pytest.mark.parametrize("payload", _LEGACY_RECORD_BYTES)
def test_old_checkpoint_records_require_explicit_reset(tmp_path: Path, payload: str) -> None:
    """Former checkpoint records cannot silently enter the current runtime."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported version"):
        SyncContinuityStore(tmp_path, "code").load()
    assert path.read_text(encoding="utf-8") == payload


def test_join_fence_updates_retain_add_and_remove_in_one_record(tmp_path: Path) -> None:
    """Settling observed joins preserves pending joins and adds newly fenced rooms."""
    store = SyncContinuityStore(tmp_path, "code")
    store.update_join_fences(add={"!joined:localhost", "!pending:localhost", "!left:localhost"})

    record = store.update_join_fences(
        retain={"!joined:localhost", "!pending:localhost"},
        add={"!new:localhost"},
        remove={"!joined:localhost"},
    )

    expected = SyncContinuityRecord(
        revision=2,
        pending_join_decrypt_fences=frozenset({"!new:localhost", "!pending:localhost"}),
    )
    assert record == expected
    assert SyncContinuityStore(tmp_path, "code").load() == expected


def test_crash_before_atomic_replace_preserves_old_fences(tmp_path: Path) -> None:
    """A failed pre-replace write cannot expose part of a fence update."""
    store = SyncContinuityStore(tmp_path, "code")
    store.update_join_fences(add={"!joined:localhost"})
    before = store.load()

    with (
        patch(
            "mindroom.matrix.sync_continuity.write_json_file_durable",
            side_effect=OSError("crash before replace"),
        ),
        pytest.raises(OSError, match="crash before replace"),
    ):
        store.update_join_fences(add={"!new:localhost"}, remove={"!joined:localhost"})

    assert SyncContinuityStore(tmp_path, "code").load() == before


def test_rename_failure_never_falls_back_to_a_tearing_copy(tmp_path: Path) -> None:
    """A failed atomic rename must leave the complete old fence record."""
    store = SyncContinuityStore(tmp_path, "code")
    store.update_join_fences(add={"!joined:localhost"})
    before = store.load()

    with (
        patch("mindroom.durable_write.os.replace", side_effect=OSError("rename unavailable")),
        patch("mindroom.durable_write.safe_replace") as safe_replace,
        pytest.raises(OSError, match="rename unavailable"),
    ):
        store.update_join_fences(add={"!new:localhost"}, remove={"!joined:localhost"})

    safe_replace.assert_not_called()
    assert SyncContinuityStore(tmp_path, "code").load() == before


def test_crash_after_atomic_replace_restores_complete_fence_update(tmp_path: Path) -> None:
    """A restart after replace observes both additions and removals together."""

    class SimulatedCrash(BaseException):
        pass

    store = SyncContinuityStore(tmp_path, "code")
    store.update_join_fences(add={"!joined:localhost"})

    def replace_then_crash(*args: object, **kwargs: object) -> None:
        write_json_file_durable(*args, **kwargs)
        raise SimulatedCrash

    with (
        patch(
            "mindroom.matrix.sync_continuity.write_json_file_durable",
            side_effect=replace_then_crash,
        ),
        pytest.raises(SimulatedCrash),
    ):
        store.update_join_fences(add={"!new:localhost"}, remove={"!joined:localhost"})

    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord(
        revision=2,
        pending_join_decrypt_fences=frozenset({"!new:localhost"}),
    )


def test_concurrent_stores_serialize_fresh_read_updates_without_resurrection(tmp_path: Path) -> None:
    """A stale concurrent writer cannot resurrect a settled join fence."""
    store_a = SyncContinuityStore(tmp_path, "code")
    store_b = SyncContinuityStore(tmp_path, "code")
    store_a.update_join_fences(add={"!joined:localhost"})
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    writer_calls = 0
    writer_calls_lock = threading.Lock()

    def blocking_first_write(*args: object, **kwargs: object) -> None:
        nonlocal writer_calls
        with writer_calls_lock:
            writer_calls += 1
            is_first = writer_calls == 1
        if is_first:
            first_write_entered.set()
            assert release_first_write.wait(timeout=2)
        write_json_file_durable(*args, **kwargs)

    with (
        patch(
            "mindroom.matrix.sync_continuity.write_json_file_durable",
            side_effect=blocking_first_write,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        remove_future = executor.submit(store_a.update_join_fences, remove={"!joined:localhost"})
        assert first_write_entered.wait(timeout=2)
        add_future = executor.submit(store_b.update_join_fences, add={"!pending:localhost"})
        assert not add_future.done()
        release_first_write.set()
        remove_future.result(timeout=2)
        add_future.result(timeout=2)

    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord(
        revision=3,
        pending_join_decrypt_fences=frozenset({"!pending:localhost"}),
    )


def test_each_changed_record_gets_monotonic_revision_under_store_lock(tmp_path: Path) -> None:
    """Durable update order remains visible to out-of-order runtime publishers."""
    store = SyncContinuityStore(tmp_path, "code")

    first = store.update_join_fences(add={"!joined:localhost"})
    no_op = store.update_join_fences(add={"!joined:localhost"})
    second = store.update_join_fences(add={"!pending:localhost"})

    assert first.revision == 1
    assert no_op.revision == 1
    assert second.revision == 2
    assert store.load().revision == 2


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":"mindroom-sync-continuity-v1","checkpoint":"bad","pending_join_decrypt_fences":[]}',
        b"not json",
        b"\xff",
        b"[]",
        b'{"version":[],"revision":0,"pending_join_decrypt_fences":[]}',
        b'{"version":"mindroom-sync-continuity-v4","revision":true,"pending_join_decrypt_fences":[]}',
        b'{"version":"mindroom-sync-continuity-v4","revision":-1,"pending_join_decrypt_fences":[]}',
        b'{"version":"mindroom-sync-continuity-v4","revision":0}',
        b'{"version":"mindroom-sync-continuity-v4","revision":0,"pending_join_decrypt_fences":[null]}',
        b'{"version":"mindroom-sync-continuity-v4","revision":0,"pending_join_decrypt_fences":[""]}',
        (
            b'{"version":"mindroom-sync-continuity-v3","revision":0,'
            b'"checkpoint":null,"pending_join_decrypt_fences":["!room:localhost","!room:localhost"]}'
        ),
        (
            b'{"version":"mindroom-sync-continuity-v4","revision":0,'
            b'"pending_join_decrypt_fences":["!room:localhost","!room:localhost"]}'
        ),
        (b'{"version":"mindroom-sync-continuity-v4","revision":0,"pending_join_decrypt_fences":[],"extra":true}'),
        b'{"version":"mindroom-sync-continuity-v5","revision":0,"pending_join_decrypt_fences":[]}',
    ],
)
def test_unknown_or_corrupt_fence_record_fails_closed(tmp_path: Path, payload: bytes) -> None:
    """Unknown or malformed fence state cannot silently become an unfenced restart."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    with pytest.raises(RuntimeError, match="continuity"):
        SyncContinuityStore(tmp_path, "code").load()


def test_mutation_rejects_invalid_continuity_without_repair(tmp_path: Path) -> None:
    """A fence update cannot erase an unreadable record."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version":"future"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="continuity"):
        SyncContinuityStore(tmp_path, "code").update_join_fences(add={"!pending:localhost"})

    assert path.read_text(encoding="utf-8") == '{"version":"future"}'


def test_legacy_token_path_is_ignored_without_compatibility_parsing(tmp_path: Path) -> None:
    """Old token formats cannot collide with the join-fence record."""
    path = tmp_path / "sync_tokens" / "code.token"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"version":"mindroom-sync-token-v2","token":"s_old","store_generation":"old"}',
        encoding="utf-8",
    )

    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord()
