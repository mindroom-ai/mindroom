"""External-trigger authority policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.main import Config


def is_external_trigger_administrator(config: Config, requester_id: str) -> bool:
    """Return whether one canonical or aliased requester may administer triggers."""
    canonical_requester = config.authorization.resolve_alias(requester_id)
    return (
        canonical_requester in config.administrators
        or canonical_requester in config.external_trigger_policy.admin_users
    )
