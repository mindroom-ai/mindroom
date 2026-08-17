"""Requester-scoped OAuth wrapper for Agno's GitHub toolkit."""

from __future__ import annotations

import json
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from html import unescape
from typing import TYPE_CHECKING, Protocol, cast

from agno.tools import github as agno_github_module
from agno.tools.github import GithubTools as AgnoGithubTools
from agno.utils import log as agno_log_module
from github import Auth, Github, GithubException

from mindroom.config.auth import AuthorizationConfig  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot_if_readable_sync,
    oauth_credentials_usable,
    refresh_oauth_credentials_blocking,
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

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_PENDING_ACCESS_TOKEN = "mindroom-oauth-connection-pending"  # noqa: S105
_SANITIZED_OAUTH_REFRESH_ERROR_MESSAGE = "OAuth credential refresh failed"
_SANITIZED_GITHUB_PROVIDER_ERROR_MESSAGE = "GitHub request failed"
_AGNO_GITHUB_PROVIDER_DETAIL_LOG_PREFIXES = (
    "Error getting actual open issues:",
    "Error getting open PRs count:",
    "Error processing individual PR:",
    "Error getting recent open PRs:",
    "Error calculating PR metrics:",
    "Error getting contributors:",
    "Error decoding file content:",
)
_TYPED_PROVIDER_FAILURE_ENTRYPOINTS = frozenset(
    {"delete_file", "edit_issue", "get_pull_request_count", "update_file"},
)


@dataclass(frozen=True, slots=True)
class _GithubProviderFailure:
    """Typed provider failure captured before Agno serializes the exception."""

    status_code: int | None


_github_provider_failure: ContextVar[_GithubProviderFailure | None] = ContextVar(
    "github_provider_failure",
    default=None,
)


def _github_exception_status_code(exc: GithubException) -> int | None:
    status_code = exc.status
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return status_code
    return None


class _SanitizeAgnoGithubLogFilter(logging.Filter):
    """Remove provider-controlled GitHub exception text before log rendering."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is not None:
            _exception_type, exception, _traceback = record.exc_info
            if isinstance(exception, GithubException):
                status_code = _github_exception_status_code(exception)
                _github_provider_failure.set(_GithubProviderFailure(status_code))
                record.msg = "GitHub provider request failed (error_type=GithubException, status_code=%s)"
                record.args = (status_code if status_code is not None else "unknown",)
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
        elif isinstance(record.msg, str) and record.msg.startswith(_AGNO_GITHUB_PROVIDER_DETAIL_LOG_PREFIXES):
            record.msg = "GitHub provider detail suppressed"
            record.args = ()
        return True


def _install_agno_github_log_sanitizers() -> None:
    upstream_loggers = {
        agno_github_module.logger,
        agno_log_module.logger,
        agno_log_module.agent_logger,
        agno_log_module.team_logger,
        agno_log_module.workflow_logger,
    }
    for upstream_logger in upstream_loggers:
        if not any(isinstance(log_filter, _SanitizeAgnoGithubLogFilter) for log_filter in upstream_logger.filters):
            upstream_logger.addFilter(_SanitizeAgnoGithubLogFilter())


_install_agno_github_log_sanitizers()


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


def _sanitized_github_exception_result(exc: GithubException) -> str:
    status_code = _github_exception_status_code(exc)
    _github_provider_failure.set(_GithubProviderFailure(status_code))
    return json.dumps({"error": _SANITIZED_GITHUB_PROVIDER_ERROR_MESSAGE})


def _is_serialized_github_error_result(result: object) -> bool:
    """Recognize Agno's generic error envelope without reparsing exception text."""
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and set(payload) == {"error"} and isinstance(payload["error"], str)


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
        self._explicit_access_token = explicit_access_token
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
        return active_oauth_credential_context(
            self._oauth_provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            authorization=self._authorization,
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
            token = self._explicit_access_token
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
        stored_token = _normalized_access_token(
            (credentials or {}).get("token") or (credentials or {}).get("access_token"),
        )
        if stored_token is None:
            raise self._connection_required()
        if stored_token == self.access_token and self._github_state().client is not None:
            return
        self.access_token = stored_token
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
                _function_name: str = function.name,
                **kwargs: object,
            ) -> object:
                try:
                    self._ensure_authenticated()
                except OAuthConnectionRequired as exc:
                    return json.dumps(oauth_connection_required_payload(exc))
                failure_token = _github_provider_failure.set(None)
                try:
                    result = _entrypoint(*args, **kwargs)
                    provider_failure = _github_provider_failure.get()
                finally:
                    _github_provider_failure.reset(failure_token)
                if provider_failure is None:
                    if _function_name not in _TYPED_PROVIDER_FAILURE_ENTRYPOINTS and _is_serialized_github_error_result(
                        result,
                    ):
                        return json.dumps({"error": _SANITIZED_GITHUB_PROVIDER_ERROR_MESSAGE})
                    return result
                status_code = provider_failure.status_code
                logger.warning(
                    "github_tool_call_failed",
                    provider_id=self._oauth_provider.id,
                    error_type="GithubException",
                    status_code=status_code if status_code is not None else "unknown",
                )
                if not self._explicit_access_token and status_code == 401:
                    self.access_token = None
                    return json.dumps(
                        oauth_connection_required_payload(
                            self._connection_required(reason=OAUTH_ACCESS_REJECTED_REASON),
                        ),
                    )
                return json.dumps({"error": _SANITIZED_GITHUB_PROVIDER_ERROR_MESSAGE})

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
            return _sanitized_github_exception_result(exc)

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
            return _sanitized_github_exception_result(exc)

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
            return _sanitized_github_exception_result(exc)
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
            return _sanitized_github_exception_result(exc)
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
