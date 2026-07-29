"""Pure refresh policy for one resolved knowledge availability.

Resolving an agent's knowledge for a query is a read, but a stale or failed index
also has to get itself rescheduled. This module owns the decision half of that:
given a resolved index, the current availability, and the cooldown bookkeeping,
it returns what should happen. It performs no scheduling and reads no globals, so
cooldowns, Git poll intervals, and failed-refresh retry fingerprints are testable
without a live scheduler. ``knowledge/utils.py`` owns the effect half: it probes
the scheduler, stamps the cooldown, and submits the refresh.
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
class _RefreshDecision:
    """What one resolved availability implies for background refresh scheduling.

    ``availability`` is what the turn should report, which can differ from the
    resolved availability: a READY index whose refresh is pending or in flight is
    reported STALE so the agent does not claim to have searched the latest
    contents. ``cooldown_key`` is set only when a refresh should be scheduled, and
    is the key the caller stamps to start the next cooldown window.
    """

    availability: KnowledgeAvailability
    cooldown_key: RefreshCooldownKey | None = None

    @property
    def schedule_refresh(self) -> bool:
        """Return whether the caller should submit a refresh for this decision."""
        return self.cooldown_key is not None


def _published_index_age_seconds(value: str | None) -> float | None:
    """Return how long ago an ISO timestamp was, or None when it is absent or unparsable."""
    if value is None:
        return None
    try:
        published_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return max((datetime.now(tz=UTC) - published_at).total_seconds(), 0.0)


def _git_poll_interval_seconds(lookup: PublishedIndexResolution, config: Config) -> float | None:
    git_config = config.get_knowledge_base_config(lookup.key.base_id).git
    if git_config is None:
        return None
    return max(float(git_config.poll_interval_seconds), 0.0)


def _git_poll_due(lookup: PublishedIndexResolution, config: Config) -> bool:
    if lookup.index is None:
        return False
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return False
    age_seconds = _published_index_age_seconds(
        lookup.index.state.last_refresh_at or lookup.index.state.last_published_at,
    )
    return age_seconds is None or age_seconds >= poll_interval_seconds


def ready_index_effective_availability(
    lookup: PublishedIndexResolution,
    config: Config,
) -> KnowledgeAvailability:
    """Return request-path availability for a ready index without eager rescans."""
    availability = lookup.availability
    if availability is KnowledgeAvailability.READY and lookup.index is not None and _git_poll_due(lookup, config):
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


def _refresh_on_access_cooldown_seconds(lookup: PublishedIndexResolution, config: Config) -> float:
    """Return READY refresh throttle without request-path source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    return max(poll_interval_seconds or _REFRESH_RETRY_COOLDOWN_SECONDS, 1.0)


def _refresh_on_access_due(lookup: PublishedIndexResolution, config: Config) -> bool:
    """Return whether READY on-access refresh should be scheduled without source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return True
    return _git_poll_due(lookup, config)


def _cooldown_elapsed(
    scheduled_at: Mapping[RefreshCooldownKey, float],
    key: RefreshCooldownKey,
    *,
    now: float,
    cooldown_seconds: float,
) -> bool:
    last_scheduled_at = scheduled_at.get(key)
    return last_scheduled_at is None or now - last_scheduled_at >= cooldown_seconds


def scheduler_probe_required(
    *,
    lookup: PublishedIndexResolution | None,
    availability: KnowledgeAvailability,
    config: Config,
) -> bool:
    """Return whether deciding this availability needs a scheduler-activity probe.

    A READY index that is not due for an on-access refresh decides to do nothing,
    so the request path must not pay for a scheduler probe to learn that.
    """
    if lookup is None:
        return False
    if availability is KnowledgeAvailability.READY:
        return lookup.schedule_refresh_on_access and _refresh_on_access_due(lookup, config)
    return True


def decide_refresh(
    *,
    lookup: PublishedIndexResolution | None,
    availability: KnowledgeAvailability,
    config: Config,
    runtime_paths: RuntimePaths,
    scheduler_is_refreshing: bool,
    now: float,
    scheduled_at: Mapping[RefreshCooldownKey, float],
) -> _RefreshDecision:
    """Decide whether one resolved availability should schedule a refresh, and what the turn sees."""
    if lookup is None:
        return _RefreshDecision(availability=availability)

    refresh_target = refresh_target_for_published_index_key(lookup.key)
    if availability is KnowledgeAvailability.READY:
        return _decide_ready_refresh(
            lookup=lookup,
            config=config,
            refresh_target=refresh_target,
            scheduler_is_refreshing=scheduler_is_refreshing,
            now=now,
            scheduled_at=scheduled_at,
        )

    if scheduler_is_refreshing:
        return _RefreshDecision(availability=availability)

    if availability is KnowledgeAvailability.INITIALIZING:
        settings: Hashable | None = lookup.key.indexing_settings
        cooldown_seconds = _REFRESH_RETRY_COOLDOWN_SECONDS
    else:
        settings = _refresh_retry_settings(lookup, config, runtime_paths, availability)
        cooldown_seconds = _refresh_cooldown_seconds(lookup, config, availability)

    cooldown_key = (refresh_target, availability, settings)
    if not _cooldown_elapsed(scheduled_at, cooldown_key, now=now, cooldown_seconds=cooldown_seconds):
        return _RefreshDecision(availability=availability)
    return _RefreshDecision(availability=availability, cooldown_key=cooldown_key)


def _decide_ready_refresh(
    *,
    lookup: PublishedIndexResolution,
    config: Config,
    refresh_target: KnowledgeRefreshTarget,
    scheduler_is_refreshing: bool,
    now: float,
    scheduled_at: Mapping[RefreshCooldownKey, float],
) -> _RefreshDecision:
    """Decide on-access refresh for an index that resolved READY."""
    if not lookup.schedule_refresh_on_access or not _refresh_on_access_due(lookup, config):
        return _RefreshDecision(availability=KnowledgeAvailability.READY)
    if scheduler_is_refreshing:
        return _RefreshDecision(availability=KnowledgeAvailability.STALE)

    cooldown_key = (refresh_target, KnowledgeAvailability.READY, lookup.key.indexing_settings)
    if not _cooldown_elapsed(
        scheduled_at,
        cooldown_key,
        now=now,
        cooldown_seconds=_refresh_on_access_cooldown_seconds(lookup, config),
    ):
        return _RefreshDecision(availability=KnowledgeAvailability.READY)
    return _RefreshDecision(availability=KnowledgeAvailability.STALE, cooldown_key=cooldown_key)
