"""Coordinate personal-room triggers, recovery, and retention across live agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.personal_room_store import (
    personal_room_cleanup_exclusions,
    personal_room_record_path,
    read_personal_room,
    retained_personal_rooms,
)
from mindroom.matrix.state import resolve_room_aliases
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.personal_rooms import PersonalRoomService
    from mindroom.matrix.room_member_joins import RoomMemberJoin
    from mindroom.runtime_protocols import SupportsClientConfig


logger = get_logger(__name__)


@dataclass(frozen=True)
class PersonalRoomTarget:
    """A connected owner's service and its separate startup readiness projection."""

    service: PersonalRoomService
    first_sync_complete: bool


@dataclass
class PersonalRoomLifecycle:
    """Own feature policy while consuming narrow runtime and live-service projections."""

    agent_name: str
    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    service: PersonalRoomService
    lookup_target: Callable[[str], PersonalRoomTarget | None]
    requester_user_id: Callable[[nio.RoomMessageFormatted], str]
    _reconciled: bool = field(default=False, init=False)
    _config_revision: int = field(default=0, init=False)

    @property
    def observes_onboarding_joins(self) -> bool:
        """Whether router baseline joins need durable admission for this feature."""
        return self.agent_name == ROUTER_AGENT_NAME and self.runtime.config.personal_rooms is not None

    def config_changed(self) -> None:
        """Revisit existing records and optional backfill after a configuration reload."""
        self._config_revision += 1
        self._reconciled = False

    async def _onboard(
        self,
        user_id: str,
        source_room_id: str,
        *,
        reinvite_departed_owner: bool = False,
    ) -> None:
        """Forward a trusted router observation; unavailable owners leave it retryable."""
        settings = self.runtime.config.personal_rooms
        if (
            not self.observes_onboarding_joins
            or settings is None
            or source_room_id not in resolve_room_aliases(settings.onboarding_rooms, self.runtime_paths)
            or not is_human_requester_id(user_id, self.runtime.config, self.runtime_paths)
        ):
            return
        target = self.lookup_target(settings.agent)
        if target is None or self.runtime.client is None:
            msg = "Personal-room target is not ready"
            raise RuntimeError(msg)
        await target.service.ensure(
            user_id,
            source_room_id,
            self.runtime.client,
            reinvite_departed_owner=reinvite_departed_owner,
        )

    async def handle_command(self, room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> bool:
        """Recognize exact self-onboarding commands through trusted requester resolution."""
        settings = self.runtime.config.personal_rooms
        if (
            not self.observes_onboarding_joins
            or settings is None
            or event.body.strip() not in settings.commands
            or room.room_id not in resolve_room_aliases(settings.onboarding_rooms, self.runtime_paths)
        ):
            return False
        requester = self.requester_user_id(event)
        if is_human_requester_id(
            event.sender,
            self.runtime.config,
            self.runtime_paths,
        ) and requester == resolve_human_requester_alias(
            event.sender,
            self.runtime.config,
            self.runtime_paths,
        ):
            await self._onboard(event.sender, room.room_id)
        return True

    async def member_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Finish local welcomes and route definite joins without bypassing baseline admission."""
        if event.membership in {"join", "leave", "ban"}:
            await self.service.owner_membership_event(room.room_id, event.state_key, event.membership)
        if self.runtime.config.personal_rooms is None or event.membership != "join" or event.prev_membership == "join":
            return
        if self.observes_onboarding_joins and event.prev_membership is not None:
            await self._onboard(
                event.state_key,
                room.room_id,
                reinvite_departed_owner=event.prev_membership == "leave",
            )

    async def baseline_join(self, join: RoomMemberJoin) -> None:
        """Onboard unknown prior membership only after the existing durable baseline gate."""
        if join.prev_membership is None:
            await self._onboard(join.user_id, join.room_id)

    def _recorded_candidates(self, agent_name: str) -> tuple[set[tuple[str, str]], bool]:
        """Read each retained intent independently; keep damaged files retryable."""
        candidates: set[tuple[str, str]] = set()
        failed = False
        directory = personal_room_record_path(self.runtime_paths, agent_name, "").parent
        try:
            paths = list(directory.glob("*.json"))
        except OSError:
            logger.exception("Personal-room records unavailable", agent=agent_name)
            return candidates, True
        for path in paths:
            try:
                record = read_personal_room(path)
            except Exception:
                logger.exception("Personal-room record invalid", record=path.name)
                failed = True
                continue
            if record is not None:
                candidates.add((record.user_id, record.source_room_id))
        return candidates, failed

    async def reconcile(self) -> None:
        """Retry recorded intent and optional lobby backfill after the owner has synced."""
        revision = self._config_revision
        settings = self.runtime.config.personal_rooms
        if self._reconciled or not self.observes_onboarding_joins or settings is None:
            return
        target = self.lookup_target(settings.agent)
        if target is None or not target.first_sync_complete or self.runtime.client is None:
            return
        candidates, failed = self._recorded_candidates(settings.agent)
        if settings.backfill:
            for room_id in resolve_room_aliases(settings.onboarding_rooms, self.runtime_paths):
                try:
                    members = await get_room_members(self.runtime.client, room_id)
                except Exception:
                    logger.exception("Personal-room backfill failed", room_id=room_id)
                    failed = True
                    continue
                if members is None:
                    logger.error("Personal-room backfill membership unavailable", room_id=room_id)
                    failed = True
                    continue
                candidates.update((user_id, room_id) for user_id in members)
        for user_id, room_id in sorted(candidates):
            try:
                await self._onboard(user_id, room_id)
            except Exception:
                logger.exception("Personal-room reconciliation failed", user_id=user_id, room_id=room_id)
                failed = True
        if not failed and revision == self._config_revision:
            self._reconciled = True

    def retained_room_ids(self) -> set[str]:
        """Return recorded rejoin authority, including when provisioning is disabled."""
        client = self.runtime.client
        return retained_personal_rooms(
            self.runtime_paths,
            self.agent_name,
            user_id=client.user_id if client is not None else None,
        )

    async def cleanup_exclusions(self) -> set[str]:
        """Protect interrupted creates conservatively without granting rejoin authority."""
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for personal-room retention"
            raise RuntimeError(msg)
        return await personal_room_cleanup_exclusions(client, self.runtime_paths, self.agent_name)
