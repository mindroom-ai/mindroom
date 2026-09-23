"""Origin-room publisher identity, typed authorization results, and bounded successful-decision caching."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.report_publishing.store import OriginRoomBinding


class ReportAuthorizationReason(StrEnum):
    """Stable origin-room authorization outcome categories."""

    AUTHORIZED = "authorized"
    VIEWER_NOT_JOINED = "viewer_not_joined"
    PUBLISHER_NOT_JOINED = "publisher_not_joined"
    PUBLISHER_IDENTITY_MISMATCH = "publisher_identity_mismatch"
    AUTHORIZATION_BACKEND_UNAVAILABLE = "authorization_backend_unavailable"


@dataclass(frozen=True)
class ReportAuthorizationDecision:
    """One origin-room authorization decision."""

    reason: ReportAuthorizationReason
    cache_hit: bool = False

    @property
    def authorized(self) -> bool:
        """Return whether report access is allowed."""
        return self.reason is ReportAuthorizationReason.AUTHORIZED

    @property
    def backend_unavailable(self) -> bool:
        """Return whether authorization could not reach authoritative state."""
        return self.reason is ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE


@dataclass(frozen=True)
class OriginRoomAuthorizationKey:
    """Security-relevant identity tuple for one cached decision."""

    origin_room: OriginRoomBinding
    viewer_matrix_user_id: str


def current_publisher_matrix_user_id(config: Config, runtime_paths: RuntimePaths, entity_name: str) -> str | None:
    """Return one configured entity's current Matrix ID, or None when it cannot publish or authorize reports."""
    try:
        return entity_identity_registry(config, runtime_paths).current_id(entity_name).full_id
    except (DuplicateManagedEntityIdentityError, KeyError, MissingManagedEntityAccountError):
        return None


class SuccessfulReportAuthorizationCache:
    """Short, bounded cache that coalesces and stores successful checks only."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 20.0,
        max_entries: int = 1024,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            msg = "Report authorization cache ttl_seconds must be positive."
            raise ValueError(msg)
        if max_entries <= 0:
            msg = "Report authorization cache max_entries must be positive."
            raise ValueError(msg)
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._successful: OrderedDict[OriginRoomAuthorizationKey, float] = OrderedDict()
        self._in_flight: dict[OriginRoomAuthorizationKey, asyncio.Task[ReportAuthorizationDecision]] = {}

    async def authorize(
        self,
        key: OriginRoomAuthorizationKey,
        check: Callable[[], Coroutine[Any, Any, ReportAuthorizationDecision]],
    ) -> ReportAuthorizationDecision:
        """Return cached success or coalesce one authoritative check."""
        now = self._monotonic()
        expires_at = self._successful.get(key)
        if expires_at is not None:
            if expires_at > now:
                self._successful.move_to_end(key)
                return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZED, cache_hit=True)
            del self._successful[key]

        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._run_check(key, check))
            self._in_flight[key] = task
        return await asyncio.shield(task)

    async def _run_check(
        self,
        key: OriginRoomAuthorizationKey,
        check: Callable[[], Coroutine[Any, Any, ReportAuthorizationDecision]],
    ) -> ReportAuthorizationDecision:
        """Own one authoritative check through cache update and cleanup."""
        try:
            decision = await check()
            if decision.authorized:
                self._successful[key] = self._monotonic() + self._ttl_seconds
                self._successful.move_to_end(key)
                while len(self._successful) > self._max_entries:
                    self._successful.popitem(last=False)
            return decision
        finally:
            if self._in_flight.get(key) is asyncio.current_task():
                self._in_flight.pop(key, None)
