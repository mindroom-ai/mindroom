"""Refresh policy decisions for one resolved knowledge availability.

Resolving an agent's knowledge for a query is a read, but a stale or failed index
also has to get itself rescheduled. This module owns the decision half of that;
``knowledge/utils.py`` owns the effect half, driving these functions in order:

1. ``refresh_trigger`` -- pure and cheap. Does this availability warrant a refresh
   at all, and what should the turn report while one is in flight? Returns None
   when nothing should happen, which is the common READY case.
2. The caller probes the scheduler, reached only when a trigger exists.
3. ``refresh_cooldown_key`` -- the one step that touches the filesystem. Deferred
   to here so an already-in-flight refresh never pays for it.
4. The caller samples the cooldown clock, and ``cooldown_elapsed`` -- pure -- says
   whether this key's throttle window has expired. The clock is sampled last, after
   the probe and the key, so that the timestamp the caller stamps records when the
   refresh was actually scheduled rather than some instant before it. Cadence does
   not depend on this: probe latency lands on both the stamp and the next
   comparison and cancels out.

Splitting it this way keeps a single source of truth for "should this refresh":
there is no second predicate that can drift out of sync with the first.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.redaction import embedded_http_userinfo
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    PublishedIndexResolution,
    refresh_target_for_published_index_key,
)

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_REFRESH_RETRY_COOLDOWN_SECONDS = 300.0
_EMBEDDED_GIT_USERINFO_FINGERPRINT_KEY = secrets.token_bytes(32)

RefreshCooldownKey = tuple[KnowledgeRefreshTarget, KnowledgeAvailability, "Hashable | None"]


@dataclass(frozen=True)
class _RefreshTrigger:
    """A refresh this availability warrants, pending a scheduler probe and its cooldown.

    ``availability_while_refreshing`` is what the turn should report once the
    refresh is in flight or newly queued. It differs from the resolved
    availability only for READY, which is downgraded to STALE so the agent does
    not claim to have searched contents a pending refresh may replace.
    """

    availability_while_refreshing: KnowledgeAvailability
    cooldown_seconds: float


def _published_index_age_seconds(value: str | None, *, wall_now: datetime) -> float | None:
    """Return how long ago an ISO timestamp was, or None when it is absent or unparsable."""
    if value is None:
        return None
    try:
        published_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return max((wall_now - published_at).total_seconds(), 0.0)


def _git_poll_interval_seconds(lookup: PublishedIndexResolution, config: Config) -> float | None:
    git_config = config.get_knowledge_base_config(lookup.key.base_id).git
    if git_config is None:
        return None
    return max(float(git_config.poll_interval_seconds), 0.0)


def _git_poll_due(lookup: PublishedIndexResolution, config: Config, *, wall_now: datetime) -> bool:
    if lookup.index is None:
        return False
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return False
    age_seconds = _published_index_age_seconds(
        lookup.index.state.last_refresh_at or lookup.index.state.last_published_at,
        wall_now=wall_now,
    )
    return age_seconds is None or age_seconds >= poll_interval_seconds


def ready_index_effective_availability(
    lookup: PublishedIndexResolution,
    config: Config,
    *,
    wall_now: datetime,
) -> KnowledgeAvailability:
    """Return request-path availability for a ready index without eager rescans."""
    availability = lookup.availability
    if (
        availability is KnowledgeAvailability.READY
        and lookup.index is not None
        and _git_poll_due(lookup, config, wall_now=wall_now)
    ):
        availability = KnowledgeAvailability.STALE
    return availability


def _refresh_cooldown_seconds(
    lookup: PublishedIndexResolution,
    config: Config,
    availability: KnowledgeAvailability,
) -> float:
    if availability is not KnowledgeAvailability.STALE:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    return max(poll_interval_seconds, 1.0)


def _refresh_on_access_cooldown_seconds(lookup: PublishedIndexResolution, config: Config) -> float:
    """Return READY refresh throttle without request-path source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    return max(poll_interval_seconds or _REFRESH_RETRY_COOLDOWN_SECONDS, 1.0)


def _refresh_on_access_due(lookup: PublishedIndexResolution, config: Config, *, wall_now: datetime) -> bool:
    """Return whether READY on-access refresh should be scheduled without source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return True
    return _git_poll_due(lookup, config, wall_now=wall_now)


def refresh_trigger(
    *,
    lookup: PublishedIndexResolution,
    availability: KnowledgeAvailability,
    config: Config,
    wall_now: datetime,
) -> _RefreshTrigger | None:
    """Return the refresh this resolved availability warrants, or None to do nothing.

    Pure, and cheap enough to run on every knowledge read: a READY index that is
    not yet due for an on-access refresh returns None here, before the caller pays
    for a scheduler probe.
    """
    if availability is KnowledgeAvailability.READY:
        if not lookup.schedule_refresh_on_access or not _refresh_on_access_due(lookup, config, wall_now=wall_now):
            return None
        return _RefreshTrigger(
            availability_while_refreshing=KnowledgeAvailability.STALE,
            cooldown_seconds=_refresh_on_access_cooldown_seconds(lookup, config),
        )

    cooldown_seconds = (
        _REFRESH_RETRY_COOLDOWN_SECONDS
        if availability is KnowledgeAvailability.INITIALIZING
        else _refresh_cooldown_seconds(lookup, config, availability)
    )
    return _RefreshTrigger(availability_while_refreshing=availability, cooldown_seconds=cooldown_seconds)


def refresh_cooldown_key(
    lookup: PublishedIndexResolution,
    config: Config,
    runtime_paths: RuntimePaths,
    availability: KnowledgeAvailability,
) -> RefreshCooldownKey:
    """Return the throttle key identifying one refresh attempt.

    A REFRESH_FAILED key folds in a fingerprint of the Git credential that could
    fix the retry, so rotating that credential retries immediately instead of
    waiting out the cooldown. Building it reads the credentials-manager memo and
    ``stat()``s a file, which is why the caller defers this until after the
    scheduler probe rules out an in-flight refresh.
    """
    refresh_target = refresh_target_for_published_index_key(lookup.key)
    if availability in (KnowledgeAvailability.READY, KnowledgeAvailability.INITIALIZING):
        return (refresh_target, availability, lookup.key.indexing_settings)
    return (refresh_target, availability, _refresh_retry_settings(lookup, config, runtime_paths, availability))


def cooldown_elapsed(
    scheduled_at: Mapping[RefreshCooldownKey, float],
    key: RefreshCooldownKey,
    *,
    monotonic_now: float,
    cooldown_seconds: float,
) -> bool:
    """Return whether this key's throttle window has expired."""
    last_scheduled_at = scheduled_at.get(key)
    return last_scheduled_at is None or monotonic_now - last_scheduled_at >= cooldown_seconds


def _refresh_retry_settings(
    lookup: PublishedIndexResolution,
    config: Config,
    runtime_paths: RuntimePaths,
    availability: KnowledgeAvailability,
) -> Hashable | None:
    if availability is KnowledgeAvailability.CONFIG_MISMATCH:
        return lookup.key.indexing_settings
    if availability is KnowledgeAvailability.REFRESH_FAILED:
        return (lookup.key.indexing_settings, *_failed_refresh_retry_fingerprint(lookup, config, runtime_paths))
    return None


def _failed_refresh_retry_fingerprint(
    lookup: PublishedIndexResolution,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, ...]:
    """Return a secret-free fingerprint for Git refresh/auth settings that can fix a failed retry."""
    git_config = config.get_knowledge_base_config(lookup.key.base_id).git
    if git_config is None:
        return ()

    fingerprint = [
        "git-refresh",
        f"credentials_service:{git_config.credentials_service or ''}",
        f"sync_timeout_seconds:{git_config.sync_timeout_seconds}",
        f"embedded_userinfo:{_embedded_userinfo_fingerprint(git_config.repo_url)}",
    ]
    if git_config.credentials_service is None:
        return tuple(fingerprint)

    credentials_path = get_runtime_shared_credentials_manager(runtime_paths).get_credentials_path(
        git_config.credentials_service,
    )
    try:
        credentials_stat = credentials_path.stat()
    except OSError:
        fingerprint.extend(("credentials_mtime_ns:", "credentials_size:"))
    else:
        fingerprint.extend(
            (
                f"credentials_mtime_ns:{credentials_stat.st_mtime_ns}",
                f"credentials_size:{credentials_stat.st_size}",
            ),
        )
    return tuple(fingerprint)


def _embedded_userinfo_fingerprint(repo_url: str) -> str:
    userinfo = embedded_http_userinfo(repo_url)
    if userinfo is None:
        return ""
    username, secret = userinfo
    payload = f"{username}\0{secret}".encode()
    return hmac.new(_EMBEDDED_GIT_USERINFO_FINGERPRINT_KEY, payload, hashlib.sha256).hexdigest()
