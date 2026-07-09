"""Pure room invitation helpers for the orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.entity_resolution import mindroom_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix_identifiers import split_concrete_matrix_user_ids

if TYPE_CHECKING:
    from mindroom.config.main import RuntimeConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _filter_concrete_matrix_user_ids(user_ids: set[str], *, warning_message: str) -> set[str]:
    """Return inviteable Matrix user IDs and log skipped wildcard or placeholder entries."""
    concrete_user_ids, skipped = split_concrete_matrix_user_ids(user_ids)
    if skipped:
        logger.warning(warning_message, user_ids=skipped)
    return set(concrete_user_ids)


def get_authorized_user_ids_to_invite(config: RuntimeConfig) -> set[str]:
    """Collect Matrix users from authorization config that can be invited."""
    user_ids = set(config.authorization.global_users)
    for room_users in config.authorization.room_permissions.values():
        user_ids.update(room_users)
    return _filter_concrete_matrix_user_ids(
        user_ids,
        warning_message="Skipping non-concrete authorization user IDs for invites",
    )


def get_root_space_user_ids_to_invite(config: RuntimeConfig, runtime_paths: RuntimePaths) -> set[str]:
    """Collect Matrix users that should be invited to the private root Space."""
    user_ids = _filter_concrete_matrix_user_ids(
        set(config.authorization.global_users),
        warning_message="Skipping non-concrete global user IDs for root space invites",
    )
    internal_user_id = mindroom_user_id(config, runtime_paths)
    if internal_user_id is not None:
        user_ids.add(internal_user_id)
    return user_ids
