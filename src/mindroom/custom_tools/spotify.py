"""Spotify toolkit that renews the dashboard's OAuth access token before it expires."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.spotify import SpotifyTools as AgnoSpotifyTools

from mindroom.credentials import CredentialsManager, load_scoped_credentials, save_scoped_credentials
from mindroom.spotify_tokens import current_spotify_credentials

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class SpotifyTools(AgnoSpotifyTools):
    """Agno Spotify toolkit whose stored access token is renewed in the primary process, which holds the client secret."""

    def __init__(
        self,
        access_token: str,
        default_market: str | None = "US",
        timeout: int = 30,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager,
        worker_target: ResolvedWorkerTarget | None,
    ) -> None:
        super().__init__(access_token=access_token, default_market=default_market, timeout=timeout)
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target

    # AGNO_COMPAT: SpotifyTools sends one fixed access token and never renews it.
    # Reason: Agno 3.0.9 SpotifyTools takes only an access token and sends it on every request, while Spotify access
    # tokens expire after an hour, so every call after that returns an expired-token error.
    # Upstream issue: Tracking gap; no matching issue has been verified.
    # Upstream PR: No matching fix has been verified.
    # Remove when: Agno accepts a token provider or refresh credentials and renews expiring tokens itself;
    # saving the renewed token in MindRoom's scoped credential store stays MindRoom behavior.
    # Coverage: tests/test_spotify_tools.py::test_tool_renews_an_expiring_token_before_calling_spotify;
    # tests/test_spotify_tools.py::test_tool_without_the_client_secret_keeps_the_stored_token.
    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        credentials = load_scoped_credentials(
            "spotify",
            credentials_manager=self._credentials_manager,
            worker_target=self._worker_target,
            primary_built_tool=True,
        )
        if credentials and credentials.get("access_token"):
            renewed = current_spotify_credentials(credentials, self._runtime_paths, self._save_credentials)
            self.access_token = renewed["access_token"]
        return super()._make_request(endpoint, method, body, params)

    def _save_credentials(self, credentials: dict[str, Any]) -> None:
        save_scoped_credentials(
            "spotify",
            credentials,
            credentials_manager=self._credentials_manager,
            worker_target=self._worker_target,
            primary_built_tool=True,
        )
