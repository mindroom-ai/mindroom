"""Spotify toolkit that sends the connected account's current OAuth access token."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.spotify import SpotifyTools as AgnoSpotifyTools

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    oauth_credentials_usable,
    refresh_oauth_credentials_blocking,
)
from mindroom.oauth.providers import OAuthProviderError, OAuthRefreshRejectedError
from mindroom.oauth.service import (
    OAUTH_REFRESH_REJECTED_REASON,
    OAUTH_RESET_REQUIRED_REASON,
    oauth_connection_required,
)
from mindroom.oauth.spotify import spotify_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class SpotifyTools(AgnoSpotifyTools):
    """Agno Spotify toolkit that reads its access token from the Spotify OAuth connection on every request."""

    def __init__(
        self,
        default_market: str | None = "US",
        timeout: int = 30,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager,
        worker_target: ResolvedWorkerTarget | None,
        runtime_config: Config | None = None,
    ) -> None:
        # Each request reads the token, so none is known yet.
        super().__init__(access_token="", default_market=default_market, timeout=timeout)
        self._provider = spotify_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config

    # AGNO_COMPAT: SpotifyTools sends one fixed access token and never renews it.
    # Reason: Agno 3.0.9 SpotifyTools takes only an access token and sends it on every request, while Spotify access
    # tokens expire after an hour, so every call after that returns an expired-token error.
    # Upstream issue: Tracking gap; no matching issue has been verified.
    # Upstream PR: No matching fix has been verified.
    # Remove when: Agno accepts a token provider or refresh credentials and renews expiring tokens itself;
    # reading the token from MindRoom's OAuth connection of the call's scope stays MindRoom behavior.
    # Coverage: tests/test_spotify_tools.py::test_tool_sends_the_refreshed_oauth_token,
    # tests/test_spotify_tools.py::test_tool_without_a_connection_asks_to_connect.
    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.access_token = self._current_access_token()
        return super()._make_request(endpoint, method, body, params)

    def _current_access_token(self) -> str:
        """Return a current access token, refreshing one about to expire, or raise the connect prompt."""
        context = active_oauth_credential_context(
            self._provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            config=self._config,
        )
        try:
            credentials = refresh_oauth_credentials_blocking(context)
        except OAuthCredentialUnreadableError:
            raise oauth_connection_required(context, reason=OAUTH_RESET_REQUIRED_REASON) from None
        except OAuthRefreshRejectedError:
            raise oauth_connection_required(context, reason=OAUTH_REFRESH_REJECTED_REASON) from None
        except OAuthProviderError as exc:
            logger.warning("spotify_oauth_refresh_failed", error_type=type(exc).__name__)
            msg = "Spotify authorization could not be refreshed. Retry this request shortly."
            raise OAuthProviderError(msg) from None
        token = (credentials or {}).get("token")
        if not oauth_credentials_usable(self._provider, self._runtime_paths, credentials) or not isinstance(token, str):
            raise oauth_connection_required(context)
        return token
