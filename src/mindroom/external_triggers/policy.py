"""External-trigger authority policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
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
