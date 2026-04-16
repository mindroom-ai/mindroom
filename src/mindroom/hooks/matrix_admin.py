"""Hook-facing Matrix admin helper wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.matrix.client import add_room_to_space, create_room, get_room_members, invite_to_room
from mindroom.matrix.identity import extract_server_name_from_homeserver

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

    from .types import HookMatrixAdmin


@dataclass(frozen=True, slots=True)
class _BoundHookMatrixAdmin:
    """Minimal hook-facing Matrix admin surface bound to one client."""

    client: nio.AsyncClient
    runtime_paths: RuntimePaths

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
        """Create one room with the existing managed room helper."""
        return await create_room(
            client=self.client,
            name=name,
            alias=alias_localpart,
            topic=topic,
            power_users=power_user_ids,
        )

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


def build_hook_matrix_admin(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
) -> HookMatrixAdmin:
    """Return a minimal hook-facing Matrix admin helper bound to one client."""
    return _BoundHookMatrixAdmin(client=client, runtime_paths=runtime_paths)
