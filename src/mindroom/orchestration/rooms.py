"""Managed room setup and the invitation rosters it uses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.access_policy import resolve_room_policy
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import configured_bot_user_ids_for_room, is_configured_room
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_members, invite_to_room
from mindroom.matrix.identity import managed_account_user_id
from mindroom.matrix.rooms import (
    ensure_all_rooms_exist,
    ensure_root_space,
    ensure_server_emoji_room,
    ensure_user_in_rooms,
    reconcile_managed_rooms,
)
from mindroom.matrix.state import load_rooms, matrix_state_for_runtime, resolve_room_aliases
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.matrix_identifiers import extract_server_name_from_homeserver, split_concrete_matrix_user_ids

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot

logger = get_logger(__name__)


def _filter_concrete_matrix_user_ids(user_ids: set[str], *, warning_message: str) -> set[str]:
    """Return inviteable Matrix user IDs and log skipped wildcard or placeholder entries."""
    concrete_user_ids, skipped = split_concrete_matrix_user_ids(user_ids)
    if skipped:
        logger.warning(warning_message, user_ids=skipped)
    return set(concrete_user_ids)


def _room_user_ids_to_invite(
    config: Config,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return the membership invitation roster for one room."""
    state = matrix_state_for_runtime(runtime_paths)
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


def _root_space_user_ids_to_invite(config: Config) -> set[str]:
    """Collect Matrix users that should be invited to the private root Space."""
    user_ids = {
        user_id
        for room_key in config.managed_room_keys()
        for user_id in resolve_room_policy(config, room_key).invite_users
    }
    return _filter_concrete_matrix_user_ids(
        user_ids,
        warning_message="Skipping non-concrete membership root-space invite user IDs",
    )


def warn_about_foreign_homeserver_authorities(config: Config, runtime_paths: RuntimePaths) -> None:
    """Warn when administrators or managed-room invitees and admins live on another homeserver."""
    server_name = config.get_domain(runtime_paths)
    room_policies = [resolve_room_policy(config, room_key) for room_key in config.managed_room_keys()]
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


@dataclass
class RoomSetup:
    """Create managed rooms, reconcile their policy, and bring bots into their rooms through the router."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    # The router's client when it runs; managed room work runs through its account.
    router_client: Callable[[], nio.AsyncClient | None]
    refresh_agent_reply_memberships: Callable[[], Awaitable[None]]

    async def setup(self, bots: list[AgentBot]) -> None:
        """Setup rooms and ensure all bots have correct memberships.

        This shared flow is used during both initial startup and config updates.
        """
        # Ensure all configured rooms exist before reconciling memberships.
        room_ids = await self.ensure_rooms_exist()
        await self.link_root_space(room_ids)
        await self.ensure_server_emoji_room()

        # Resolve room aliases now that any missing rooms have been created.
        config = self.config_provider()
        self.resolve_aliases(bots, config)

        # Join the router before policy and invitations, including existing private rooms.
        for bot in bots:
            if bot.agent_name == ROUTER_AGENT_NAME:
                await bot.ensure_rooms()
        snapshots = await self._reconcile_room_policies(room_ids)
        await self._ensure_room_invitations(snapshots)
        all_rooms = load_rooms(runtime_paths=self.runtime_paths)
        all_room_ids = {room_key: room.room_id for room_key, room in all_rooms.items()}
        if all_room_ids and config.mindroom_user is not None:
            await ensure_user_in_rooms(
                constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
                all_room_ids,
                self.runtime_paths,
                display_name=config.mindroom_user.display_name,
            )

        await asyncio.gather(*(bot.ensure_rooms() for bot in bots if bot.agent_name != ROUTER_AGENT_NAME))

        await self.refresh_agent_reply_memberships()

        logger.info("All agents have joined their configured rooms")

    async def ensure_rooms_exist(self) -> dict[str, str]:
        """Ensure all configured rooms exist, creating them if necessary.

        The router bot performs room creation because it holds the required permissions.
        """
        client = self.router_client()
        if client is None:
            return {}
        room_ids = await ensure_all_rooms_exist(client, self.config_provider(), self.runtime_paths)
        logger.info("ensured_room_existence", room_count=len(room_ids))
        return room_ids

    async def _reconcile_room_policies(self, room_ids: dict[str, str]) -> dict[str, RoomStateSnapshot]:
        """Wire the router's fresh policy reconciliation into startup and reload."""
        client = self.router_client()
        if client is None:
            return {}
        return await reconcile_managed_rooms(client, self.config_provider(), self.runtime_paths, room_ids)

    async def link_root_space(self, room_ids: dict[str, str]) -> None:
        """Ensure the optional root Matrix Space exists and link the current managed rooms."""
        client = self.router_client()
        config = self.config_provider()
        if client is None or not config.matrix_space.enabled:
            return
        root_space_id = await ensure_root_space(client, config, self.runtime_paths, room_ids, admin_user_ids=set())
        invite_user_ids = _root_space_user_ids_to_invite(config)
        if root_space_id is None or not invite_user_ids:
            return
        current_members = await get_room_members(client, root_space_id)
        if current_members is None:
            logger.warning("room_invitations_skipped_members_unavailable", room_id=root_space_id)
            return
        await _invite_missing(client, root_space_id, invite_user_ids, current_members)

    async def ensure_server_emoji_room(self) -> None:
        """Create and maintain the optional server custom emoji room through the router."""
        client = self.router_client()
        if client is not None:
            await ensure_server_emoji_room(client, self.config_provider(), self.runtime_paths)

    async def _ensure_room_invitations(self, snapshots: dict[str, RoomStateSnapshot]) -> None:
        """Ensure all agents and the internal user are invited to their configured rooms.

        The router client performs these invitations because it has admin privileges
        across the managed rooms.
        """
        client = self.router_client()
        if client is None:
            return
        config = self.config_provider()
        joined_rooms = await get_joined_rooms(client)
        if not joined_rooms:
            return

        server_name = extract_server_name_from_homeserver(
            constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths),
            runtime_paths=self.runtime_paths,
        )
        internal_user_id = (
            managed_account_user_id(INTERNAL_USER_ACCOUNT_KEY, server_name, self.runtime_paths)
            if config.mindroom_user is not None
            else None
        )

        for room_id in joined_rooms:
            configured_bots = configured_bot_user_ids_for_room(config, room_id, self.runtime_paths)
            managed = bool(configured_bots) or is_configured_room(config, room_id, self.runtime_paths)
            if not managed and internal_user_id is None:
                continue

            snapshot = snapshots.get(room_id)
            current_members = (
                snapshot.present_user_ids() if snapshot is not None else await get_room_members(client, room_id)
            )
            if current_members is None:
                logger.warning("room_invitations_skipped_members_unavailable", room_id=room_id)
                continue
            invite_user_ids = _room_user_ids_to_invite(config, room_id, self.runtime_paths) if managed else set()
            if internal_user_id is not None:
                invite_user_ids.add(internal_user_id)
            await _invite_missing(client, room_id, [*sorted(invite_user_ids), *configured_bots], current_members)

        logger.info("Ensured room invitations for all configured responders and authorized users")

    def resolve_aliases(self, bots: list[AgentBot], config: Config) -> None:
        """Resolve currently known room aliases into each bot's configured room IDs."""
        for bot in bots:
            room_aliases = get_rooms_for_entity(bot.agent_name, config)
            bot.rooms = resolve_room_aliases(room_aliases, runtime_paths=self.runtime_paths)


async def _invite_missing(
    client: nio.AsyncClient,
    room_id: str,
    user_ids: Iterable[str],
    current_members: set[str],
) -> None:
    """Invite each listed user who is not yet a member of a room."""
    for user_id in user_ids:
        if user_id in current_members:
            continue
        if await invite_to_room(client, room_id, user_id):
            current_members.add(user_id)
        else:
            logger.warning("room_invite_failed", room_id=room_id, user_id=user_id)
