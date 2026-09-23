"""Recoverable personal rooms owned by an existing agent's Matrix runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import (
    HOOK_SOURCE_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    resolve_config_relative_path,
)
from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.matrix.avatar import set_room_avatar_from_file
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import (
    admin_join_room_user,
    create_room,
    ensure_room_admin_power_levels,
    get_room_members,
    invite_to_room,
)
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.personal_room_store import (
    PersonalRoomRecord,
    personal_room_digest,
    personal_room_record_path,
    read_personal_room,
    write_personal_room,
)
from mindroom.matrix.state import resolve_room_aliases
from mindroom.matrix_identifiers import managed_room_alias_localpart
from mindroom.requester_identity import is_human_requester_id, runtime_matrix_domain

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.config.personal_rooms import PersonalRoomsConfig
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_protocols import SupportsClientConfigMemberships

_OWNERSHIP_EVENT = "org.mindroom.personal_room"


class _PolicyChangedError(Exception):
    """Current policy no longer authorizes this provisioning attempt."""


@dataclass
class PersonalRoomService:
    """Keep only personal-room lifecycle state; execution remains in the normal bot."""

    agent_name: str
    runtime: SupportsClientConfigMemberships
    runtime_paths: RuntimePaths
    change_membership: Callable[[str, str], Awaitable[bool]]

    def _settings(self) -> PersonalRoomsConfig | None:
        settings = self.runtime.config.personal_rooms
        return settings if settings is not None and settings.agent == self.agent_name else None

    def _client(self) -> nio.AsyncClient:
        client = self.runtime.client
        if client is None:
            msg = "Personal-room agent is not connected"
            raise RuntimeError(msg)
        return client

    def _allowed(self, user_id: str, room_id: str) -> bool:
        config = self.runtime.config
        return is_human_requester_id(user_id, config, self.runtime_paths) and is_sender_allowed_for_agent_reply_in_room(
            user_id,
            self.agent_name,
            config,
            room_id,
            self.runtime_paths,
            self.runtime.agent_reply_memberships,
            require_resolved_membership=True,
        )

    def _current_settings(self, user_id: str, source_room_id: str) -> PersonalRoomsConfig | None:
        """Read current onboarding and access policy at an authority boundary."""
        settings = self._settings()
        if (
            settings is None
            or source_room_id not in resolve_room_aliases(settings.onboarding_rooms, self.runtime_paths)
            or not self._allowed(user_id, source_room_id)
        ):
            return None
        return settings

    def _require_current_settings(self, user_id: str, source_room_id: str) -> PersonalRoomsConfig:
        settings = self._current_settings(user_id, source_room_id)
        if settings is None:
            raise _PolicyChangedError
        return settings

    def _requester_admin_allowed(self, record: PersonalRoomRecord, source_room_id: str) -> bool:
        settings = self._current_settings(record.user_id, source_room_id)
        return settings is not None and settings.requester_admin

    def _avatar_write_allowed(
        self,
        record: PersonalRoomRecord,
        source_room_id: str,
        choice: tuple[str | None, bool],
    ) -> bool:
        settings = self._current_settings(record.user_id, source_room_id)
        return settings is not None and (settings.avatar, settings.avatar_from_requester) == choice

    def _welcome_write_allowed(self, record: PersonalRoomRecord, source_room_id: str, *, human_joined: bool) -> bool:
        if self._current_settings(record.user_id, source_room_id) is None:
            return False
        assert record.room_id is not None
        assert record.welcome_content is not None
        dispatch = record.welcome_content.get(SOURCE_KIND_KEY) == HOOK_DISPATCH_SOURCE_KIND
        return not dispatch or (human_joined and self._allowed(record.user_id, record.room_id))

    async def _eligible_settings(
        self,
        user_id: str,
        source_room_id: str,
        source_client: nio.AsyncClient,
    ) -> PersonalRoomsConfig | None:
        if self._current_settings(user_id, source_room_id) is None:
            return None
        members = await get_room_members(source_client, source_room_id)
        if members is None:
            msg = "Personal-room onboarding membership unavailable"
            raise RuntimeError(msg)
        return self._current_settings(user_id, source_room_id) if user_id in members else None

    async def ensure(
        self,
        user_id: str,
        source_room_id: str,
        source_client: nio.AsyncClient,
        *,
        reinvite_departed_owner: bool = False,
    ) -> str | None:
        """Reconcile a currently eligible human from a router-observed room."""
        if await self._eligible_settings(user_id, source_room_id, source_client) is None:
            return None
        path = personal_room_record_path(self.runtime_paths, self.agent_name, user_id)
        try:
            async with async_exclusive_file_lock(path.with_suffix(".lock")):
                settings = await self._eligible_settings(user_id, source_room_id, source_client)
                if settings is None:
                    return None
                record = await run_blocking_until_complete(read_personal_room, path)
                if record is None:
                    localpart = managed_room_alias_localpart(
                        f"{settings.alias_prefix}_{personal_room_digest(user_id)[:20]}",
                        self.runtime_paths,
                    )
                    record = PersonalRoomRecord(
                        user_id=user_id,
                        alias=f"#{localpart}:{runtime_matrix_domain(self.runtime_paths)}",
                        source_room_id=source_room_id,
                    )
                    await run_blocking_until_complete(write_personal_room, path, record)
                if (record.resume_source_room_id or record.source_room_id) != source_room_id:
                    self._require_current_settings(user_id, source_room_id)
                    record.resume_source_room_id = source_room_id
                    await run_blocking_until_complete(write_personal_room, path, record)
                if record.room_id is None:
                    record.room_id = await self._resolve_or_create(record, source_room_id)
                    await run_blocking_until_complete(write_personal_room, path, record)
                human_joined = await self._reconcile_membership(
                    record,
                    path,
                    source_client,
                    source_room_id,
                    reinvite_departed_owner,
                )
                if human_joined is None:
                    return record.room_id
                await self._finish(record, path, source_room_id, human_joined=human_joined)
                settings = self._require_current_settings(user_id, source_room_id)
                if source_room_id == record.source_room_id:
                    await self._confirm(record, path, settings, source_client)
                return record.room_id
        except _PolicyChangedError:
            return None

    async def _reconcile_membership(
        self,
        record: PersonalRoomRecord,
        path: Path,
        source_client: nio.AsyncClient,
        source_room_id: str,
        reinvite_departed_owner: bool,
    ) -> bool | None:
        """Refresh remote authority at each requester mutation; None preserves a departure."""
        roster = await self._validate_room(record)
        initial_owner_membership = roster.get(record.user_id)
        allow_reinvite = initial_owner_membership == "leave" and reinvite_departed_owner and record.adoption is None
        if await self._observe_owner_membership(record, path, initial_owner_membership, allow_reinvite):
            return None
        self._require_current_settings(record.user_id, source_room_id)
        assert record.room_id is not None
        if not await self.change_membership(record.room_id, "join"):
            msg = "Personal-room agent membership failed"
            raise RuntimeError(msg)
        roster = await self._validate_room(record)
        owner_membership = roster.get(record.user_id)
        if await self._observe_owner_membership(record, path, owner_membership, allow_reinvite):
            return None
        await self._invite_owner_if_needed(record, owner_membership, source_room_id)
        roster = await self._validate_room(record)
        owner_membership = roster.get(record.user_id)
        if await self._observe_owner_membership(record, path, owner_membership, False):
            return None
        return await self._finish_initial_join(
            record,
            path,
            source_client,
            source_room_id,
            initial_owner_membership,
            owner_membership,
        )

    async def _invite_owner_if_needed(
        self,
        record: PersonalRoomRecord,
        owner_membership: str | None,
        source_room_id: str,
    ) -> None:
        """Invite only an absent owner or one with explicit re-invite authority."""
        if owner_membership in {"join", "invite"}:
            return
        self._require_current_settings(record.user_id, source_room_id)
        assert record.room_id is not None
        if not await invite_to_room(self._client(), record.room_id, record.user_id):
            msg = "Personal-room invite failed"
            raise RuntimeError(msg)

    async def _observe_owner_membership(
        self,
        record: PersonalRoomRecord,
        path: Path,
        owner_membership: str | None,
        reinvite_departed_owner: bool,
    ) -> bool:
        """Retire initial join intent on observed completion or departure before further work."""
        if record.initial_join_pending and owner_membership in {"join", "leave", "ban"}:
            record.initial_join_pending = False
            await run_blocking_until_complete(write_personal_room, path, record)
        if owner_membership not in {"leave", "ban"}:
            return False
        return owner_membership == "ban" or record.adoption is not None or not reinvite_departed_owner

    async def _finish_initial_join(
        self,
        record: PersonalRoomRecord,
        path: Path,
        source_client: nio.AsyncClient,
        source_room_id: str,
        initial_owner_membership: str | None,
        current_owner_membership: str | None,
    ) -> bool:
        """Retry only an initial create's join while the original roster permits it."""
        if not record.initial_join_pending:
            return current_owner_membership == "join"
        settings = self._require_current_settings(record.user_id, source_room_id)
        if (
            initial_owner_membership in {None, "invite"}
            and current_owner_membership == "invite"
            and settings.auto_join_requester
        ):
            assert record.room_id is not None
            if not await admin_join_room_user(source_client, record.room_id, record.user_id):
                msg = "Personal-room initial join failed"
                raise RuntimeError(msg)
            human_joined = True
        else:
            human_joined = current_owner_membership == "join"
        record.initial_join_pending = False
        await run_blocking_until_complete(write_personal_room, path, record)
        return human_joined

    def _ownership(self, user_id: str) -> dict[str, str]:
        return {"user_id": user_id, "agent_user_id": self._client().user_id}

    async def _resolve_or_create(self, record: PersonalRoomRecord, source_room_id: str) -> str:
        client = self._client()
        response = await client.room_resolve_alias(record.alias)
        if isinstance(response, nio.RoomResolveAliasResponse):
            record.room_id = response.room_id
            await self._validate_room(record)
            return response.room_id
        if not isinstance(response, nio.RoomResolveAliasError) or response.status_code != "M_NOT_FOUND":
            msg = "Personal-room alias lookup failed"
            raise RuntimeError(msg)
        settings = self._require_current_settings(record.user_id, source_room_id)
        values = self._template_values(record)
        room_id = await create_room(
            client,
            name=settings.name.format(**values),
            alias=record.alias[1:].split(":", 1)[0],
            topic=settings.topic.format(**values),
            initial_state=[
                {"type": _OWNERSHIP_EVENT, "state_key": "", "content": self._ownership(record.user_id)},
                {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
                {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "invited"}},
            ],
        )
        if room_id is not None:
            record.initial_join_pending = settings.auto_join_requester
            return room_id
        # Concurrent creators and ambiguous create responses converge on the alias.
        response = await client.room_resolve_alias(record.alias)
        if isinstance(response, nio.RoomResolveAliasResponse):
            record.room_id = response.room_id
            await self._validate_room(record)
            return response.room_id
        msg = "Personal-room creation failed"
        raise RuntimeError(msg)

    async def _validate_room(self, record: PersonalRoomRecord) -> dict[str, str]:
        assert record.room_id is not None
        response = await self._client().room_get_state(record.room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            msg = "Personal-room ownership state unavailable"
            raise RuntimeError(msg)  # noqa: TRY004 - a Matrix transport failure is retryable, not a caller type error
        visibility = await self._client().room_get_visibility(record.room_id)
        if not isinstance(visibility, nio.RoomGetVisibilityResponse) or visibility.visibility != "private":
            msg = "Personal-room directory must remain private"
            raise RuntimeError(msg)
        state = {(event["type"], event.get("state_key", "")): event for event in response.events}
        creator = state.get(("m.room.create", ""), {}).get("sender")
        marker = state.get((_OWNERSHIP_EVENT, ""), {})
        roster = {
            key: event.get("content", {}).get("membership", "")
            for (kind, key), event in state.items()
            if kind == "m.room.member"
        }
        joined_or_invited = {
            user_id for user_id, membership in roster.items() if membership in {"join", "invite", "knock"}
        }
        agent_id = self._client().user_id
        expected_creator = agent_id
        expected_history = "invited"
        permitted_members = {record.user_id, agent_id}
        if record.adoption is not None:
            expected_creator = record.adoption.creator_user_id
            expected_history = record.adoption.expected_history_visibility
            permitted_members.update(record.adoption.additional_user_ids)
            router_id = record.adoption.router_user_id
            if record.adoption.agent_user_id != agent_id or (
                router_id is not None
                and router_id
                != entity_identity_registry(self.runtime.config, self.runtime_paths)
                .current_id(ROUTER_AGENT_NAME)
                .full_id
            ):
                msg = "Personal-room adopted ownership identity does not match"
                raise RuntimeError(msg)
            if router_id is not None:
                permitted_members.add(router_id)
        power = state.get(("m.room.power_levels", ""), {}).get("content", {})
        agent_power = power.get("users", {}).get(agent_id, power.get("users_default", 0))
        if (
            creator != expected_creator
            or marker.get("sender") != self._client().user_id
            or marker.get("content") != self._ownership(record.user_id)
            or roster.get(self._client().user_id) != "join"
            or joined_or_invited - permitted_members
            or agent_power < max(100, power.get("state_default", 50), power.get("invite", 0))
            or state.get(("m.room.join_rules", ""), {}).get("content", {}).get("join_rule") != "invite"
            or state.get(("m.room.history_visibility", ""), {}).get("content", {}).get("history_visibility")
            != expected_history
        ):
            msg = "Personal-room ownership or membership does not match"
            raise RuntimeError(msg)
        return roster

    def _template_values(self, record: PersonalRoomRecord) -> dict[str, str]:
        return {
            "user": record.user_id,
            "room": record.alias,
            "agent": self.runtime.config.agents[self.agent_name].display_name,
        }

    async def _finish(  # noqa: C901, PLR0911, PLR0912 - preserve ordered authorization and receipt boundaries
        self,
        record: PersonalRoomRecord,
        path: Path,
        source_room_id: str,
        *,
        human_joined: bool,
    ) -> None:
        assert record.room_id is not None
        settings = self._current_settings(record.user_id, source_room_id)
        if settings is None:
            return
        client = self._client()
        if settings.requester_admin:
            granted = await ensure_room_admin_power_levels(
                client,
                record.room_id,
                [record.user_id],
                write_allowed=lambda: self._requester_admin_allowed(record, source_room_id),
            )
            if not granted and self._requester_admin_allowed(record, source_room_id):
                msg = "Personal-room admin grant failed"
                raise RuntimeError(msg)
        settings = self._current_settings(record.user_id, source_room_id)
        if settings is None:
            return
        if (settings.avatar is not None or settings.avatar_from_requester) and not record.avatar_done:
            choice = (settings.avatar, settings.avatar_from_requester)
            await self._set_avatar(record, source_room_id)
            if not self._avatar_write_allowed(record, source_room_id, choice):
                return
            record.avatar_done = True
            await run_blocking_until_complete(write_personal_room, path, record)
        if record.welcome_completed or record.welcome_event_id is not None:
            return
        if record.welcome_content is None:
            if not settings.welcome:
                return
            content = build_message_content(
                settings.welcome.format(**self._template_values(record)),
                mentioned_user_ids=[client.user_id] if settings.welcome_dispatch else None,
            )
            if settings.welcome_dispatch:
                content.update(
                    {
                        SOURCE_KIND_KEY: HOOK_DISPATCH_SOURCE_KIND,
                        ORIGINAL_SENDER_KEY: record.user_id,
                        HOOK_SOURCE_KEY: "native:personal_room",
                    },
                )
            else:
                content["msgtype"] = "m.notice"
            record.welcome_content = content
            record.welcome_device_id = client.device_id
            await run_blocking_until_complete(write_personal_room, path, record)
        if not self._welcome_write_allowed(record, source_room_id, human_joined=human_joined):
            return
        if record.welcome_device_id != client.device_id or not client.device_id:
            msg = "Personal-room pending welcome belongs to a different Matrix device"
            raise RuntimeError(msg)
        delivered = await send_message_result(
            client,
            record.room_id,
            record.welcome_content,
            transaction_id=f"personal-welcome-{personal_room_digest(record.room_id)}",
            write_allowed=lambda: self._welcome_write_allowed(record, source_room_id, human_joined=human_joined),
        )
        if delivered is None:
            if not self._welcome_write_allowed(record, source_room_id, human_joined=human_joined):
                return
            msg = "Personal-room welcome delivery failed"
            raise RuntimeError(msg)
        record.welcome_event_id = delivered.event_id
        record.welcome_completed = True
        await run_blocking_until_complete(write_personal_room, path, record)

    async def _set_avatar(  # noqa: C901 - recheck policy between Matrix reads and writes
        self,
        record: PersonalRoomRecord,
        source_room_id: str,
    ) -> None:
        assert record.room_id is not None
        client = self._client()
        current = await client.room_get_state_event(record.room_id, "m.room.avatar")
        if isinstance(current, nio.RoomGetStateEventResponse):
            if current.content.get("url"):
                return
        elif not isinstance(current, nio.RoomGetStateEventError) or current.status_code != "M_NOT_FOUND":
            msg = "Personal-room avatar state unavailable"
            raise RuntimeError(msg)
        settings = self._current_settings(record.user_id, source_room_id)
        if settings is None:
            return
        if settings.avatar is None and not settings.avatar_from_requester:
            return
        choice = (settings.avatar, settings.avatar_from_requester)
        if settings.avatar is not None:
            success = await set_room_avatar_from_file(
                client,
                record.room_id,
                resolve_config_relative_path(settings.avatar, self.runtime_paths),
                write_allowed=lambda: self._avatar_write_allowed(record, source_room_id, choice),
            )
        else:
            profile = await client.get_profile(record.user_id)
            if not isinstance(profile, nio.ProfileGetResponse):
                msg = "Personal-room requester profile unavailable"
                raise RuntimeError(msg)
            if not profile.avatar_url:
                return
            if not self._avatar_write_allowed(record, source_room_id, choice):
                return
            response = await client.room_put_state(record.room_id, "m.room.avatar", {"url": profile.avatar_url})
            success = isinstance(response, nio.RoomPutStateResponse)
        if not success:
            if not self._avatar_write_allowed(record, source_room_id, choice):
                return
            msg = "Personal-room avatar update failed"
            raise RuntimeError(msg)

    async def _confirm(
        self,
        record: PersonalRoomRecord,
        path: Path,
        settings: PersonalRoomsConfig,
        source_client: nio.AsyncClient,
    ) -> None:
        if not settings.confirmation or record.confirmation_event_id is not None:
            return
        if record.confirmation_content is None:
            record.confirmation_content = build_message_content(
                settings.confirmation.format(**self._template_values(record)),
            )
            record.confirmation_content["msgtype"] = "m.notice"
            record.confirmation_device_id = source_client.device_id
            await run_blocking_until_complete(write_personal_room, path, record)
        if self._current_settings(record.user_id, record.source_room_id) is None:
            return
        if not source_client.device_id or record.confirmation_device_id != source_client.device_id:
            msg = "Personal-room pending confirmation belongs to a different Matrix device"
            raise RuntimeError(msg)
        delivered = await send_message_result(
            source_client,
            record.source_room_id,
            record.confirmation_content,
            transaction_id=f"personal-confirmation-{personal_room_digest(record.room_id or record.alias)}",
            write_allowed=lambda: self._current_settings(record.user_id, record.source_room_id) is not None,
        )
        if delivered is None:
            if self._current_settings(record.user_id, record.source_room_id) is None:
                return
            msg = "Personal-room confirmation delivery failed"
            raise RuntimeError(msg)
        record.confirmation_event_id = delivered.event_id
        await run_blocking_until_complete(write_personal_room, path, record)

    async def owner_membership_event(self, room_id: str, user_id: str, membership: str) -> None:
        """Retire observed owner enrollment before current-policy-dependent welcome work."""
        if membership not in {"join", "leave", "ban"}:
            return
        path = personal_room_record_path(self.runtime_paths, self.agent_name, user_id)
        if not path.is_file():
            return
        async with async_exclusive_file_lock(path.with_suffix(".lock")):
            record = await run_blocking_until_complete(read_personal_room, path)
            if record is None or record.room_id != room_id:
                return
            await self._observe_owner_membership(record, path, membership, False)
            if membership != "join":
                return
            settings = self._settings()
            if settings is None or not self._allowed(user_id, room_id):
                return
            roster = await self._validate_room(record)
            await self._finish(
                record,
                path,
                record.resume_source_room_id or record.source_room_id,
                human_joined=roster.get(user_id) == "join",
            )
