"""Pure room invitation helpers for the orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.access_policy import resolve_room_policy
from mindroom.logging_config import get_logger
from mindroom.matrix.state import MatrixState
from mindroom.matrix_identifiers import split_concrete_matrix_user_ids

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _filter_concrete_matrix_user_ids(user_ids: set[str], *, warning_message: str) -> set[str]:
    """Return inviteable Matrix user IDs and log skipped wildcard or placeholder entries."""
    concrete_user_ids, skipped = split_concrete_matrix_user_ids(user_ids)
    if skipped:
        logger.warning(warning_message, user_ids=skipped)
    return set(concrete_user_ids)


def get_room_user_ids_to_invite(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return the membership invitation roster for one room."""
    state = MatrixState.load(runtime_paths=runtime_paths)
    room_keys = sorted(room_key for room_key, room in state.rooms.items() if room.room_id == room_id)
    if len(room_keys) != 1:
        logger.warning(
            "membership_room_invites_skipped_unresolved_room",
            room_id=room_id,
            matching_room_keys=room_keys,
        )
        return set()
    policy = resolve_room_policy(config, room_keys[0])
    return _filter_concrete_matrix_user_ids(
        set(policy.invite_users),
        warning_message="Skipping non-concrete membership invite user IDs",
    )


def get_root_space_user_ids_to_invite(config: Config, runtime_paths: RuntimePaths) -> set[str]:
    """Collect Matrix users that should be invited to the private root Space."""
    del runtime_paths
    configured_room_keys = sorted(
        room_key for room_key in config.get_all_configured_rooms() if not room_key.startswith(("!", "#"))
    )
    user_ids = {
        user_id for room_key in configured_room_keys for user_id in resolve_room_policy(config, room_key).invite_users
    }
    return _filter_concrete_matrix_user_ids(
        user_ids,
        warning_message="Skipping non-concrete membership root-space invite user IDs",
    )


def warn_about_foreign_homeserver_authorities(config: Config, runtime_paths: RuntimePaths) -> None:
    """Warn when administrators or managed-room invitees and admins live on another homeserver."""
    server_name = config.get_domain(runtime_paths)
    room_policies = [
        resolve_room_policy(config, room_key)
        for room_key in config.get_all_configured_rooms()
        if not room_key.startswith(("!", "#"))
    ]
    user_ids = [
        *config.administrators,
        *(user_id for policy in room_policies for user_id in (*policy.invite_users, *policy.admins)),
    ]
    concrete_user_ids, _ = split_concrete_matrix_user_ids(user_ids)
    foreign_user_ids = sorted(
        user_id for user_id in concrete_user_ids if user_id.split(":", 1)[1].lower() != server_name.lower()
    )
    if foreign_user_ids:
        logger.warning(
            "Administrators, room invitees, or room admins are on another homeserver; remove them unless you trust them",
            server_name=server_name,
            user_ids=foreign_user_ids,
        )
