"""Per-sender media frame key management for MatrixRTC calls.

Ports the key policy of matrix-js-sdk's ``RTCEncryptionManager``: every
participant encrypts their outbound media with their own 16-byte key,
distributed to the other participants over an encrypted transport. Keys
rotate when someone leaves (so leavers lose access) and when someone joins
after a grace period (so joiners cannot decrypt earlier media). Indices
cycle through 0-255.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.matrix_rtc.events import CallMember, ReceivedFrameKey

#: Wait this long after distributing a rotated key before encrypting with it,
#: so slower participants receive it before frames arrive.
_USE_KEY_DELAY_MS = 1000

#: Skip rotation for joiners when the current key is younger than this.
_KEY_ROTATION_GRACE_PERIOD_MS = 10_000

_KEY_SIZE_BYTES = 16
_KEY_INDEX_MODULUS = 256


@dataclass(frozen=True)
class _KeyDistribution:
    """A planned key send: give ``targets`` the key, then start using it."""

    key: bytes
    key_index: int
    targets: tuple[CallMember, ...]
    #: Wait this long before encrypting outbound media with the key. Zero for
    #: keys that are already in use and are only being shared with joiners.
    apply_after_ms: int

    @property
    def key_base64(self) -> str:
        return base64.b64encode(self.key).decode("ascii")


@dataclass(frozen=True)
class _InboundFrameKey:
    """A validated remote key ready to hand to the media layer."""

    participant_identity: str
    key_index: int
    key: bytes


@dataclass(frozen=True)
class _SharedWith:
    user_id: str
    device_id: str
    membership_ts: int


@dataclass
class FrameKeyManager:
    """Tracks the outbound key lifecycle and validates inbound keys."""

    own_user_id: str
    own_device_id: str
    _key: bytes | None = field(default=None, init=False)
    _key_index: int = field(default=0, init=False)
    _key_created_ms: int = field(default=0, init=False)
    _key_activation_ms: int = field(default=0, init=False)
    _shared_with: list[_SharedWith] = field(default_factory=list, init=False)
    _newest_inbound_ts: dict[tuple[str, str, int], int] = field(default_factory=dict, init=False)

    def update_memberships(self, members: list[CallMember], now_ms: int) -> _KeyDistribution | None:
        """Reconcile the outbound key with the current remote memberships.

        Returns the distribution to perform, or ``None`` when nothing changed.
        Callers must report a completed send via :meth:`mark_distributed`.
        """
        remote = [
            _SharedWith(user_id=m.user_id, device_id=m.device_id, membership_ts=m.created_ts)
            for m in members
            if not (m.user_id == self.own_user_id and m.device_id == self.own_device_id)
        ]
        members_by_identity = {
            (m.user_id, m.device_id, m.created_ts): m
            for m in members
            if not (m.user_id == self.own_user_id and m.device_id == self.own_device_id)
        }

        if self._key is None:
            # First key: usable immediately, shared with everyone present.
            self._key = secrets.token_bytes(_KEY_SIZE_BYTES)
            self._key_index = 0
            self._key_created_ms = now_ms
            self._key_activation_ms = now_ms
            return _KeyDistribution(
                key=self._key,
                key_index=self._key_index,
                targets=tuple(members_by_identity.values()),
                apply_after_ms=0,
            )

        # A member that rejoined with a new membership event needs the key again.
        still_valid_shares = [s for s in self._shared_with if s in remote]
        any_left = len(still_valid_shares) < len(self._shared_with)
        joined = [identity for identity in remote if identity not in still_valid_shares]

        if any_left:
            return self._rotate(members_by_identity, remote, now_ms)
        if joined:
            if now_ms - self._key_created_ms < _KEY_ROTATION_GRACE_PERIOD_MS:
                targets = tuple(members_by_identity[(s.user_id, s.device_id, s.membership_ts)] for s in joined)
                return _KeyDistribution(
                    key=self._key,
                    key_index=self._key_index,
                    targets=targets,
                    apply_after_ms=max(0, self._key_activation_ms - now_ms),
                )
            return self._rotate(members_by_identity, remote, now_ms)
        return None

    def _rotate(
        self,
        members_by_identity: dict[tuple[str, str, int], CallMember],
        remote: list[_SharedWith],
        now_ms: int,
    ) -> _KeyDistribution:
        self._key = secrets.token_bytes(_KEY_SIZE_BYTES)
        self._key_index = (self._key_index + 1) % _KEY_INDEX_MODULUS
        self._key_created_ms = now_ms
        self._key_activation_ms = now_ms + _USE_KEY_DELAY_MS
        self._shared_with = []
        del remote  # rotation always re-shares with every current member
        return _KeyDistribution(
            key=self._key,
            key_index=self._key_index,
            targets=tuple(members_by_identity.values()),
            apply_after_ms=_USE_KEY_DELAY_MS,
        )

    def is_current_outbound_key(self, key: bytes, key_index: int) -> bool:
        """Return whether a delayed activation still targets the newest key."""
        return self._key == key and self._key_index == key_index

    def mark_distributed(
        self,
        distribution: _KeyDistribution,
        delivered: tuple[CallMember, ...] | None = None,
    ) -> None:
        """Record successful sends so unavailable targets are retried later."""
        for member in distribution.targets if delivered is None else delivered:
            share = _SharedWith(user_id=member.user_id, device_id=member.device_id, membership_ts=member.created_ts)
            if share not in self._shared_with:
                self._shared_with.append(share)

    def receive(
        self,
        received: ReceivedFrameKey,
        now_ms: int,
        *,
        participant_identity: str,
    ) -> _InboundFrameKey | None:
        """Validate and decode a remote key, dropping stale duplicates.

        Ordering uses the local receipt time because the event's ``sent_ts``
        is supplied by the remote sender and cannot be trusted.
        """
        if isinstance(received.key_index, bool) or not 0 <= received.key_index < _KEY_INDEX_MODULUS:
            return None
        filter_key = (received.user_id, received.claimed_device_id, received.key_index)
        newest = self._newest_inbound_ts.get(filter_key)
        if newest is not None and now_ms < newest:
            return None
        try:
            key = base64.b64decode(received.key_base64, validate=True)
        except ValueError:
            return None
        if len(key) != _KEY_SIZE_BYTES:
            return None
        # Record the dedup timestamp only for keys that validate, so a
        # malformed payload cannot poison the filter against a good retry.
        self._newest_inbound_ts[filter_key] = now_ms
        return _InboundFrameKey(
            participant_identity=participant_identity,
            key_index=received.key_index,
            key=key,
        )
