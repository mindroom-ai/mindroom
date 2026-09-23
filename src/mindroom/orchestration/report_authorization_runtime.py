"""Origin-room report authorization runtime binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.room_membership import cached_joined_member_ids, ensure_room_membership_synced
from mindroom.report_publishing.authorization import ReportAuthorizationReason, current_publisher_matrix_user_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.report_publishing.store import OriginRoomBinding


@dataclass(frozen=True)
class _OriginRoomReportAuthorizer:
    """Authorize reports against current entity identity and the publisher's synced room membership."""

    config: Config
    bots: Mapping[str, AgentBot | TeamBot]
    runtime_paths: RuntimePaths

    async def authorize(  # noqa: PLR0911 - each identity and membership outcome is a distinct decision
        self,
        origin_room: OriginRoomBinding,
        viewer_matrix_user_id: str,
    ) -> ReportAuthorizationReason:
        """Authorize one viewer against one report's exact origin room."""
        publisher_matrix_user_id = origin_room.publisher_matrix_user_id
        current_publisher_id = current_publisher_matrix_user_id(
            self.config,
            self.runtime_paths,
            origin_room.publisher_entity_name,
        )
        if current_publisher_id != publisher_matrix_user_id:
            return ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH
        publisher_bot = self.bots.get(origin_room.publisher_entity_name)
        if publisher_bot is None or publisher_bot.client is None or not publisher_bot.running:
            return ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE
        if publisher_bot.matrix_id.full_id != publisher_matrix_user_id:
            return ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH

        client = publisher_bot.client
        # nio drops left rooms from its joined-room projection.
        room = client.rooms.get(origin_room.room_id)
        if room is None:
            return ReportAuthorizationReason.PUBLISHER_NOT_JOINED
        if not await ensure_room_membership_synced(client, room, sender_id=viewer_matrix_user_id):
            return ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE
        joined_member_ids = cached_joined_member_ids(room)
        if publisher_matrix_user_id not in joined_member_ids:
            return ReportAuthorizationReason.PUBLISHER_NOT_JOINED
        if viewer_matrix_user_id not in joined_member_ids:
            return ReportAuthorizationReason.VIEWER_NOT_JOINED
        return ReportAuthorizationReason.AUTHORIZED


@dataclass
class ReportAuthorizationRuntimeCoordinator:
    """Own API binding for live origin-room report authorization."""

    runtime_paths: RuntimePaths
    api_enabled: bool = True

    def bind_if_ready(
        self,
        config: Config | None,
        bots: Mapping[str, AgentBot | TeamBot],
    ) -> None:
        """Bind report authorization after at least one live Matrix bot exists."""
        if not self.api_enabled or config is None:
            return
        if not any(bot.client is not None for bot in bots.values()):
            return
        authorizer = _OriginRoomReportAuthorizer(
            config=config,
            bots=bots,
            runtime_paths=self.runtime_paths,
        )
        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.bind_report_authorization_runtime(api_main.app, authorizer.authorize)

    def unbind(self) -> None:
        """Clear report authorization runtime from bundled API app."""
        if not self.api_enabled:
            return
        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.unbind_report_authorization_runtime(api_main.app)

    def unbind_for_entity_changes(self, entity_names: Iterable[str]) -> None:
        """Clear cached authority before entity lifecycle changes."""
        if set(entity_names):
            self.unbind()
