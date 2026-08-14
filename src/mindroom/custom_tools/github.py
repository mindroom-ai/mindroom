"""Requester-scoped OAuth wrapper for Agno's GitHub toolkit."""

from __future__ import annotations

import asyncio
import json
import threading
from contextvars import copy_context
from functools import wraps
from typing import TYPE_CHECKING, cast

from agno.tools.github import GithubTools as AgnoGithubTools
from github import Auth, Github

from mindroom.credentials import CredentialsManager, load_scoped_credentials
from mindroom.logging_config import get_logger
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProviderError, oauth_connection_required_payload
from mindroom.oauth.service import (
    build_oauth_connect_instruction,
    oauth_connect_url,
    oauth_credentials_usable,
    refresh_scoped_oauth_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_PENDING_ACCESS_TOKEN = "mindroom-oauth-connection-pending"  # noqa: S105


class GithubTools(AgnoGithubTools):
    """Agno GitHub tools authenticated by explicit or requester-scoped credentials."""

    def __init__(
        self,
        access_token: str | None = None,
        base_url: str | None = None,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager,
        worker_target: ResolvedWorkerTarget | None,
        **kwargs: object,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._oauth_provider = github_oauth_provider()
        explicit_access_token = access_token or runtime_paths.env_value("GITHUB_ACCESS_TOKEN")
        self._explicit_access_token = bool(explicit_access_token)
        initial_access_token = explicit_access_token or self._stored_access_token() or _PENDING_ACCESS_TOKEN
        self._active_access_token = initial_access_token
        super().__init__(access_token=initial_access_token, base_url=base_url, **kwargs)
        self._wrap_oauth_function_entrypoints()

    def _stored_access_token(self) -> str | None:
        credentials = load_scoped_credentials(
            self._oauth_provider.credential_service,
            credentials_manager=self._credentials_manager,
            worker_target=self._worker_target,
        )
        if credentials is None:
            return None
        token = credentials.get("token") or credentials.get("access_token")
        return token if isinstance(token, str) and token else None

    def _connection_required(self) -> OAuthConnectionRequired:
        connect_url = oauth_connect_url(
            self._oauth_provider,
            self._runtime_paths,
            worker_target=self._worker_target,
        )
        return OAuthConnectionRequired(
            build_oauth_connect_instruction(self._oauth_provider, connect_url),
            provider_id=self._oauth_provider.id,
            connect_url=connect_url,
        )

    def _refresh_oauth_credentials(self) -> dict[str, object] | None:
        refresh = refresh_scoped_oauth_credentials(
            self._oauth_provider,
            self._runtime_paths,
            credentials_manager=self._credentials_manager,
            worker_target=self._worker_target,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(refresh)

        result: list[dict[str, object] | None] = []
        errors: list[BaseException] = []
        context = copy_context()

        def run_refresh() -> None:
            try:
                refreshed = context.run(asyncio.run, refresh)
                result.append(cast("dict[str, object] | None", refreshed))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_refresh, name="mindroom-github-oauth-refresh")
        thread.start()
        thread.join()
        if errors:
            raise errors[0]
        return result[0]

    def _ensure_authenticated(self) -> None:
        if self._explicit_access_token:
            return
        try:
            credentials = self._refresh_oauth_credentials()
        except OAuthProviderError as exc:
            logger.warning(
                "github_oauth_refresh_failed",
                provider_id=self._oauth_provider.id,
                error_type=type(exc).__name__,
            )
            raise self._connection_required() from exc
        if not oauth_credentials_usable(self._oauth_provider, self._runtime_paths, credentials):
            raise self._connection_required()
        token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
        if not isinstance(token, str) or not token:
            raise self._connection_required()
        if token == self._active_access_token:
            return
        self.access_token = token
        self._active_access_token = token
        self.g = self.authenticate()

    def _wrap_oauth_function_entrypoints(self) -> None:
        for function in self.functions.values():
            entrypoint = function.entrypoint
            if entrypoint is None:
                continue

            @wraps(entrypoint)
            def oauth_entrypoint(
                *args: object,
                _entrypoint: Callable[..., object] = entrypoint,
                **kwargs: object,
            ) -> object:
                try:
                    self._ensure_authenticated()
                except OAuthConnectionRequired as exc:
                    return json.dumps(oauth_connection_required_payload(exc))
                return _entrypoint(*args, **kwargs)

            function.entrypoint = oauth_entrypoint
            setattr(self, function.name, oauth_entrypoint)

    def authenticate(self) -> Github:
        """Build the PyGithub client without logging credential values."""
        if not self.access_token:
            raise self._connection_required()
        auth = Auth.Token(self.access_token)
        if self.base_url:
            return Github(base_url=self.base_url, auth=auth)
        return Github(auth=auth)
