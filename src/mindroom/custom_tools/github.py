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
    oauth_credentials_worker_target,
    refresh_scoped_oauth_credentials,
)
from mindroom.tool_system.worker_routing import active_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_PENDING_ACCESS_TOKEN = "mindroom-oauth-connection-pending"  # noqa: S105


def _normalized_access_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _is_github_authentication_error(result: object) -> bool:
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return isinstance(error, str) and error.lstrip().startswith("401 ")


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
        explicit_access_token = _normalized_access_token(access_token) or _normalized_access_token(
            runtime_paths.env_value("GITHUB_ACCESS_TOKEN"),
        )
        self._explicit_access_token = bool(explicit_access_token)
        initial_access_token = explicit_access_token or self._stored_access_token() or _PENDING_ACCESS_TOKEN
        self._active_access_token = initial_access_token
        super().__init__(access_token=initial_access_token, base_url=base_url, **kwargs)
        self._wrap_oauth_function_entrypoints()

    def _stored_access_token(self) -> str | None:
        worker_target = self._oauth_credentials_worker_target()
        if self._oauth_provider.requester_scoped_credentials and worker_target is None:
            return None
        credentials = load_scoped_credentials(
            self._oauth_provider.credential_service,
            credentials_manager=self._credentials_manager,
            worker_target=worker_target,
        )
        if credentials is None:
            return None
        token = credentials.get("token") or credentials.get("access_token")
        return _normalized_access_token(token)

    def _oauth_credentials_worker_target(self) -> ResolvedWorkerTarget | None:
        execution_identity = active_tool_execution_identity(None)
        if execution_identity is None and self._worker_target is not None:
            execution_identity = self._worker_target.execution_identity
        return oauth_credentials_worker_target(
            self._oauth_provider,
            self._worker_target,
            execution_identity=execution_identity,
        )

    def _connection_required(self) -> OAuthConnectionRequired:
        connect_url = oauth_connect_url(
            self._oauth_provider,
            self._runtime_paths,
            worker_target=self._oauth_credentials_worker_target(),
        )
        return OAuthConnectionRequired(
            build_oauth_connect_instruction(self._oauth_provider, connect_url),
            provider_id=self._oauth_provider.id,
            connect_url=connect_url,
        )

    def _refresh_oauth_credentials(self) -> dict[str, object] | None:
        worker_target = self._oauth_credentials_worker_target()
        if self._oauth_provider.requester_scoped_credentials and worker_target is None:
            return None
        refresh = refresh_scoped_oauth_credentials(
            self._oauth_provider,
            self._runtime_paths,
            credentials_manager=self._credentials_manager,
            worker_target=worker_target,
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
        token = _normalized_access_token(
            (credentials or {}).get("token") or (credentials or {}).get("access_token"),
        )
        if token is None:
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
                result = _entrypoint(*args, **kwargs)
                if not self._explicit_access_token and _is_github_authentication_error(result):
                    return json.dumps(oauth_connection_required_payload(self._connection_required()))
                return result

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
