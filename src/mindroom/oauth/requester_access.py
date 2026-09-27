"""Access tokens for tools whose OAuth credentials always belong to the requesting user."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    oauth_credentials_usable,
    refresh_oauth_credentials_with_result,
)
from mindroom.oauth.providers import OAuthProviderError, OAuthRefreshRejectedError
from mindroom.oauth.service import (
    OAUTH_REFRESH_REJECTED_REASON,
    OAUTH_RESET_REQUIRED_REASON,
    oauth_connection_required,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class OAuthRefreshUnavailableError(Exception):
    """The provider could not refresh the requester's token right now; retrying later may work."""


@dataclass(frozen=True, slots=True)
class RequesterOAuthAccess:
    """Resolve and refresh one provider's token for the requester of the active tool call."""

    provider: OAuthProvider
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None
    config: Config | None

    def credential_context(self) -> OAuthCredentialContext:
        """Return the requester's credential scope for the active tool call."""
        return active_oauth_credential_context(
            self.provider,
            self.runtime_paths,
            self.credentials_manager,
            self.worker_target,
            config=self.config,
        )

    async def connection_required(self, *, reason: str | None = None) -> OAuthConnectionRequired:
        """Build the prompt that asks the requester to connect, reconnect, or reset."""
        # Building the link reads credential state synchronously, so keep it off the event loop.
        return await asyncio.to_thread(oauth_connection_required, self.credential_context(), reason=reason)

    async def access_token(self) -> str:
        """Return a current access token, refreshing it when it is about to expire.

        Raises ``OAuthConnectionRequired`` when the requester must connect or reconnect, and
        ``OAuthRefreshUnavailableError`` when a refresh failed for a reason retrying may fix.
        """
        context = self.credential_context()
        if context.worker_target is None:
            # Requester-only credentials never fall back to a shared or global store.
            raise await self.connection_required()
        try:
            refreshed = await refresh_oauth_credentials_with_result(context)
        except OAuthCredentialUnreadableError:
            raise await self.connection_required(reason=OAUTH_RESET_REQUIRED_REASON) from None
        except OAuthRefreshRejectedError:
            raise await self.connection_required(reason=OAUTH_REFRESH_REJECTED_REASON) from None
        except OAuthProviderError as exc:
            logger.warning("oauth_refresh_failed", provider_id=self.provider.id, error_type=type(exc).__name__)
            raise OAuthRefreshUnavailableError from None
        credentials = refreshed.credentials
        usable = await asyncio.to_thread(oauth_credentials_usable, self.provider, self.runtime_paths, credentials)
        token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
        if not usable or not isinstance(token, str) or not token:
            raise await self.connection_required()
        return token
