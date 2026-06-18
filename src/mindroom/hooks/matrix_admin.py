"""Hook-facing Matrix admin helper wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

from mindroom.logging_config import get_logger
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


logger = get_logger(__name__)


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
        """Create one room with the existing managed room helper."""
        return await create_room(
            client=self.client,
            name=name,
            alias=alias_localpart,
            topic=topic,
            power_users=power_user_ids,
        )

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        """Invite one user into one room; use ensure_room_members for bulk reconciliation."""
        current_members: set[str] | None = None
        if self.config is not None:
            current_members = await self._get_room_members_or_none(room_id)
            if current_members is not None and user_id in current_members:
                self._persist_invited_room_for_managed_entity(room_id, user_id)
                return True

        invited = await invite_to_room(self.client, room_id, user_id)
        if invited:
            self._persist_invited_room_for_managed_entity(room_id, user_id)
            return True

        if self.config is not None and current_members is None:
            refreshed_members = await self._get_room_members_or_none(room_id)
            if refreshed_members is not None and user_id in refreshed_members:
                self._persist_invited_room_for_managed_entity(room_id, user_id)
                return True
        return False

    async def ensure_room_members(self, room_id: str, user_ids: list[str]) -> set[str]:
        """Invite missing users into one room and return users newly invited."""
        current_members = await self._get_room_members_or_none(room_id)
        known_members = current_members or set()
        invited_user_ids: set[str] = set()
        entity_names_by_user_id = self._managed_entity_names_by_user_id()
        entities_to_persist: set[str] = set()
        unverified_user_ids: set[str] = set()

        for user_id in sorted(set(user_ids)):
            if user_id in known_members:
                self._record_managed_entity_for_user_id(user_id, entity_names_by_user_id, entities_to_persist)
                continue

            if await invite_to_room(self.client, room_id, user_id):
                known_members.add(user_id)
                invited_user_ids.add(user_id)
                self._record_managed_entity_for_user_id(user_id, entity_names_by_user_id, entities_to_persist)
            elif current_members is None:
                unverified_user_ids.add(user_id)

        await self._record_verified_members_after_failed_invites(
            room_id,
            unverified_user_ids,
            entity_names_by_user_id,
            entities_to_persist,
        )

        self._persist_invited_room_for_entities(room_id, entities_to_persist)
        return invited_user_ids

    async def get_room_members(self, room_id: str) -> set[str]:
        """Return the current joined members for one room."""
        return await get_room_members(self.client, room_id)

    async def _get_room_members_or_none(self, room_id: str) -> set[str] | None:
        """Return room members, or None when Matrix did not return authoritative membership."""
        response = await self.client.joined_members(room_id)
        if isinstance(response, nio.JoinedMembersResponse):
            return {member.user_id for member in response.members}
        logger.warning("matrix_room_members_fetch_failed", room_id=room_id)
        return None

    async def _record_verified_members_after_failed_invites(
        self,
        room_id: str,
        unverified_user_ids: set[str],
        entity_names_by_user_id: dict[str, str],
        entities_to_persist: set[str],
    ) -> None:
        """Persist managed users found by a retry after initial membership was unavailable."""
        if not unverified_user_ids:
            return
        refreshed_members = await self._get_room_members_or_none(room_id)
        if refreshed_members is None:
            return
        for user_id in sorted(unverified_user_ids):
            if user_id in refreshed_members:
                self._record_managed_entity_for_user_id(user_id, entity_names_by_user_id, entities_to_persist)

    @staticmethod
    def _record_managed_entity_for_user_id(
        user_id: str,
        entity_names_by_user_id: dict[str, str],
        entities_to_persist: set[str],
    ) -> None:
        """Add the configured entity for one managed user ID, when known."""
        entity_name = entity_names_by_user_id.get(user_id)
        if entity_name is not None:
            entities_to_persist.add(entity_name)

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

    def _persist_invited_room_for_managed_entity(self, room_id: str, user_id: str) -> None:
        """Record plugin-managed rooms for bot lifecycle cleanup preservation."""
        if self.config is None:
            return

        entity_name = self._managed_entity_name_for_user_id(user_id)
        if entity_name is None:
            return
        self._persist_invited_room_for_entity(room_id, entity_name)

    def _persist_invited_room_for_entities(self, room_id: str, entity_names: set[str]) -> None:
        """Record one plugin-managed room for configured bot entities."""
        for entity_name in sorted(entity_names):
            self._persist_invited_room_for_entity(room_id, entity_name)

    def _persist_invited_room_for_entity(self, room_id: str, entity_name: str) -> None:
        """Record one plugin-managed room for one configured bot entity."""
        if self.config is None or not should_persist_invited_rooms(self.config, entity_name):
            return
        path = invited_rooms_path(self.runtime_paths.storage_root, entity_name)
        room_ids = load_invited_rooms(path)
        if room_id in room_ids:
            return
        room_ids.add(room_id)
        save_invited_rooms(path, room_ids)

    def _managed_entity_name_for_user_id(self, user_id: str) -> str | None:
        """Return configured bot entity name for one persisted Matrix user ID."""
        if self.config is None:
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

    def _managed_entity_names_by_user_id(self) -> dict[str, str]:
        """Return persisted Matrix user IDs keyed to configured bot entity names."""
        if self.config is None:
            return {}

        domain = self.config.get_domain(self.runtime_paths)
        entity_names_by_user_id: dict[str, str] = {}
        for entity_name in invited_room_entity_names(self.config):
            user_id = managed_account_user_id(
                managed_account_key(entity_name),
                domain,
                self.runtime_paths,
            )
            if user_id is not None:
                entity_names_by_user_id[user_id] = entity_name
        return entity_names_by_user_id


def build_hook_matrix_admin(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    *,
    config: Config | None = None,
) -> HookMatrixAdmin:
    """Return a minimal hook-facing Matrix admin helper bound to one client."""
    return _BoundHookMatrixAdmin(client=client, runtime_paths=runtime_paths, config=config)
