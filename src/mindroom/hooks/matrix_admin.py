"""Hook-facing Matrix admin helper wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.client_room_admin import add_room_to_space, create_room, get_room_members, invite_to_room
from mindroom.matrix.identity import managed_account_key, managed_account_user_id
from mindroom.matrix.invited_rooms_store import (
    invited_room_entity_names,
    invited_rooms_path,
    load_invited_rooms,
    save_invited_rooms,
    should_persist_invited_rooms,
)
from mindroom.matrix_identifiers import extract_server_name_from_homeserver

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

    from .types import HookMatrixAdmin


@dataclass(frozen=True, slots=True)
class _BoundHookMatrixAdmin:
    """Minimal hook-facing Matrix admin surface bound to one client."""

    client: nio.AsyncClient
    runtime_paths: RuntimePaths
    config: Config | None = None

    async def resolve_alias(self, alias: str) -> str | None:
        """Resolve one room alias and return the room ID when it exists."""
        response = await self.client.room_resolve_alias(alias)
        if isinstance(response, nio.RoomResolveAliasResponse):
            return str(response.room_id)
        return None

    async def create_room(
        self,
        *,
        name: str,
        alias_localpart: str | None = None,
        topic: str | None = None,
        power_user_ids: list[str] | None = None,
    ) -> str | None:
        """Create one room and record it so lifecycle cleanup preserves it for the creator."""
        room_id = await create_room(
            client=self.client,
            name=name,
            alias=alias_localpart,
            topic=topic,
            power_users=power_user_ids,
        )
        if room_id is not None:
            self._persist_created_room_for_creator(room_id)
        return room_id

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        """Invite one user into one room."""
        return await invite_to_room(self.client, room_id, user_id)

    async def get_room_members(self, room_id: str) -> set[str]:
        """Return the current joined members for one room."""
        return await get_room_members(self.client, room_id)

    async def add_room_to_space(self, space_room_id: str, room_id: str) -> bool:
        """Link one room under an existing Matrix Space."""
        server_name = extract_server_name_from_homeserver(
            self.client.homeserver,
            runtime_paths=self.runtime_paths,
        )
        return await add_room_to_space(self.client, space_room_id, room_id, server_name)

    async def put_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> bool:
        """Write one room state event using the bound admin-capable client."""
        response = await self.client.room_put_state(
            room_id=room_id,
            event_type=event_type,
            content=content,
            state_key=state_key,
        )
        return isinstance(response, nio.RoomPutStateResponse)

    def _persist_created_room_for_creator(self, room_id: str) -> None:
        """Record a room the bound managed entity created so cleanup preserves it.

        The creator is never invited into its own room, so the invite-accept
        lifecycle that records invited rooms never fires for it. Persist here so
        ``leave_unconfigured_rooms`` keeps plugin-created rooms across restarts.
        """
        if self.config is None:
            return
        entity_name = self._managed_entity_name_for_user_id(self.client.user_id)
        if entity_name is None or not should_persist_invited_rooms(self.config, entity_name):
            return
        path = invited_rooms_path(self.runtime_paths.storage_root, entity_name)
        room_ids = load_invited_rooms(path)
        if room_id in room_ids:
            return
        room_ids.add(room_id)
        save_invited_rooms(path, room_ids)

    def _managed_entity_name_for_user_id(self, user_id: str | None) -> str | None:
        """Return the configured bot entity name for one managed Matrix user ID."""
        if self.config is None or user_id is None:
            return None

        domain = self.config.get_domain(self.runtime_paths)
        for entity_name in invited_room_entity_names(self.config):
            entity_user_id = managed_account_user_id(
                managed_account_key(entity_name),
                domain,
                self.runtime_paths,
            )
            if entity_user_id == user_id:
                return entity_name
        return None


def build_hook_matrix_admin(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    *,
    config: Config | None = None,
) -> HookMatrixAdmin:
    """Return a minimal hook-facing Matrix admin helper bound to one client."""
    return _BoundHookMatrixAdmin(client=client, runtime_paths=runtime_paths, config=config)
