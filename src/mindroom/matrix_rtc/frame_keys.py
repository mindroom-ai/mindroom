"""Per-sender media frame key management for MatrixRTC calls.

Ports the key policy of matrix-js-sdk's ``RTCEncryptionManager``: every
participant encrypts their outbound media with their own key, distributed to
the other participants over an encrypted transport. We send 16-byte keys for
SDK 41 compatibility and accept both 16- and 32-byte keys. Keys rotate when
someone leaves (so leavers lose access) and when someone joins
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

_OUTBOUND_KEY_SIZE_BYTES = 16
_INBOUND_KEY_SIZES = frozenset({16, 32})
_KEY_INDEX_MODULUS = 256


@dataclass(frozen=True)
class _KeyDistribution:
    """A planned key send: give ``targets`` the key, then start using it."""

    key: bytes
    key_index: int
    targets: tuple[CallMember, ...]
    #: Counts rotations without wrapping, so a newer key always compares newer than an older one.
    generation: int

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


def _share(member: CallMember) -> _SharedWith:
    return _SharedWith(user_id=member.user_id, device_id=member.device_id, membership_ts=member.created_ts)


@dataclass
class FrameKeyManager:
    """Tracks the outbound key lifecycle and validates inbound keys."""

    own_user_id: str
    own_device_id: str
    _key: bytes | None = field(default=None, init=False)
    _key_index: int = field(default=0, init=False)
    _key_created_ms: int = field(default=0, init=False)
    _key_generation: int = field(default=0, init=False)
    _armed_generation: int = field(default=-1, init=False)
    # Sets keep each distribution linear in the roster, which a caller can inflate with planted devices.
    _shared_with: set[_SharedWith] = field(default_factory=set, init=False)
    _exposed_to: set[_SharedWith] = field(default_factory=set, init=False)
    _newest_inbound_ts: dict[tuple[str, str, int], int] = field(default_factory=dict, init=False)

    def update_memberships(self, members: list[CallMember], now_ms: int) -> _KeyDistribution | None:
        """Reconcile the outbound key with the current remote memberships.

        Returns the distribution to perform, or ``None`` when nothing changed.
        Callers must report a completed send via :meth:`mark_distributed`.
        """
        members_by_share = {
            _share(m): m for m in members if not (m.user_id == self.own_user_id and m.device_id == self.own_device_id)
        }

        if self._key is None:
            # First key: usable immediately, shared with everyone present.
            self._key = secrets.token_bytes(_OUTBOUND_KEY_SIZE_BYTES)
            self._key_index = 0
            self._key_created_ms = now_ms
            return _KeyDistribution(
                key=self._key,
                key_index=self._key_index,
                targets=tuple(members_by_share.values()),
                generation=self._key_generation,
            )

        # A member that rejoined with a new membership event needs the key again.
        any_left = any(share not in members_by_share for share in self._exposed_to)
        joined = [member for share, member in members_by_share.items() if share not in self._shared_with]

        if any_left:
            return self._rotate(members_by_share, now_ms)
        if joined:
            # A device this key already targeted but did not reach gets it again, however late.
            retry_only = all(_share(member) in self._exposed_to for member in joined)
            if retry_only or now_ms - self._key_created_ms < _KEY_ROTATION_GRACE_PERIOD_MS:
                return _KeyDistribution(
                    key=self._key,
                    key_index=self._key_index,
                    targets=tuple(joined),
                    generation=self._key_generation,
                )
            return self._rotate(members_by_share, now_ms)
        return None

    def _rotate(self, members_by_share: dict[_SharedWith, CallMember], now_ms: int) -> _KeyDistribution:
        self._key = secrets.token_bytes(_OUTBOUND_KEY_SIZE_BYTES)
        self._key_index = (self._key_index + 1) % _KEY_INDEX_MODULUS
        self._key_created_ms = now_ms
        self._key_generation += 1
        self._shared_with = set()
        self._exposed_to = set()
        # Rotation always re-shares with every current member.
        return _KeyDistribution(
            key=self._key,
            key_index=self._key_index,
            targets=tuple(members_by_share.values()),
            generation=self._key_generation,
        )

    def mark_distributed(
        self,
        distribution: _KeyDistribution,
        delivered: tuple[CallMember, ...] | None = None,
    ) -> bool:
        """Record successful sends so unavailable targets are retried later.

        Returns whether this ended the key generation's first send attempt, which fixes its one
        activation deadline. Callers report a raised send too, with nothing delivered: like a send
        that reached nobody, it still retires the old key (callers hear silence until a retry lands).
        """
        first_send = distribution.generation > self._armed_generation
        self._armed_generation = max(self._armed_generation, distribution.generation)
        self.mark_exposed(distribution)
        self._shared_with.update(
            _share(member) for member in (distribution.targets if delivered is None else delivered)
        )
        return first_send

    def mark_exposed(self, distribution: _KeyDistribution) -> None:
        """Record every device that may have received this key before transport returns."""
        self._exposed_to.update(_share(member) for member in distribution.targets)

    def receive(
        self,
        received: ReceivedFrameKey,
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
        if newest is not None and received.received_at_ms < newest:
            return None
        # MSC4143 specifies unpadded base64; SDK 41 and 45 send it padded. Accept exactly those two spellings.
        padding = "" if received.key_base64.endswith("=") else "=" * (-len(received.key_base64) % 4)
        try:
            key = base64.b64decode(received.key_base64 + padding, validate=True)
        except ValueError:
            return None
        if len(key) not in _INBOUND_KEY_SIZES:
            return None
        # Record the dedup timestamp only for keys that validate, so a
        # malformed payload cannot poison the filter against a good retry.
        self._newest_inbound_ts[filter_key] = received.received_at_ms
        return _InboundFrameKey(
            participant_identity=participant_identity,
            key_index=received.key_index,
            key=key,
        )
