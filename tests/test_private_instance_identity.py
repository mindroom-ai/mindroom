"""Tests for durable private-instance identity records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.private_instance_identity import (
    PrivateInstanceIdentity,
    PrivateInstanceIdentityError,
    ensure_private_instance_identity,
    load_private_instance_identity,
)
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
        scope_root,
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
    assert load_private_instance_identity(scope_root) == identity


def test_ensure_is_idempotent_for_the_same_identity(tmp_path: Path) -> None:
    """Re-materializing the same private scope must retain its original record."""
    worker_key = "v1:tenant-a:user:requester-a"
    scope_root = _scope_root(tmp_path, worker_key)

    first = ensure_private_instance_identity(
        scope_root,
        worker_key=worker_key,
        requester_id="requester-a",
    )
    record_path = scope_root / ".mindroom-private-instance.json"
    original_contents = record_path.read_text(encoding="utf-8")
    second = ensure_private_instance_identity(
        scope_root,
        worker_key=worker_key,
        requester_id="requester-a",
    )

    assert second == first
    assert record_path.read_text(encoding="utf-8") == original_contents


def test_ensure_rejects_distinct_requesters_that_normalize_to_the_same_worker_key(tmp_path: Path) -> None:
    """A normalized worker-key collision must not replace the recorded requester."""
    worker_key = "v1:tenant-a:user:requester_a"
    scope_root = _scope_root(tmp_path, worker_key)
    ensure_private_instance_identity(
        scope_root,
        worker_key=worker_key,
        requester_id="requester/a",
    )

    with pytest.raises(PrivateInstanceIdentityError, match="conflicts"):
        ensure_private_instance_identity(
            scope_root,
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
        load_private_instance_identity(scope_root)


def test_load_rejects_a_record_copied_to_a_different_scope_root(tmp_path: Path) -> None:
    """A valid record must not authenticate a scope directory derived from another worker key."""
    source_worker_key = "v1:tenant-a:user:requester-a"
    source_scope_root = _scope_root(tmp_path, source_worker_key)
    ensure_private_instance_identity(
        source_scope_root,
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
        load_private_instance_identity(copied_scope_root)


def test_load_returns_none_when_the_record_is_missing(tmp_path: Path) -> None:
    """Absent metadata is distinguishable from malformed metadata for read-only discovery."""
    scope_root = _scope_root(tmp_path, "v1:tenant-a:user:requester-a")

    assert load_private_instance_identity(scope_root) is None
