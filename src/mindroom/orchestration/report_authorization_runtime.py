"""Origin-room report authorization runtime binding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms, get_room_members
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.authorization import (
    OriginRoomAuthorizationKey,
    ReportAuthorizationDecision,
    ReportAuthorizationReason,
    SuccessfulReportAuthorizationCache,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.report_publishing.store import PublishedReport

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
        report: PublishedReport,
        viewer_matrix_user_id: str,
    ) -> ReportAuthorizationDecision:
        """Authorize one viewer against one report's exact origin room."""
        if report.access_policy is not ReportAccessPolicy.ORIGIN_ROOM:
            return ReportAuthorizationDecision(ReportAuthorizationReason.MALFORMED_REPORT)
        if (
            report.origin_room_id is None
            or report.publisher_entity_name is None
            or report.publisher_matrix_user_id is None
        ):
            return ReportAuthorizationDecision(ReportAuthorizationReason.MALFORMED_REPORT)

        publisher_entity_name = report.publisher_entity_name
        try:
            expected_publisher_id = (
                entity_identity_registry(
                    self.config,
                    self.runtime_paths,
                )
                .current_id(publisher_entity_name)
                .full_id
            )
        except (
            DuplicateManagedEntityIdentityError,
            KeyError,
            MissingManagedEntityAccountError,
        ):
            return ReportAuthorizationDecision(ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH)
        publisher_bot = self.bots.get(publisher_entity_name)
        if publisher_bot is None or publisher_bot.client is None or not publisher_bot.running:
            return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE)
        if (
            expected_publisher_id != report.publisher_matrix_user_id
            or publisher_bot.matrix_id.full_id != report.publisher_matrix_user_id
        ):
            return ReportAuthorizationDecision(ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH)

        key = OriginRoomAuthorizationKey(
            origin_room_id=report.origin_room_id,
            viewer_matrix_user_id=viewer_matrix_user_id,
            publisher_entity_name=publisher_entity_name,
            publisher_matrix_user_id=report.publisher_matrix_user_id,
        )
        return await self.cache.authorize(
            key,
            lambda: self._authorize_membership(
                publisher_bot,
                origin_room_id=report.origin_room_id or "",
                viewer_matrix_user_id=viewer_matrix_user_id,
                publisher_matrix_user_id=report.publisher_matrix_user_id or "",
            ),
        )

    @staticmethod
    async def _authorize_membership(
        publisher_bot: AgentBot | TeamBot,
        *,
        origin_room_id: str,
        viewer_matrix_user_id: str,
        publisher_matrix_user_id: str,
    ) -> ReportAuthorizationDecision:
        client = publisher_bot.client
        if client is None:
            return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE)
        reason = ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE
        try:
            joined_room_ids = await get_joined_rooms(client)
            if joined_room_ids is not None:
                if origin_room_id not in joined_room_ids:
                    reason = ReportAuthorizationReason.PUBLISHER_NOT_JOINED
                else:
                    joined_members = await get_room_members(client, origin_room_id)
                    if joined_members is not None:
                        if publisher_matrix_user_id not in joined_members:
                            reason = ReportAuthorizationReason.PUBLISHER_NOT_JOINED
                        elif viewer_matrix_user_id not in joined_members:
                            reason = ReportAuthorizationReason.VIEWER_NOT_JOINED
                        else:
                            reason = ReportAuthorizationReason.AUTHORIZED
        except Exception as exc:
            logger.warning(
                "report_membership_lookup_failed",
                error_type=type(exc).__name__,
            )
        return ReportAuthorizationDecision(reason)


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
