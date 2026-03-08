"""Matrix room and membership reconciliation for the orchestrator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID, extract_server_name_from_homeserver
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.orchestrator import MultiAgentOrchestrator

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


async def _setup_rooms_and_memberships(
    self: MultiAgentOrchestrator,
    bots: list[AgentBot | TeamBot],
) -> None:
    """Setup rooms and ensure all bots have correct memberships."""
    room_ids = await self._ensure_rooms_exist()
    await self._ensure_root_space(room_ids)

    config = self._require_config()
    for bot in bots:
        room_aliases = self.get_rooms_for_entity(bot.agent_name, config)
        bot.rooms = self.resolve_room_aliases(room_aliases)

    async def _ensure_internal_user_memberships() -> None:
        all_rooms = self.load_rooms()
        all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
        if all_room_ids and config.mindroom_user is not None:
            await self.ensure_user_in_rooms(self.matrix_homeserver, all_room_ids)

    await self._ensure_room_invitations()
    await _ensure_internal_user_memberships()
    await _ensure_bot_room_memberships(bots)

    if any(bot.agent_name == ROUTER_AGENT_NAME for bot in bots):
        room_ids = await self._ensure_rooms_exist()
        await self._ensure_root_space(room_ids)

    await self._ensure_room_invitations()
    await _ensure_internal_user_memberships()

    follow_up_bots = [bot for bot in bots if bot.agent_name != ROUTER_AGENT_NAME]
    if follow_up_bots:
        await _ensure_bot_room_memberships(follow_up_bots)

    logger.info("All agents have joined their configured rooms")


async def _ensure_bot_room_memberships(bots: list[AgentBot | TeamBot]) -> None:
    """Ensure each bot has joined its assigned rooms."""
    await asyncio.gather(*(bot.ensure_rooms() for bot in bots))


def _get_router_bot(self: MultiAgentOrchestrator) -> AgentBot | TeamBot | None:
    """Return the router bot when it exists and has an active client."""
    router_bot = self.agent_bots.get(ROUTER_AGENT_NAME)
    if router_bot is None:
        logger.warning("Router not available")
        return None
    if router_bot.client is None:
        logger.warning("Router client not available")
        return None
    return router_bot


async def _ensure_rooms_exist(self: MultiAgentOrchestrator) -> dict[str, str]:
    """Ensure all configured rooms exist, creating them if necessary."""
    router_bot = _get_router_bot(self)
    if router_bot is None:
        return {}

    config = self._require_config()
    room_ids = await self.ensure_all_rooms_exist(router_bot.client, config)
    logger.info(f"Ensured existence of {len(room_ids)} rooms")
    return room_ids


async def _ensure_root_space(self: MultiAgentOrchestrator, room_ids: dict[str, str] | None = None) -> None:
    """Ensure the optional root Matrix Space exists and link the current managed rooms."""
    router_bot = _get_router_bot(self)
    if router_bot is None:
        return

    config = self._require_config()
    if not config.matrix_space.enabled:
        return

    normalized_room_ids = room_ids if isinstance(room_ids, dict) else {}
    root_space_id = await self.ensure_root_space(router_bot.client, config, normalized_room_ids)
    if root_space_id is None:
        return

    invite_user_ids = _get_root_space_user_ids_to_invite(config)
    if not invite_user_ids:
        return

    current_members = await self.get_room_members(router_bot.client, root_space_id)
    for user_id in sorted(invite_user_ids):
        if user_id in current_members:
            continue
        success = await self.invite_to_room(router_bot.client, root_space_id, user_id)
        if success:
            logger.info(f"Invited user {user_id} to root space {root_space_id}")
        else:
            logger.warning(f"Failed to invite user {user_id} to root space {root_space_id}")


async def _invite_user_if_missing(
    self: MultiAgentOrchestrator,
    room_id: str,
    user_id: str,
    current_members: set[str],
    *,
    success_message: str,
    failure_message: str,
) -> None:
    """Invite one user if they are not already a member."""
    router_bot = _get_router_bot(self)
    if router_bot is None:
        return
    if user_id in current_members:
        return
    success = await self.invite_to_room(router_bot.client, room_id, user_id)
    if success:
        logger.info(success_message)
        current_members.add(user_id)
    else:
        logger.warning(failure_message)


async def _invite_internal_user_to_rooms(
    self: MultiAgentOrchestrator,
    config: Config,
    joined_rooms: list[str],
    authorized_user_ids: set[str],
) -> set[str]:
    """Invite the configured internal user to all joined rooms when needed."""
    router_bot = _get_router_bot(self)
    if router_bot is None:
        return authorized_user_ids
    assert router_bot.client is not None

    state = self.matrix_state_cls.load()
    user_account = state.get_account(INTERNAL_USER_ACCOUNT_KEY)
    if config.mindroom_user is None or not user_account:
        return authorized_user_ids

    server_name = extract_server_name_from_homeserver(self.matrix_homeserver)
    user_id = MatrixID.from_username(user_account.username, server_name).full_id
    authorized_user_ids.discard(user_id)
    for room_id in joined_rooms:
        room_members = await self.get_room_members(router_bot.client, room_id)
        await _invite_user_if_missing(
            self,
            room_id,
            user_id,
            room_members,
            success_message=f"Invited user {user_id} to room {room_id}",
            failure_message=f"Failed to invite user {user_id} to room {room_id}",
        )
    return authorized_user_ids


async def _invite_authorized_users_to_room(
    self: MultiAgentOrchestrator,
    room_id: str,
    current_members: set[str],
    authorized_user_ids: set[str],
    config: Config,
) -> None:
    """Invite authorized human users who can access a given room."""
    for authorized_user_id in authorized_user_ids:
        if not self.is_authorized_sender(authorized_user_id, config, room_id):
            continue
        await _invite_user_if_missing(
            self,
            room_id,
            authorized_user_id,
            current_members,
            success_message=f"Invited authorized user {authorized_user_id} to room {room_id}",
            failure_message=f"Failed to invite authorized user {authorized_user_id} to room {room_id}",
        )


async def _invite_configured_bots_to_room(
    self: MultiAgentOrchestrator,
    room_id: str,
    current_members: set[str],
    configured_bots: Iterable[str],
    server_name: str,
) -> None:
    """Invite all configured bots for a room."""
    for bot_username in configured_bots:
        bot_user_id = MatrixID.from_username(bot_username, server_name).full_id
        await _invite_user_if_missing(
            self,
            room_id,
            bot_user_id,
            current_members,
            success_message=f"Invited {bot_username} to room {room_id}",
            failure_message=f"Failed to invite {bot_username} to room {room_id}",
        )


async def _ensure_room_invitations(self: MultiAgentOrchestrator) -> None:
    """Ensure all agents and the user are invited to their configured rooms."""
    router_bot = _get_router_bot(self)
    if router_bot is None:
        return

    config = self.config
    if not config:
        logger.warning("No configuration available, cannot ensure room invitations")
        return

    joined_rooms = await self.get_joined_rooms(router_bot.client)
    if not joined_rooms:
        return

    server_name = extract_server_name_from_homeserver(self.matrix_homeserver)
    authorized_user_ids = _get_authorized_user_ids_to_invite(config)
    authorized_user_ids = await _invite_internal_user_to_rooms(
        self,
        config,
        joined_rooms,
        authorized_user_ids,
    )

    for room_id in joined_rooms:
        configured_bots = config.get_configured_bots_for_room(room_id)
        if not configured_bots:
            continue

        current_members = await self.get_room_members(router_bot.client, room_id)
        await _invite_authorized_users_to_room(self, room_id, current_members, authorized_user_ids, config)
        await _invite_configured_bots_to_room(self, room_id, current_members, configured_bots, server_name)

    logger.info("Ensured room invitations for all configured agents and authorized users")
