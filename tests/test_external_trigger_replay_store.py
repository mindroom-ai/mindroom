"""Tests for external trigger payloads and replay storage."""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from mindroom.constants import safe_replace
from mindroom.durable_write import _fsync_directory
from mindroom.external_triggers.models import ExternalTriggerAcceptedResponse, ExternalTriggerPayload
from mindroom.external_triggers.replay_store import (
    ExternalTriggerEventClaim,
    ExternalTriggerReplayStore,
    ExternalTriggerReplayStoreError,
    ExternalTriggerThreadKeyClaim,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.queues import Queue
    from multiprocessing.synchronize import Event


def _store_path(tmp_path: Path) -> Path:
    path = tmp_path / "external_triggers" / "replay.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _claim_nonce_with_slow_read_worker(
    control_state_root: str,
    start_event: Event,
    result_queue: Queue[tuple[str, bool | str]],
) -> None:
    """Claim one nonce after slowing reads enough to expose missing cross-process locking."""
    original_read_store = cast(
        "Callable[[ExternalTriggerReplayStore], object]",
        ExternalTriggerReplayStore._read_store,
    )

    def slow_read_store(self: ExternalTriggerReplayStore) -> object:
        store = original_read_store(self)
        time.sleep(0.1)
        return store

    ExternalTriggerReplayStore._read_store = slow_read_store
    try:
        if not start_event.wait(timeout=5):
            result_queue.put(("error", "timed out waiting for start signal"))
            return
        claimed = ExternalTriggerReplayStore(Path(control_state_root)).claim_nonce(
            "campground",
            "nonce-1",
            now=1_000,
            ttl_seconds=300,
        )
        result_queue.put(("ok", claimed))
    except BaseException as exc:
        result_queue.put(("error", repr(exc)))


def test_payload_rejects_target_override_fields_and_uses_isolated_data_dict() -> None:
    """Payloads should not accept target override fields and should not share data defaults."""
    with pytest.raises(ValidationError, match="room_id"):
        ExternalTriggerPayload.model_validate(
            {
                "kind": "campground.availability",
                "message": "Site opened",
                "room_id": "!unsafe:example.org",
            },
        )

    first = ExternalTriggerPayload(kind="campground.availability", message="Site opened")
    second = ExternalTriggerPayload(kind="campground.availability", message="Different site opened")
    first.data["site"] = "42"

    assert first.kind == "campground.availability"
    assert first.message == "Site opened"
    assert first.event_id is None
    assert first.title is None
    assert first.data == {"site": "42"}
    assert second.data == {}


@pytest.mark.parametrize(
    ("field_name", "payload"),
    [
        ("kind", {"kind": "  ", "message": "Site opened"}),
        ("message", {"kind": "campground.availability", "message": "  "}),
    ],
)
def test_payload_rejects_blank_kind_and_message(field_name: str, payload: dict[str, str]) -> None:
    """Payload kind and message must contain non-whitespace text."""
    with pytest.raises(ValidationError, match=field_name):
        ExternalTriggerPayload.model_validate(payload)


def test_accepted_response_defaults_matrix_event_id_and_duplicate_flag() -> None:
    """Accepted responses should expose stable duplicate and delivery fields."""
    response = ExternalTriggerAcceptedResponse(
        accepted=True,
        trigger_id="campground",
        event_id="availability-123",
    )

    assert response.accepted is True
    assert response.duplicate is False
    assert response.trigger_id == "campground"
    assert response.event_id == "availability-123"
    assert response.matrix_event_id is None


def test_shared_store_instances_coordinate_nonce_and_event_claims(tmp_path: Path) -> None:
    """Store instances for one tracking root should share replay state."""
    first_store = ExternalTriggerReplayStore(tmp_path)
    second_store = ExternalTriggerReplayStore(tmp_path)

    assert first_store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert not second_store.claim_nonce("campground", "nonce-1", now=1_001, ttl_seconds=300)

    assert first_store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )
    assert second_store.claim_event_id("campground", "availability-123", now=1_001, ttl_seconds=300) is (
        ExternalTriggerEventClaim.IN_PROGRESS
    )

    second_store.mark_event_delivered("campground", "availability-123", now=1_002, ttl_seconds=300)

    assert first_store.claim_event_id("campground", "availability-123", now=1_003, ttl_seconds=300) is (
        ExternalTriggerEventClaim.DELIVERED
    )


def test_shared_store_processes_coordinate_nonce_claims(tmp_path: Path) -> None:
    """Separate API processes should not both claim the same nonce from one filesystem store."""
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_nonce_with_slow_read_worker,
            args=(str(tmp_path), start_event, result_queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start_event.set()

    results: list[bool] = []
    try:
        for _ in processes:
            try:
                status, payload = result_queue.get(timeout=10)
            except Empty as exc:
                msg = "timed out waiting for replay-store worker result"
                raise AssertionError(msg) from exc
            assert status == "ok", payload
            assert isinstance(payload, bool), payload
            results.append(payload)
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(results) == [False, True]


def test_release_after_send_failure_keeps_nonce_single_use_but_allows_event_retry(tmp_path: Path) -> None:
    """Rollback release should remove only the event-id claim."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    store.release_event_id("campground", "availability-123")

    assert not store.claim_nonce("campground", "nonce-1", now=1_010, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_010, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )


def test_expired_nonce_and_event_id_can_be_reclaimed(tmp_path: Path) -> None:
    """Expired replay claims should be pruned on the next claim."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    assert store.claim_nonce("campground", "nonce-1", now=1_301, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_301, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )


def test_in_progress_event_id_processing_ttl_outlives_nonce_ttl(tmp_path: Path) -> None:
    """Long-running deliveries should not be re-claimed when signature replay TTL expires."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=86_400) is (
        ExternalTriggerEventClaim.FRESH
    )

    assert store.claim_nonce("campground", "nonce-1", now=1_301, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_301, ttl_seconds=86_400) is (
        ExternalTriggerEventClaim.IN_PROGRESS
    )


def test_nonce_and_event_id_remain_claimed_at_exact_expiry_boundary(tmp_path: Path) -> None:
    """Replay claims should expire after their final valid timestamp."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    assert not store.claim_nonce("campground", "nonce-1", now=1_300, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_300, ttl_seconds=300) is (
        ExternalTriggerEventClaim.IN_PROGRESS
    )

    assert store.claim_nonce("campground", "nonce-1", now=1_301, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_301, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )


@pytest.mark.parametrize("store_payload", [["not", "a", "dict"], {"nonces": {}}])
def test_invalid_store_shape_fails_closed(tmp_path: Path, store_payload: object) -> None:
    """Malformed top-level store structure should not reset replay protection."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps(store_payload), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


@pytest.mark.parametrize(
    "store_payload",
    [
        {"nonces": {"campground": {"nonce-1": {"expires_at": "later"}}}, "events": {}},
        {"nonces": {"campground": {"nonce-1": {}}}, "events": {}},
        {"nonces": {"campground": {"nonce-1": {"expires_at": True}}}, "events": {}},
        {"nonces": {"campground": ["nonce-1"]}, "events": {}},
    ],
)
def test_invalid_nested_nonce_record_fails_closed(tmp_path: Path, store_payload: object) -> None:
    """Malformed nonce records should not be silently dropped."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps(store_payload), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


@pytest.mark.parametrize(
    "store_payload",
    [
        {
            "nonces": {},
            "events": {"campground": {"availability-123": {"state": "bad", "expires_at": 1_300}}},
        },
        {
            "nonces": {},
            "events": {
                "campground": {
                    "availability-123": {"state": "delivered", "expires_at": "later"},
                },
            },
        },
        {"nonces": {}, "events": {"campground": ["availability-123"]}},
    ],
)
def test_invalid_nested_event_record_fails_closed(tmp_path: Path, store_payload: object) -> None:
    """Malformed event records should not be silently dropped."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps(store_payload), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300)


def test_corrupt_json_store_fails_closed(tmp_path: Path) -> None:
    """Syntactically corrupt store JSON should not reset replay protection."""
    store_path = _store_path(tmp_path)
    store_path.write_text("{not valid json", encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


def test_replay_store_read_oserror_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay store read failures should not become implicit empty state."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps({"nonces": {}, "events": {}}), encoding="utf-8")
    original_read_text = type(store_path).read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == store_path:
            msg = "permission denied"
            raise OSError(msg)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(store_path), "read_text", read_text)
    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="unavailable"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


def test_replay_store_write_oserror_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay store write failures should surface through the typed store error."""

    def raise_disk_full(_fd: int) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("mindroom.durable_write.os.fsync", raise_disk_full)
    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="unavailable"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


def test_replay_store_write_uses_bind_mount_safe_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay writes should survive filesystems where atomic replace reports EBUSY."""
    store_path = _store_path(tmp_path)
    original_replace = type(store_path).replace

    def raise_busy_on_store_replace(path: Path, target: Path) -> Path:
        if target == store_path:
            msg = "Device or resource busy"
            raise OSError(msg)
        return original_replace(path, target)

    monkeypatch.setattr(type(store_path), "replace", raise_busy_on_store_replace)
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert json.loads(store_path.read_text(encoding="utf-8"))["nonces"]["campground"]["nonce-1"] == {
        "expires_at": 1_300,
    }


def test_safe_replace_copy_fallback_fsyncs_target_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind-mount fallback should flush copied target bytes, not only the temp file."""
    temp_path = tmp_path / "state.json.tmp"
    target_path = tmp_path / "state.json"
    temp_path.write_text('{"ok": true}', encoding="utf-8")
    original_replace = type(temp_path).replace
    opened_paths: dict[int, Path] = {}
    fsynced_paths: list[Path] = []
    closed_fds: list[int] = []

    def raise_busy_on_replace(path: Path, target: Path) -> Path:
        if path == temp_path and target == target_path:
            msg = "Device or resource busy"
            raise OSError(msg)
        return original_replace(path, target)

    def record_open(path: Path, _flags: int) -> int:
        fd = 100 + len(opened_paths)
        opened_paths[fd] = Path(path)
        return fd

    def record_fsync(fd: int) -> None:
        fsynced_paths.append(opened_paths[fd])

    monkeypatch.setattr(type(temp_path), "replace", raise_busy_on_replace)
    monkeypatch.setattr("mindroom.constants.os.open", record_open)
    monkeypatch.setattr("mindroom.constants.os.fsync", record_fsync)
    monkeypatch.setattr("mindroom.constants.os.close", closed_fds.append)

    safe_replace(temp_path, target_path)

    assert target_path.read_text(encoding="utf-8") == '{"ok": true}'
    assert fsynced_paths == [target_path]
    assert closed_fds == [100]
    assert not temp_path.exists()


def test_fsync_directory_ignores_unsupported_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory fsync is best-effort on filesystems that do not support it."""
    closed_fds: list[int] = []

    def raise_unsupported(_fd: int) -> None:
        msg = "unsupported"
        raise OSError(msg)

    monkeypatch.setattr("mindroom.durable_write.os.open", lambda _path, _flags: 123)
    monkeypatch.setattr("mindroom.durable_write.os.fsync", raise_unsupported)
    monkeypatch.setattr("mindroom.durable_write.os.close", closed_fds.append)

    _fsync_directory(tmp_path)

    assert closed_fds == [123]


def test_payload_rejects_blank_thread_key() -> None:
    """A thread key must carry a value when present; ``None`` means per-delivery threads."""
    with pytest.raises(ValidationError, match="thread_key must not be empty"):
        ExternalTriggerPayload(kind="campground.availability", message="Site open", thread_key="  ")

    assert ExternalTriggerPayload(kind="campground.availability", message="Site open").thread_key is None
    assert (
        ExternalTriggerPayload(kind="campground.availability", message="Site open", thread_key=" site-42 ").thread_key
        == "site-42"
    )


def test_payload_rejects_oversized_thread_key() -> None:
    """Thread keys live for days in the shared replay store, so their size is bounded."""
    with pytest.raises(ValidationError, match="thread_key must be at most 256 characters"):
        ExternalTriggerPayload(kind="campground.availability", message="Site open", thread_key="k" * 257)

    assert (
        ExternalTriggerPayload(kind="campground.availability", message="Site open", thread_key="k" * 256).thread_key
        == "k" * 256
    )


def test_store_without_threads_section_is_accepted(tmp_path: Path) -> None:
    """Replay files written before thread keys existed still load."""
    _store_path(tmp_path).write_text(json.dumps({"nonces": {}, "events": {}}), encoding="utf-8")
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_thread_key(
        "campground",
        "site-42",
        room_id="!room:localhost",
        now=1_000,
        pending_ttl_seconds=60,
    ) == (ExternalTriggerThreadKeyClaim.FRESH, None)
    store.bind_thread_root("campground", "site-42", "$root-1", room_id="!room:localhost", now=1_000, ttl_seconds=600)
    assert json.loads(_store_path(tmp_path).read_text(encoding="utf-8"))["threads"] == {
        "campground": {
            "site-42": {"room_id": "!room:localhost", "thread_event_id": "$root-1", "expires_at": 1_600},
        },
    }


@pytest.mark.parametrize(
    "threads_payload",
    [
        {"campground": {"site-42": {"room_id": "!r:x", "thread_event_id": "", "expires_at": 1}}},
        {"campground": {"site-42": {"room_id": "!r:x", "thread_event_id": "$root", "expires_at": "1"}}},
        {"campground": {"site-42": {"room_id": "", "thread_event_id": "$root", "expires_at": 1}}},
        {"campground": {"site-42": {"thread_event_id": "$root", "expires_at": 1}}},
        {"campground": {"site-42": {"expires_at": 1}}},
        {"campground": "not-a-mapping"},
    ],
)
def test_invalid_nested_thread_record_fails_closed(tmp_path: Path, threads_payload: object) -> None:
    """Corrupt thread records are rejected instead of silently dropped."""
    _store_path(tmp_path).write_text(
        json.dumps({"nonces": {}, "events": {}, "threads": threads_payload}),
        encoding="utf-8",
    )

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid external trigger replay store structure"):
        ExternalTriggerReplayStore(tmp_path).claim_thread_key(
            "campground",
            "site-42",
            room_id="!r:x",
            now=1,
            pending_ttl_seconds=60,
        )


ROOM = "!room:localhost"


def _claim(
    store: ExternalTriggerReplayStore,
    key: str = "site-42",
    *,
    scope: str = "campground",
    room: str = ROOM,
    now: int,
) -> tuple[ExternalTriggerThreadKeyClaim, str | None]:
    return store.claim_thread_key(scope, key, room_id=room, now=now, pending_ttl_seconds=60)


def test_thread_key_claim_reserves_then_binds_then_resolves(tmp_path: Path) -> None:
    """First claim reserves the key; a bound key resolves to its root within scope and room."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert _claim(store, now=1_000) == (ExternalTriggerThreadKeyClaim.FRESH, None)
    assert _claim(store, now=1_001) == (ExternalTriggerThreadKeyClaim.PENDING, None)
    store.bind_thread_root("campground", "site-42", "$root-1", room_id=ROOM, now=1_002, ttl_seconds=600)

    assert _claim(store, now=1_003) == (ExternalTriggerThreadKeyClaim.BOUND, "$root-1")
    assert _claim(store, now=1_602) == (ExternalTriggerThreadKeyClaim.BOUND, "$root-1")
    assert _claim(store, now=1_603) == (ExternalTriggerThreadKeyClaim.FRESH, None)
    assert _claim(store, "site-43", now=1_000) == (ExternalTriggerThreadKeyClaim.FRESH, None)
    assert _claim(store, scope="other-scope", now=1_000) == (ExternalTriggerThreadKeyClaim.FRESH, None)


def test_thread_key_bound_to_another_room_is_reclaimed(tmp_path: Path) -> None:
    """A re-pointed trigger room must not relate deliveries to a foreign root."""
    store = ExternalTriggerReplayStore(tmp_path)
    store.bind_thread_root("campground", "site-42", "$root-1", room_id=ROOM, now=1_000, ttl_seconds=600)

    assert _claim(store, room="!elsewhere:localhost", now=1_001) == (ExternalTriggerThreadKeyClaim.FRESH, None)
    assert _claim(store, room="!elsewhere:localhost", now=1_002) == (ExternalTriggerThreadKeyClaim.PENDING, None)


def test_pending_thread_key_expires_and_release_keeps_bound_keys(tmp_path: Path) -> None:
    """A crashed first delivery frees the key after the pending TTL; release never drops a bound key."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert _claim(store, now=1_000) == (ExternalTriggerThreadKeyClaim.FRESH, None)
    assert _claim(store, now=1_060) == (ExternalTriggerThreadKeyClaim.PENDING, None)
    assert _claim(store, now=1_061) == (ExternalTriggerThreadKeyClaim.FRESH, None)

    store.release_thread_key("campground", "site-42")
    assert _claim(store, now=1_062) == (ExternalTriggerThreadKeyClaim.FRESH, None)

    store.bind_thread_root("campground", "site-42", "$root-1", room_id=ROOM, now=1_063, ttl_seconds=600)
    store.release_thread_key("campground", "site-42")
    store.release_thread_key("campground", "missing")
    store.release_thread_key("other-scope", "site-42")
    assert _claim(store, now=1_064) == (ExternalTriggerThreadKeyClaim.BOUND, "$root-1")


def test_bind_thread_root_refreshes_retention(tmp_path: Path) -> None:
    """Every continued delivery extends how long the key keeps routing to its root."""
    store = ExternalTriggerReplayStore(tmp_path)
    store.bind_thread_root("campground", "site-42", "$root-1", room_id=ROOM, now=1_000, ttl_seconds=600)
    store.bind_thread_root("campground", "site-42", "$root-1", room_id=ROOM, now=1_500, ttl_seconds=600)

    assert _claim(store, now=2_099) == (ExternalTriggerThreadKeyClaim.BOUND, "$root-1")
    assert _claim(store, now=2_101) == (ExternalTriggerThreadKeyClaim.FRESH, None)


def _claim_thread_key_with_slow_read_worker(
    control_state_root: str,
    start_event: Event,
    result_queue: Queue[tuple[str, str]],
) -> None:
    """Claim one new thread key after slowing reads enough to expose missing cross-process locking."""
    original_read_store = cast(
        "Callable[[ExternalTriggerReplayStore], object]",
        ExternalTriggerReplayStore._read_store,
    )

    def slow_read_store(self: ExternalTriggerReplayStore) -> object:
        store = original_read_store(self)
        time.sleep(0.1)
        return store

    ExternalTriggerReplayStore._read_store = slow_read_store
    try:
        if not start_event.wait(timeout=5):
            result_queue.put(("error", "timed out waiting for start signal"))
            return
        claim, _root = ExternalTriggerReplayStore(Path(control_state_root)).claim_thread_key(
            "campground",
            "site-42",
            room_id=ROOM,
            now=1_000,
            pending_ttl_seconds=60,
        )
        result_queue.put(("ok", claim.value))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))


def test_concurrent_first_deliveries_reserve_one_thread_key(tmp_path: Path) -> None:
    """Two processes racing on a new key: exactly one may open the root, the other waits."""
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_thread_key_with_slow_read_worker,
            args=(str(tmp_path), start_event, result_queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start_event.set()

    results: list[str] = []
    try:
        for _ in processes:
            try:
                status, payload = result_queue.get(timeout=10)
            except Empty as exc:
                msg = "timed out waiting for replay-store worker result"
                raise AssertionError(msg) from exc
            assert status == "ok", payload
            results.append(payload)
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(results) == ["fresh", "pending"]
