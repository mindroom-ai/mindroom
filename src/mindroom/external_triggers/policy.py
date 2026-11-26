"""External-trigger authority policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.requester_identity import equivalent_requester_ids, resolve_human_requester_alias

if TYPE_CHECKING:
    from mindroom.config.external_trigger_policy import ExternalEventSourceConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def is_external_trigger_administrator(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str,
) -> bool:
    """Return whether one canonical or aliased requester may administer triggers."""
    canonical_requester = resolve_human_requester_alias(requester_id, config, runtime_paths)
    return (
        canonical_requester in config.administrators
        or canonical_requester in config.external_trigger_policy.admin_users
    )


def is_source_subscriber_allowed(
    source: ExternalEventSourceConfig,
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether owner_user_id may subscribe to or receive events from source."""
    if is_external_trigger_administrator(config, runtime_paths, owner_user_id):
        return True
    owner_ids = {owner_user_id, *equivalent_requester_ids(owner_user_id, config, runtime_paths)}
    for subscriber in source.subscribers:
        subscriber_ids = {subscriber, *equivalent_requester_ids(subscriber, config, runtime_paths)}
        if owner_ids & subscriber_ids:
            return True
    return False
