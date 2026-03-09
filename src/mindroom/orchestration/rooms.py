"""Pure room invitation helpers for the orchestrator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config

logger = get_logger(__name__)


def _is_concrete_matrix_user_id(user_id: str) -> bool:
    """Return whether this string is a concrete Matrix user ID."""
    return (
        user_id.startswith("@") and ":" in user_id and "*" not in user_id and "?" not in user_id and " " not in user_id
    )


def _filter_concrete_matrix_user_ids(user_ids: set[str], *, warning_message: str) -> set[str]:
    """Return inviteable Matrix user IDs and log skipped wildcard or placeholder entries."""
    concrete_user_ids = {user_id for user_id in user_ids if _is_concrete_matrix_user_id(user_id)}
    skipped = sorted(user_ids - concrete_user_ids)
    if skipped:
        logger.warning(warning_message, user_ids=skipped)
    return concrete_user_ids


def _get_authorized_user_ids_to_invite(config: Config) -> set[str]:
    """Collect Matrix users from authorization config that can be invited."""
    user_ids = set(config.authorization.global_users)
    for room_users in config.authorization.room_permissions.values():
        user_ids.update(room_users)
    return _filter_concrete_matrix_user_ids(
        user_ids,
        warning_message="Skipping non-concrete authorization user IDs for invites",
    )


def _get_root_space_user_ids_to_invite(config: Config) -> set[str]:
    """Collect Matrix users that should be invited to the private root Space."""
    user_ids = _filter_concrete_matrix_user_ids(
        set(config.authorization.global_users),
        warning_message="Skipping non-concrete global user IDs for root space invites",
    )
    internal_user_id = config.get_mindroom_user_id()
    if internal_user_id is not None:
        user_ids.add(internal_user_id)
    return user_ids


async def _ensure_bot_room_memberships(bots: list[AgentBot | TeamBot]) -> None:
    """Ensure each bot has joined its assigned rooms."""
    await asyncio.gather(*(bot.ensure_rooms() for bot in bots))
