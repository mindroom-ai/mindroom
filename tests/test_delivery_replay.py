"""Tests for the delivery replay store's caller-chosen root."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.delivery_replay import DeliveryEventClaim, DeliveryReplayStore, DeliveryThreadKeyClaim

if TYPE_CHECKING:
    from pathlib import Path


def test_store_uses_given_root_and_skips_legacy_split_without_file(tmp_path: Path) -> None:
    """A store without a legacy file keeps its scope files in the root and never reads a sibling replay.json."""
    root = tmp_path / "slack" / "replay"
    sibling = root.parent / "replay.json"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("not json", encoding="utf-8")

    store = DeliveryReplayStore(root=root)

    assert store.claim_event_id("slack:agent:a", "Ev1", now=0, ttl_seconds=60) is DeliveryEventClaim.FRESH
    assert list(root.glob("*.json"))
    assert sibling.read_text(encoding="utf-8") == "not json"


def test_bound_thread_roundtrip(tmp_path: Path) -> None:
    """A reserved thread key binds to its root, and the next claim returns that root."""
    store = DeliveryReplayStore(root=tmp_path / "replay")
    claim, root, reservation = store.claim_thread_key(
        "scope",
        "thread",
        room_id="!room:localhost",
        now=0,
        pending_ttl_seconds=60,
    )
    assert claim is DeliveryThreadKeyClaim.FRESH
    assert root is None
    assert reservation is not None

    bound_root = store.bind_thread_root(
        "scope",
        "thread",
        "$root",
        room_id="!room:localhost",
        reservation=reservation,
        now=1,
        ttl_seconds=600,
    )
    assert bound_root == "$root"

    assert store.claim_thread_key(
        "scope",
        "thread",
        room_id="!room:localhost",
        now=2,
        pending_ttl_seconds=60,
    ) == (DeliveryThreadKeyClaim.BOUND, "$root", None)


def test_bound_thread_root_reads_only_live_bindings(tmp_path: Path) -> None:
    """Only a bound, unexpired key names its room and root; looking claims nothing."""
    store = DeliveryReplayStore(root=tmp_path / "replay")
    assert store.bound_thread_root("scope", "thread", now=0) is None
    _claim, _root, reservation = store.claim_thread_key(
        "scope",
        "thread",
        room_id="!room:localhost",
        now=0,
        pending_ttl_seconds=60,
    )
    assert store.bound_thread_root("scope", "thread", now=1) is None

    store.bind_thread_root(
        "scope",
        "thread",
        "$root",
        room_id="!room:localhost",
        reservation=reservation,
        now=1,
        ttl_seconds=600,
    )

    assert store.bound_thread_root("scope", "thread", now=601) == ("!room:localhost", "$root")
    assert store.bound_thread_root("scope", "thread", now=602) is None
    assert store.bound_thread_root("scope", "other", now=1) is None
