"""Focused tests for atomic Matrix sync continuity persistence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.durable_write import write_json_file_durable
from mindroom.matrix.sync_certification import SyncCheckpoint
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore

if TYPE_CHECKING:
    from pathlib import Path

_GENERATION = "cache-generation"


def _checkpoint(token: str) -> SyncCheckpoint:
    return SyncCheckpoint(token=token, cache_generation=_GENERATION)


def test_classic_acceptance_atomically_advances_checkpoint_and_clears_join_fence(
    tmp_path: Path,
) -> None:
    """One durable record must contain both sides of accepted Classic continuity."""
    store = SyncContinuityStore(tmp_path, "code")
    store.replace_checkpoint(_checkpoint("s_before"))
    store.update_join_fences(add={"!joined:localhost", "!pending:localhost"})

    record = store.accept_classic_response(
        _checkpoint("s_after"),
        joined_room_ids={"!joined:localhost"},
    )

    expected = SyncContinuityRecord(
        revision=3,
        checkpoint=_checkpoint("s_after"),
        pending_join_decrypt_fences=frozenset({"!pending:localhost"}),
    )
    assert record == expected
    assert SyncContinuityStore(tmp_path, "code").load() == expected


def test_crash_before_atomic_replace_preserves_old_checkpoint_and_fence(
    tmp_path: Path,
) -> None:
    """A failed pre-replace write cannot expose either half of new continuity."""
    store = SyncContinuityStore(tmp_path, "code")
    store.replace_checkpoint(_checkpoint("s_before"))
    store.update_join_fences(add={"!joined:localhost"})
    before = store.load()

    with (
        patch(
            "mindroom.matrix.sync_continuity.write_json_file_durable",
            side_effect=OSError("crash before replace"),
        ),
        pytest.raises(OSError, match="crash before replace"),
    ):
        store.accept_classic_response(
            _checkpoint("s_after"),
            joined_room_ids={"!joined:localhost"},
        )

    assert SyncContinuityStore(tmp_path, "code").load() == before


def test_rename_failure_never_falls_back_to_a_tearing_copy(tmp_path: Path) -> None:
    """A failed atomic rename must leave the complete old continuity record."""
    store = SyncContinuityStore(tmp_path, "code")
    store.replace_checkpoint(_checkpoint("s_before"))
    store.update_join_fences(add={"!joined:localhost"})
    before = store.load()

    with (
        patch("mindroom.durable_write.os.replace", side_effect=OSError("rename unavailable")),
        patch("mindroom.durable_write.safe_replace") as safe_replace,
        pytest.raises(OSError, match="rename unavailable"),
    ):
        store.accept_classic_response(
            _checkpoint("s_after"),
            joined_room_ids={"!joined:localhost"},
        )

    safe_replace.assert_not_called()
    assert SyncContinuityStore(tmp_path, "code").load() == before


def test_crash_after_atomic_replace_restores_new_checkpoint_without_fence(
    tmp_path: Path,
) -> None:
    """A restart after replace must observe the complete new continuity pair."""

    class SimulatedCrash(BaseException):
        pass

    store = SyncContinuityStore(tmp_path, "code")
    store.replace_checkpoint(_checkpoint("s_before"))
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
        store.accept_classic_response(
            _checkpoint("s_after"),
            joined_room_ids={"!joined:localhost"},
        )

    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord(
        revision=3,
        checkpoint=_checkpoint("s_after"),
    )


def test_concurrent_stores_serialize_fresh_read_updates_without_resurrection(
    tmp_path: Path,
) -> None:
    """A stale concurrent writer cannot erase a checkpoint or pending join."""
    store_a = SyncContinuityStore(tmp_path, "code")
    store_b = SyncContinuityStore(tmp_path, "code")
    store_a.replace_checkpoint(_checkpoint("s_before"))
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
        checkpoint_future = executor.submit(store_a.replace_checkpoint, _checkpoint("s_after"))
        assert first_write_entered.wait(timeout=2)
        fence_future = executor.submit(store_b.update_join_fences, add={"!pending:localhost"})
        assert not fence_future.done()
        release_first_write.set()
        checkpoint_future.result(timeout=2)
        fence_future.result(timeout=2)

    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord(
        revision=3,
        checkpoint=_checkpoint("s_after"),
        pending_join_decrypt_fences=frozenset({"!pending:localhost"}),
    )


def test_each_changed_record_gets_monotonic_revision_under_store_lock(
    tmp_path: Path,
) -> None:
    """Durable update order must remain visible to out-of-order runtime publishers."""
    store = SyncContinuityStore(tmp_path, "code")

    first = store.replace_checkpoint(_checkpoint("s_first"))
    no_op = store.replace_checkpoint(_checkpoint("s_first"))
    second = store.update_join_fences(add={"!pending:localhost"})

    assert first.revision == 1
    assert no_op.revision == 1
    assert second.revision == 2
    assert store.load().revision == 2


@pytest.mark.parametrize(
    "payload",
    [
        '{"version":"mindroom-sync-continuity-v1","checkpoint":"bad","pending_join_decrypt_fences":[]}',
        "not json",
        (
            '{"version":"mindroom-sync-continuity-v2","revision":true,'
            '"checkpoint":null,"pending_join_decrypt_fences":[]}'
        ),
        (
            '{"version":"mindroom-sync-continuity-v2","revision":0,'
            '"checkpoint":{"token":"s1"},"pending_join_decrypt_fences":[]}'
        ),
        (
            '{"version":"mindroom-sync-continuity-v2","revision":0,'
            '"checkpoint":null,"pending_join_decrypt_fences":["!room:localhost","!room:localhost"]}'
        ),
        (
            '{"version":"mindroom-sync-continuity-v2","revision":0,'
            '"checkpoint":null,"pending_join_decrypt_fences":[],"extra":true}'
        ),
        (
            '{"version":"mindroom-sync-continuity-v3","revision":0,'
            '"checkpoint":null,"pending_join_decrypt_fences":[],'
            '"unsettled_recovery_room_ids":["!room:localhost","!room:localhost"]}'
        ),
    ],
)
def test_obsolete_or_corrupt_continuity_record_fails_closed(
    tmp_path: Path,
    payload: str,
) -> None:
    """Unknown continuity cannot silently become a warm or unfenced restart."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="continuity"):
        SyncContinuityStore(tmp_path, "code").load()


def test_mutation_rejects_invalid_continuity_without_repair(tmp_path: Path) -> None:
    """Only explicit checkpoint clearing may repair an invalid record."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version":"future"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="continuity"):
        SyncContinuityStore(tmp_path, "code").replace_checkpoint(_checkpoint("s_after"))

    assert path.read_text(encoding="utf-8") == '{"version":"future"}'


def test_checkpoint_clear_repairs_invalid_continuity_record(tmp_path: Path) -> None:
    """Fail-closed checkpoint invalidation must restore a usable cold record."""
    path = tmp_path / "sync_continuity" / "code.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version":"future"}', encoding="utf-8")
    store = SyncContinuityStore(tmp_path, "code")

    record = store.clear_checkpoint()

    assert record == SyncContinuityRecord(revision=1)
    assert store.load() == SyncContinuityRecord(revision=1)


def test_legacy_token_path_is_ignored_without_compatibility_parsing(tmp_path: Path) -> None:
    """Old token formats cannot collide with the unified continuity record."""
    path = tmp_path / "sync_tokens" / "code.token"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"version":"mindroom-sync-token-v2","token":"s_old","cache_generation":"old"}',
        encoding="utf-8",
    )

    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord()
