"""Frame-key rotation policy tests (ported behavior of RTCEncryptionManager)."""

from __future__ import annotations

import base64

from mindroom.matrix_rtc.events import CallMember, ReceivedFrameKey
from mindroom.matrix_rtc.frame_keys import (
    _KEY_ROTATION_GRACE_PERIOD_MS,
    _USE_KEY_DELAY_MS,
    FrameKeyManager,
)

OWN_USER = "@bot:example.org"
OWN_DEVICE = "BOTDEVICE"


def _member(user: str, device: str = "DEV", created_ts: int = 0) -> CallMember:
    return CallMember(
        user_id=user,
        device_id=device,
        created_ts=created_ts,
        expires_ms=10_000_000,
        membership_id=f"{user}:{device}",
    )


def _manager() -> FrameKeyManager:
    return FrameKeyManager(own_user_id=OWN_USER, own_device_id=OWN_DEVICE)


def test_first_key_is_index_zero_and_immediately_usable() -> None:
    """First key is index zero and immediately usable."""
    manager = _manager()
    alice = _member("@alice:example.org")
    distribution = manager.update_memberships([alice], now_ms=0)
    assert distribution is not None
    assert distribution.key_index == 0
    assert distribution.apply_after_ms == 0
    assert distribution.targets == (alice,)
    assert base64.b64decode(distribution.key_base64) == distribution.key
    manager.mark_distributed(distribution)
    assert manager.update_memberships([alice], now_ms=1) is None


def test_own_membership_is_never_a_target() -> None:
    """Own membership is never a target."""
    manager = _manager()
    distribution = manager.update_memberships([_member(OWN_USER, OWN_DEVICE)], now_ms=0)
    assert distribution is not None
    assert distribution.targets == ()


def test_joiner_within_grace_period_gets_current_key_only() -> None:
    """Joiner within grace period gets current key only."""
    manager = _manager()
    alice = _member("@alice:example.org")
    first = manager.update_memberships([alice], now_ms=0)
    assert first is not None
    manager.mark_distributed(first)

    bob = _member("@bob:example.org")
    second = manager.update_memberships([alice, bob], now_ms=_KEY_ROTATION_GRACE_PERIOD_MS - 1)
    assert second is not None
    assert second.key == first.key
    assert second.key_index == 0
    assert second.targets == (bob,)
    assert second.apply_after_ms == 0


def test_joiner_after_grace_period_rotates_key_for_everyone() -> None:
    """Joiner after grace period rotates key for everyone."""
    manager = _manager()
    alice = _member("@alice:example.org")
    first = manager.update_memberships([alice], now_ms=0)
    assert first is not None
    manager.mark_distributed(first)

    bob = _member("@bob:example.org")
    second = manager.update_memberships([alice, bob], now_ms=_KEY_ROTATION_GRACE_PERIOD_MS + 1)
    assert second is not None
    assert second.key != first.key
    assert second.key_index == 1
    assert set(second.targets) == {alice, bob}
    assert second.apply_after_ms == _USE_KEY_DELAY_MS


def test_leaver_rotates_key() -> None:
    """Leaver rotates key."""
    manager = _manager()
    alice = _member("@alice:example.org")
    bob = _member("@bob:example.org")
    first = manager.update_memberships([alice, bob], now_ms=0)
    assert first is not None
    manager.mark_distributed(first)

    second = manager.update_memberships([alice], now_ms=1)
    assert second is not None
    assert second.key_index == 1
    assert second.key != first.key
    assert second.targets == (alice,)
    assert second.apply_after_ms == _USE_KEY_DELAY_MS


def test_rejoin_with_new_membership_ts_is_treated_as_leave_and_join() -> None:
    """Rejoin with new membership ts is treated as leave and join."""
    manager = _manager()
    alice = _member("@alice:example.org", created_ts=0)
    first = manager.update_memberships([alice], now_ms=0)
    assert first is not None
    manager.mark_distributed(first)

    rejoined = _member("@alice:example.org", created_ts=500)
    second = manager.update_memberships([rejoined], now_ms=600)
    assert second is not None
    assert second.key_index == 1
    assert second.targets == (rejoined,)


def test_key_index_wraps_at_256() -> None:
    """Key index wraps at 256."""
    manager = _manager()
    alice = _member("@alice:example.org", created_ts=0)
    distribution = manager.update_memberships([alice], now_ms=0)
    assert distribution is not None
    manager.mark_distributed(distribution)
    for round_number in range(1, 257):
        rejoined = _member("@alice:example.org", created_ts=round_number)
        distribution = manager.update_memberships([rejoined], now_ms=round_number)
        assert distribution is not None
        manager.mark_distributed(distribution)
        assert distribution.key_index == round_number % 256
    assert distribution.key_index == 0  # 256 rotations starting from 0 wrap back to 0


def _received(key: bytes, index: int, sent_ts: int | None) -> ReceivedFrameKey:
    return ReceivedFrameKey(
        user_id="@alice:example.org",
        claimed_device_id="DEV",
        member_id="@alice:example.org:DEV",
        key_base64=base64.b64encode(key).decode(),
        key_index=index,
        sent_ts=sent_ts,
    )


def test_receive_decodes_key_and_drops_stale_duplicates() -> None:
    """Receive decodes key and drops stale duplicates."""
    manager = _manager()
    newer = manager.receive(_received(b"B" * 16, 0, sent_ts=200), now_ms=1_000)
    assert newer is not None
    assert newer.key == b"B" * 16
    assert newer.participant_identity == "@alice:example.org:DEV"
    stale = manager.receive(_received(b"A" * 16, 0, sent_ts=100), now_ms=1_001)
    assert stale is None
    different_index = manager.receive(_received(b"C" * 16, 1, sent_ts=100), now_ms=1_002)
    assert different_index is not None


def test_receive_rejects_invalid_base64() -> None:
    """Receive rejects invalid base64."""
    manager = _manager()
    bad = ReceivedFrameKey(
        user_id="@alice:example.org",
        claimed_device_id="DEV",
        member_id="@alice:example.org:DEV",
        key_base64="not-base64!!",
        key_index=0,
    )
    assert manager.receive(bad, now_ms=0) is None


def test_malformed_key_does_not_poison_the_dedup_filter() -> None:
    """A bad payload must not block a later valid key with an older sent_ts."""
    manager = _manager()
    bad = ReceivedFrameKey(
        user_id="@alice:example.org",
        claimed_device_id="DEV",
        member_id="@alice:example.org:DEV",
        key_base64="not-base64!!",
        key_index=0,
        sent_ts=500,
    )
    assert manager.receive(bad, now_ms=0) is None
    older_but_valid = manager.receive(_received(b"B" * 16, 0, sent_ts=400), now_ms=1)
    assert older_but_valid is not None
