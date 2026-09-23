"""Origin-room report authorization runtime binding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_members
from mindroom.report_publishing.authorization import (
    OriginRoomAuthorizationKey,
    ReportAuthorizationDecision,
    ReportAuthorizationReason,
    SuccessfulReportAuthorizationCache,
    current_publisher_matrix_user_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import nio

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.report_publishing.store import OriginRoomBinding

logger = get_logger(__name__)


@dataclass
class _OriginRoomReportAuthorizer:
    """Authorize reports against current entity identity and joined membership."""

    config: Config
    bots: Mapping[str, AgentBot | TeamBot]
    runtime_paths: RuntimePaths
    cache: SuccessfulReportAuthorizationCache = field(default_factory=SuccessfulReportAuthorizationCache)

    async def authorize(
        self,
        origin_room: OriginRoomBinding,
        viewer_matrix_user_id: str,
    ) -> ReportAuthorizationDecision:
        """Authorize one viewer against one report's exact origin room."""
        publisher_matrix_user_id = origin_room.publisher_matrix_user_id
        current_publisher_id = current_publisher_matrix_user_id(
            self.config,
            self.runtime_paths,
            origin_room.publisher_entity_name,
        )
        if current_publisher_id != publisher_matrix_user_id:
            return ReportAuthorizationDecision(ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH)
        publisher_bot = self.bots.get(origin_room.publisher_entity_name)
        if publisher_bot is None or publisher_bot.client is None or not publisher_bot.running:
            return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE)
        if publisher_bot.matrix_id.full_id != publisher_matrix_user_id:
            return ReportAuthorizationDecision(ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH)

        client = publisher_bot.client
        return await self.cache.authorize(
            OriginRoomAuthorizationKey(origin_room=origin_room, viewer_matrix_user_id=viewer_matrix_user_id),
            lambda: _authorize_membership(client, origin_room, viewer_matrix_user_id),
        )


async def _authorize_membership(  # noqa: PLR0911 - each membership outcome is a distinct authorization decision
    client: nio.AsyncClient,
    origin_room: OriginRoomBinding,
    viewer_matrix_user_id: str,
) -> ReportAuthorizationDecision:
    unavailable = ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE)
    try:
        joined_room_ids = await get_joined_rooms(client)
        if joined_room_ids is None:
            return unavailable
        if origin_room.room_id not in joined_room_ids:
            return ReportAuthorizationDecision(ReportAuthorizationReason.PUBLISHER_NOT_JOINED)
        joined_members = await get_room_members(client, origin_room.room_id)
    except Exception as exc:
        # Matrix transport failures fail closed; log only the type so request URLs and room IDs stay out of logs.
        logger.warning("report_membership_lookup_failed", error_type=type(exc).__name__)
        return unavailable
    if joined_members is None:
        return unavailable
    if origin_room.publisher_matrix_user_id not in joined_members:
        return ReportAuthorizationDecision(ReportAuthorizationReason.PUBLISHER_NOT_JOINED)
    if viewer_matrix_user_id not in joined_members:
        return ReportAuthorizationDecision(ReportAuthorizationReason.VIEWER_NOT_JOINED)
    return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZED)


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
