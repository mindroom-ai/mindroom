"""Tests for durable private-instance identity records."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest

from mindroom import private_instance_identity_store
from mindroom.private_instance_identity import (
    PrivateInstanceIdentity,
    PrivateInstanceIdentityError,
    load_private_instance_identity,
)
from mindroom.private_instance_identity_store import ensure_private_instance_identity
from mindroom.tool_system.worker_routing import private_instance_scope_root_path

if TYPE_CHECKING:
    from pathlib import Path


def _scope_root(tmp_path: Path, worker_key: str) -> Path:
    return private_instance_scope_root_path(tmp_path, worker_key)


def test_ensure_persists_and_loads_the_exact_identity_schema(tmp_path: Path) -> None:
    """A persisted record must retain its owner and only the versioned schema fields."""
    worker_key = "v1:tenant-a:user_agent:requester-a:assistant"
    scope_root = _scope_root(tmp_path, worker_key)

    identity = ensure_private_instance_identity(
        tmp_path,
        worker_key=worker_key,
        requester_id="requester-a",
    )

    assert identity == PrivateInstanceIdentity(worker_key=worker_key, requester_id="requester-a")
    assert json.loads((scope_root / ".mindroom-private-instance.json").read_text(encoding="utf-8")) == {
        "format": "mindroom-private-instance",
        "version": 1,
        "worker_key": "v1:tenant-a:user_agent:requester-a:assistant",
        "requester_id": "requester-a",
    }
    assert load_private_instance_identity(tmp_path, scope_root) == identity


def test_ensure_is_idempotent_for_the_same_identity(tmp_path: Path) -> None:
    """Re-materializing the same private scope must retain its original record."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)

    first = ensure_private_instance_identity(
        tmp_path,
        worker_key=worker_key,
        requester_id="requester-a",
    )
    record_path = scope_root / ".mindroom-private-instance.json"
    original_contents = record_path.read_text(encoding="utf-8")
    second = ensure_private_instance_identity(
        tmp_path,
        worker_key=worker_key,
        requester_id="requester-a",
    )

    assert second == first
    assert record_path.read_text(encoding="utf-8") == original_contents


def test_ensure_initializes_a_new_empty_private_scope(tmp_path: Path) -> None:
    """An empty scope has no legacy owner data and may receive its first identity record."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)

    identity = ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester-a")

    assert load_private_instance_identity(tmp_path, scope_root) == identity


@pytest.mark.parametrize(
    ("worker_key", "requester_id"),
    [
        ("v1:tenant-a:user:requester-a", "requester-a"),
        ("v1:tenant-a:user_agent:@alice:example.org:assistant", "@alice:example.org"),
    ],
)
def test_ensure_initializes_a_populated_legacy_scope_for_an_exact_requester(
    tmp_path: Path,
    worker_key: str,
    requester_id: str,
) -> None:
    """A key that retains the requester verbatim can claim its legacy private scope."""
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)
    (scope_root / "legacy-state.db").write_text("legacy", encoding="utf-8")

    identity = ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id=requester_id)

    assert load_private_instance_identity(tmp_path, scope_root) == identity


def test_ensure_rejects_a_populated_legacy_scope_for_a_lossy_requester(tmp_path: Path) -> None:
    """Legacy data stays unclaimed when normalization removed requester information."""
    worker_key = "v1:tenant-a:user:requester_a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)
    (scope_root / "legacy-state.db").write_text("legacy", encoding="utf-8")

    with pytest.raises(PrivateInstanceIdentityError, match="ambiguous legacy ownership"):
        ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester/a")

    assert not (scope_root / ".mindroom-private-instance.json").exists()


def test_ensure_rejects_a_distinct_normalized_requester_for_a_populated_legacy_scope(tmp_path: Path) -> None:
    """A second requester sharing a normalized key cannot claim ambiguous legacy data."""
    worker_key = "v1:tenant-a:user:requester_a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)
    (scope_root / "legacy-state.db").write_text("legacy", encoding="utf-8")

    with pytest.raises(PrivateInstanceIdentityError, match="ambiguous legacy ownership"):
        ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester?a")

    assert not (scope_root / ".mindroom-private-instance.json").exists()


def test_ensure_uses_no_lock_for_an_existing_matching_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Steady-state owner checks must not take the creation lock or repeat durable work."""
    worker_key = "v1:tenant-a:user:requester-a"
    identity = ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester-a")

    def unexpected_lock(*_args: object, **_kwargs: object) -> None:
        msg = "steady-state identity lookup took the creation lock"
        raise AssertionError(msg)

    monkeypatch.setattr(private_instance_identity_store, "advisory_file_lock", unexpected_lock)

    assert ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester-a") == identity


def test_ensure_rejects_whitespace_requester_before_creating_private_directories(tmp_path: Path) -> None:
    """A requester rejected by the persisted schema must not create a namespace or scope."""
    with pytest.raises(PrivateInstanceIdentityError, match="invalid identity fields"):
        ensure_private_instance_identity(
            tmp_path,
            worker_key="v1:tenant-a:user:default",
            requester_id=" \t ",
        )

    assert not (tmp_path / "private_instances").exists()


def test_ensure_uses_durable_directory_creation_for_namespace_and_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """First materialization must durably publish both directory entries before writing metadata."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    original_create_directory = private_instance_identity_store.create_directory_durable
    created_directories: list[tuple[Path, int]] = []

    def record_durable_directory(path: Path, *, mode: int) -> None:
        created_directories.append((path, mode))
        original_create_directory(path, mode=mode)

    monkeypatch.setattr(private_instance_identity_store, "create_directory_durable", record_durable_directory)

    ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester-a")

    assert created_directories == [
        (tmp_path / "private_instances", 0o700),
        (scope_root, 0o700),
    ]


def test_ensure_concurrently_creates_the_same_private_scope_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent first materialization of one owner must return the same persisted identity."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    original_create_directory = private_instance_identity_store.create_directory_durable
    scope_creation_barrier = Barrier(2)

    def synchronize_scope_creation(path: Path, *, mode: int) -> None:
        if path == scope_root:
            scope_creation_barrier.wait(timeout=5)
        original_create_directory(path, mode=mode)

    monkeypatch.setattr(private_instance_identity_store, "create_directory_durable", synchronize_scope_creation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(
                ensure_private_instance_identity,
                tmp_path,
                worker_key=worker_key,
                requester_id="requester-a",
            )
            for _ in range(2)
        ]

    assert [result.result() for result in results] == [
        PrivateInstanceIdentity(worker_key=worker_key, requester_id="requester-a"),
        PrivateInstanceIdentity(worker_key=worker_key, requester_id="requester-a"),
    ]


def test_ensure_rejects_distinct_requesters_that_normalize_to_the_same_worker_key(tmp_path: Path) -> None:
    """A normalized worker-key collision must not replace the recorded requester."""
    worker_key = "v1:tenant-a:user:requester_a"
    ensure_private_instance_identity(
        tmp_path,
        worker_key=worker_key,
        requester_id="requester/a",
    )

    with pytest.raises(PrivateInstanceIdentityError, match="conflicts"):
        ensure_private_instance_identity(
            tmp_path,
            worker_key=worker_key,
            requester_id="requester?a",
        )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"format": "mindroom-private-instance", "version": 1},
        {
            "format": "mindroom-private-instance",
            "version": 1,
            "worker_key": "v1:tenant-a:user:requester-a",
            "requester_id": "requester-a",
            "unexpected": True,
        },
        {
            "format": "wrong-format",
            "version": 1,
            "worker_key": "v1:tenant-a:user:requester-a",
            "requester_id": "requester-a",
        },
        {
            "format": "mindroom-private-instance",
            "version": 2,
            "worker_key": "v1:tenant-a:user:requester-a",
            "requester_id": "requester-a",
        },
        {
            "format": "mindroom-private-instance",
            "version": 1,
            "worker_key": "v1:tenant-a:user:other-requester",
            "requester_id": "requester-a",
        },
    ],
)
def test_load_rejects_malformed_or_forged_records(tmp_path: Path, payload: object) -> None:
    """Schema and worker-key validation must fail closed for invalid record content."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)
    (scope_root / ".mindroom-private-instance.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PrivateInstanceIdentityError):
        load_private_instance_identity(tmp_path, scope_root)


def test_load_rejects_a_record_copied_to_a_different_scope_root(tmp_path: Path) -> None:
    """A valid record must not authenticate a scope directory derived from another worker key."""
    source_worker_key = "v1:tenant-a:user:requester-a"
    source_scope_root = _scope_root(tmp_path, source_worker_key)
    ensure_private_instance_identity(
        tmp_path,
        worker_key=source_worker_key,
        requester_id="requester-a",
    )
    copied_scope_root = _scope_root(tmp_path, "v1:tenant-a:user:requester-b")
    copied_scope_root.mkdir(parents=True)
    (copied_scope_root / ".mindroom-private-instance.json").write_text(
        (source_scope_root / ".mindroom-private-instance.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(PrivateInstanceIdentityError, match="location"):
        load_private_instance_identity(tmp_path, copied_scope_root)


def test_load_returns_none_when_the_record_is_missing(tmp_path: Path) -> None:
    """Absent metadata is distinguishable from malformed metadata for read-only discovery."""
    scope_root = _scope_root(tmp_path, "v1:tenant-a:user:requester-a")

    assert load_private_instance_identity(tmp_path, scope_root) is None


@pytest.mark.parametrize(
    ("worker_key", "requester_id"),
    [
        ("v1:tenant-a:user:@requester:example.test", "@requester:example.test"),
        ("v1:tenant-a:user_agent:@requester:example.test:assistant", "@requester:example.test"),
    ],
)
def test_round_trips_colon_bearing_requester_ids(
    tmp_path: Path,
    worker_key: str,
    requester_id: str,
) -> None:
    """Worker-key reconstruction must preserve requester identifiers containing colons."""
    scope_root = _scope_root(tmp_path, worker_key)

    identity = ensure_private_instance_identity(
        tmp_path,
        worker_key=worker_key,
        requester_id=requester_id,
    )

    assert load_private_instance_identity(tmp_path, scope_root) == identity


def test_ensure_rejects_a_symlinked_private_instance_namespace(tmp_path: Path) -> None:
    """The trusted storage root must not follow a redirected private-instance namespace."""
    redirected_namespace = tmp_path / "redirected-namespace"
    redirected_namespace.mkdir()
    (tmp_path / "private_instances").symlink_to(redirected_namespace, target_is_directory=True)

    with pytest.raises(PrivateInstanceIdentityError):
        ensure_private_instance_identity(
            tmp_path,
            worker_key="v1:tenant-a:user:requester-a",
            requester_id="requester-a",
        )


def test_ensure_rejects_a_symlinked_scope_root(tmp_path: Path) -> None:
    """The derived scope root must be a direct directory entry in the trusted namespace."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.parent.mkdir()
    redirect_target = tmp_path / "redirect-target"
    redirect_target.mkdir()
    scope_root.symlink_to(redirect_target, target_is_directory=True)

    with pytest.raises(PrivateInstanceIdentityError):
        ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="requester-a")


@pytest.mark.parametrize("dangling", [False, True])
def test_load_rejects_a_symlinked_identity_record(tmp_path: Path, dangling: bool) -> None:
    """A record symlink is invalid whether or not its target is present."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)
    target = tmp_path / "record-target.json"
    if not dangling:
        target.write_text(
            json.dumps(
                {
                    "format": "mindroom-private-instance",
                    "version": 1,
                    "worker_key": worker_key,
                    "requester_id": "requester-a",
                },
            ),
            encoding="utf-8",
        )
    (scope_root / ".mindroom-private-instance.json").symlink_to(target)

    with pytest.raises(PrivateInstanceIdentityError):
        load_private_instance_identity(tmp_path, scope_root)


def test_load_rejects_a_non_regular_identity_record(tmp_path: Path) -> None:
    """Only a regular file may represent the identity record."""
    scope_root = _scope_root(tmp_path, "v1:tenant-a:user:requester-a")
    (scope_root / ".mindroom-private-instance.json").mkdir(parents=True)

    with pytest.raises(PrivateInstanceIdentityError):
        load_private_instance_identity(tmp_path, scope_root)


def test_load_rejects_duplicate_json_record_fields(tmp_path: Path) -> None:
    """Repeated JSON object fields must not be collapsed into an accepted schema."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)
    scope_root.mkdir(parents=True)
    (scope_root / ".mindroom-private-instance.json").write_text(
        (
            '{"format":"mindroom-private-instance","format":"mindroom-private-instance",'
            '"version":1,"worker_key":"v1:tenant-a:user:requester-a",'
            '"requester_id":"requester-a"}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(PrivateInstanceIdentityError):
        load_private_instance_identity(tmp_path, scope_root)
