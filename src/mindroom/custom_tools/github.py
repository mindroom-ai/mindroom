"""Requester-scoped OAuth wrapper for Agno's GitHub toolkit."""

from __future__ import annotations

import json
import threading
from functools import wraps
from html import unescape
from typing import TYPE_CHECKING, Protocol, cast

from agno.tools.github import GithubTools as AgnoGithubTools
from github import Auth, Github, GithubException

from mindroom.config.auth import AuthorizationConfig  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot_if_readable_sync,
    oauth_credentials_usable,
    refresh_oauth_credentials_blocking,
    resolve_oauth_credential_context,
)
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import (
    OAuthConnectionRequired,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    oauth_connection_required_payload,
)
from mindroom.oauth.service import (
    OAUTH_ACCESS_REJECTED_REASON,
    OAUTH_REFRESH_REJECTED_REASON,
    oauth_connection_required,
)
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.worker_routing import active_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_PENDING_ACCESS_TOKEN = "mindroom-oauth-connection-pending"  # noqa: S105
_SANITIZED_OAUTH_REFRESH_ERROR_MESSAGE = "OAuth credential refresh failed"


class _GithubThreadState(threading.local):
    """Credential and PyGithub client owned by one worker thread."""

    def __init__(self) -> None:
        self.access_token: str | None = None
        self.client: Github | None = None


class _ContentWriteResult(Protocol):
    path: str
    sha: str
    html_url: str


class _CommitWriteResult(Protocol):
    sha: str
    html_url: str


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

    def _github_state(self) -> _GithubThreadState:
        state = self.__dict__.setdefault("_github_thread_state", _GithubThreadState())
        return cast("_GithubThreadState", state)

    @property
    def access_token(self) -> str | None:
        """Return token installed for current worker thread."""
        return self._github_state().access_token

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        state = self._github_state()
        if state.access_token != value:
            state.client = None
        state.access_token = value

    @property
    def g(self) -> Github:
        """Return PyGithub client installed for current worker thread."""
        client = self._github_state().client
        if client is None:
            raise self._connection_required()
        return client

    @g.setter
    def g(self, value: Github | None) -> None:
        self._github_state().client = value

    def __init__(
        self,
        access_token: str | None = None,
        base_url: str | None = None,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
        **kwargs: object,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._authorization = authorization
        self._oauth_provider = github_oauth_provider()
        explicit_access_token = _normalized_access_token(access_token) or _normalized_access_token(
            runtime_paths.env_value("GITHUB_ACCESS_TOKEN"),
        )
        self._explicit_access_token = bool(explicit_access_token)
        self._explicit_access_token_value = explicit_access_token
        initial_access_token = explicit_access_token or self._stored_access_token() or _PENDING_ACCESS_TOKEN
        super().__init__(access_token=initial_access_token, base_url=base_url, **kwargs)
        self._wrap_oauth_function_entrypoints()

    def _stored_access_token(self) -> str | None:
        context = self._oauth_credential_context()
        if self._oauth_provider.requester_scoped_credentials and context.worker_target is None:
            return None
        snapshot = load_oauth_credentials_snapshot_if_readable_sync(context)
        if snapshot is None:
            return None
        credentials = snapshot.credentials
        if credentials is None:
            return None
        token = credentials.get("token") or credentials.get("access_token")
        return _normalized_access_token(token)

    def _oauth_credential_context(self) -> OAuthCredentialContext:
        execution_identity = active_tool_execution_identity(None)
        if execution_identity is None and self._worker_target is not None:
            execution_identity = self._worker_target.execution_identity
        runtime_context = get_tool_runtime_context()
        authorization = runtime_context.config.authorization if runtime_context is not None else self._authorization
        return resolve_oauth_credential_context(
            self._oauth_provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            execution_identity=execution_identity,
            authorization=authorization,
        )

    def _connection_required(self, *, reason: str | None = None) -> OAuthConnectionRequired:
        return oauth_connection_required(self._oauth_credential_context(), reason=reason)

    def _refresh_oauth_credentials(self) -> dict[str, object] | None:
        context = self._oauth_credential_context()
        if self._oauth_provider.requester_scoped_credentials and context.worker_target is None:
            return None
        return refresh_oauth_credentials_blocking(context)

    def _ensure_authenticated(self) -> None:
        if self._explicit_access_token:
            token = self._explicit_access_token_value
            if token is None:
                raise self._connection_required()
            if self.access_token != token or self._github_state().client is None:
                self.access_token = token
                self.g = self.authenticate()
            return
        try:
            credentials = self._refresh_oauth_credentials()
        except OAuthProviderError as exc:
            logger.warning(
                "github_oauth_refresh_failed",
                provider_id=self._oauth_provider.id,
                error_type=type(exc).__name__,
            )
            if isinstance(exc, OAuthRefreshRejectedError):
                self.access_token = None
                raise self._connection_required(reason=OAUTH_REFRESH_REJECTED_REASON) from exc
            raise OAuthProviderError(
                _SANITIZED_OAUTH_REFRESH_ERROR_MESSAGE,
                oauth_error=exc.oauth_error,
            ) from None
        if not oauth_credentials_usable(self._oauth_provider, self._runtime_paths, credentials):
            raise self._connection_required()
        token = _normalized_access_token(
            (credentials or {}).get("token") or (credentials or {}).get("access_token"),
        )
        if token is None:
            raise self._connection_required()
        if token == self.access_token and self._github_state().client is not None:
            return
        self.access_token = token
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
                    self.access_token = None
                    return json.dumps(
                        oauth_connection_required_payload(
                            self._connection_required(reason=OAUTH_ACCESS_REJECTED_REASON),
                        ),
                    )
                return result

            function.entrypoint = oauth_entrypoint
            setattr(self, function.name, oauth_entrypoint)

    @wraps(AgnoGithubTools.update_file)
    def update_file(
        self,
        repo_name: str,
        path: str,
        content: str,
        message: str,
        sha: str,
        branch: str | None = None,
    ) -> str:
        """Update a file without requiring nested commit details in the response."""
        try:
            repo = self.g.get_repo(repo_name)
            if branch is None:
                result = repo.update_file(
                    path=path,
                    message=message,
                    content=content.encode("utf-8"),
                    sha=sha,
                )
            else:
                result = repo.update_file(
                    path=path,
                    message=message,
                    content=content.encode("utf-8"),
                    sha=sha,
                    branch=branch,
                )
        except GithubException as exc:
            logger.exception("github_file_update_failed", repo_name=repo_name, path=path)
            return json.dumps({"error": str(exc)})

        content_result = cast(_ContentWriteResult, result["content"])  # noqa: TC006
        commit_result = cast(_CommitWriteResult, result["commit"])  # noqa: TC006
        return json.dumps(
            {
                "path": content_result.path,
                "sha": content_result.sha,
                "url": content_result.html_url,
                "commit": {
                    "sha": commit_result.sha,
                    "message": message,
                    "url": commit_result.html_url,
                },
            },
            indent=2,
        )

    @wraps(AgnoGithubTools.delete_file)
    def delete_file(
        self,
        repo_name: str,
        path: str,
        message: str,
        sha: str,
        branch: str | None = None,
    ) -> str:
        """Delete a file without requiring nested commit details in the response."""
        try:
            repo = self.g.get_repo(repo_name)
            if branch is None:
                result = repo.delete_file(path=path, message=message, sha=sha)
            else:
                result = repo.delete_file(path=path, message=message, sha=sha, branch=branch)
        except GithubException as exc:
            logger.exception("github_file_delete_failed", repo_name=repo_name, path=path)
            return json.dumps({"error": str(exc)})

        commit_result = cast(_CommitWriteResult, result["commit"])  # noqa: TC006
        return json.dumps(
            {
                "message": f"File {path} deleted successfully",
                "commit": {
                    "sha": commit_result.sha,
                    "message": message,
                    "url": commit_result.html_url,
                },
            },
            indent=2,
        )

    @wraps(AgnoGithubTools.edit_issue)
    def edit_issue(
        self,
        repo_name: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
    ) -> str:
        """Edit only explicitly supplied issue fields."""
        if title is None and body is None:
            return json.dumps({"error": f"Provide a title or body to update issue #{issue_number}."})

        try:
            issue = self.g.get_repo(repo_name).get_issue(number=issue_number)
            if title is None:
                assert body is not None
                issue.edit(body=body)
            elif body is None:
                issue.edit(title=title)
            else:
                issue.edit(title=title, body=body)
        except GithubException as exc:
            logger.exception("github_issue_edit_failed", repo_name=repo_name, issue_number=issue_number)
            return json.dumps({"error": str(exc)})
        return json.dumps({"message": f"Issue #{issue_number} updated."}, indent=2)

    @wraps(AgnoGithubTools.get_pull_request_count)
    def get_pull_request_count(
        self,
        repo_name: str,
        state: str = "all",
        author: str | None = None,
        base: str | None = None,
        head: str | None = None,
    ) -> str:
        """Count pull requests even when PyGithub omits the aggregate count."""
        filters = {"state": state}
        if base is not None:
            filters["base"] = base
        if head is not None:
            filters["head"] = head

        try:
            pulls = self.g.get_repo(repo_name).get_pulls(**filters)
            if author is not None:
                count = sum(1 for pull in pulls if pull.user.login == author and state in ("all", pull.state))
            else:
                count = pulls.totalCount
                if count is None:
                    count = sum(1 for _pull in pulls)
        except GithubException as exc:
            logger.exception("github_pull_request_count_failed", repo_name=repo_name)
            return json.dumps({"error": str(exc)})
        return json.dumps({"count": count}, indent=2)

    @wraps(AgnoGithubTools.search_issues_and_prs)
    def search_issues_and_prs(
        self,
        query: str,
        state: str | None = None,
        type_filter: str | None = None,
        repo: str | None = None,
        user: str | None = None,
        label: str | None = None,
        sort: str = "created",
        order: str = "desc",
        page: int = 1,
        per_page: int = 30,
    ) -> str:
        """Search for issues and pull requests while restoring escaped operators."""
        return super().search_issues_and_prs(
            query=unescape(query),
            state=state,
            type_filter=type_filter,
            repo=repo,
            user=user,
            label=label,
            sort=sort,
            order=order,
            page=page,
            per_page=per_page,
        )

    def authenticate(self) -> Github:
        """Build the PyGithub client without logging credential values."""
        if not self.access_token:
            raise self._connection_required()
        auth = Auth.Token(self.access_token)
        if self.base_url:
            return Github(base_url=self.base_url, auth=auth)
        return Github(auth=auth)
