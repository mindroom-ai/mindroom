"""GitHub tools backed by explicit or MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import logging
import threading
from functools import wraps
from html import unescape
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote

from github import Auth, Github, GithubException

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    OAuthCredentialUnreadableError,
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
    OAUTH_RESET_REQUIRED_REASON,
    oauth_connection_required,
)
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_SANITIZED_OAUTH_REFRESH_ERROR_MESSAGE = "OAuth credential refresh failed"
_SANITIZED_GITHUB_PROVIDER_ERROR_MESSAGE = "GitHub request failed"
_GITHUB_PROVIDER_DETAIL_MAX_CHARS = 500
# Fixed hints explain statuses GitHub also uses for hidden resources and rate limits.
_GITHUB_PROVIDER_STATUS_HINTS = {
    403: "this GitHub connection lacks permission for the request, or GitHub rate-limited it",
    404: (
        "the requested repository, ref, path, issue, or other resource does not exist, "
        "or this GitHub connection cannot access it"
    ),
}


class _SanitizeGithubRetryLogFilter(logging.Filter):
    """Remove provider-controlled GitHub retry details before log rendering."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "github.GithubRetry":
            record.msg = "GitHub provider retry detail suppressed"
            record.args = ()
        return True


def _install_github_retry_log_filter() -> None:
    retry_logger = logging.getLogger("github.GithubRetry")
    if not any(isinstance(log_filter, _SanitizeGithubRetryLogFilter) for log_filter in retry_logger.filters):
        retry_logger.addFilter(_SanitizeGithubRetryLogFilter())


_install_github_retry_log_filter()


def _github_exception_status_code(exc: GithubException) -> int | None:
    status_code = exc.status
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return status_code
    return None


def _github_error_entry_text(error: object) -> str | None:
    if isinstance(error, str):
        return error
    if not isinstance(error, dict):
        return None
    entry = cast("dict[str, object]", error)
    labels = ", ".join(f"{key}: {entry[key]}" for key in ("field", "code") if isinstance(entry.get(key), str))
    message = entry.get("message")
    if not isinstance(message, str):
        return labels
    return f"{message} ({labels})" if labels else message


def _github_exception_detail(exc: GithubException) -> str | None:
    """Return GitHub's message and per-field errors as bounded, credential-redacted plain text."""
    data = exc.data
    if not isinstance(data, dict):
        return None
    errors = data.get("errors")
    parts = [data.get("message"), *map(_github_error_entry_text, errors if isinstance(errors, list) else ())]
    detail = " ".join("; ".join(part for part in parts if isinstance(part, str) and part).split())
    return redact_sensitive_text(detail, max_length=_GITHUB_PROVIDER_DETAIL_MAX_CHARS) or None


def _github_provider_error_result(status_code: int | None, detail: str | None) -> str:
    message = _SANITIZED_GITHUB_PROVIDER_ERROR_MESSAGE
    if status_code is not None:
        message = f"{message} with HTTP {status_code}"
        hint = _GITHUB_PROVIDER_STATUS_HINTS.get(status_code)
        if hint is not None:
            message = f"{message}: {hint}"
    if detail is not None:
        message = f"{message}. GitHub said: {detail}"
    return json.dumps({"error": message})


def _normalized_access_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _numeric_languages(raw_languages: dict[str, Any]) -> dict[str, int]:
    """Keep only numeric language byte counts (PyGithub 2.9+ injects a string ``url`` key)."""
    return {
        str(language): int(count)
        for language, count in raw_languages.items()
        if isinstance(count, int) and not isinstance(count, bool)
    }


def _safe_primitive(value: Any) -> Any:  # noqa: ANN401
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, int | float | bool | str) or value is None:
        return value
    return str(value)


def _is_binary_text(text: str) -> bool:
    if "\x00" in text:
        return True
    control_chars = sum(
        1 for ch in text[:1000] if ch not in "\t\n\r\x0b\x0c" and (ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F)
    )
    return control_chars > 200


def _collect_actual_open_issues(repo: Any) -> int | None:  # noqa: ANN401
    try:
        return sum(1 for issue in repo.get_issues(state="open") if not issue.pull_request)
    except Exception as exc:
        logger.debug("github_repo_stats_open_issues_failed", error_type=type(exc).__name__)
        return None


def _collect_open_pr_count(repo: Any) -> int | None:  # noqa: ANN401
    try:
        return int(repo.get_pulls(state="open").totalCount)
    except Exception as exc:
        logger.debug("github_repo_stats_open_prs_failed", error_type=type(exc).__name__)
        return None


def _collect_recent_open_prs(repo: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    recent: list[dict[str, Any]] = []
    try:
        for pr in repo.get_pulls(state="open"):
            if len(recent) >= 10:
                break
            try:
                recent.append(
                    {
                        "number": int(pr.number),
                        "title": str(pr.title),
                        "user": str(pr.user.login),
                        "created_at": _safe_primitive(pr.created_at),
                        "updated_at": _safe_primitive(pr.updated_at),
                        "url": str(pr.html_url),
                        "base": str(pr.base.ref),
                        "head": str(pr.head.ref),
                        "comment_count": int(pr.comments),
                    },
                )
            except Exception as exc:
                logger.debug("github_repo_stats_pr_item_failed", error_type=type(exc).__name__)
    except Exception as exc:
        logger.debug("github_repo_stats_recent_prs_failed", error_type=type(exc).__name__)
        return []
    return recent


def _collect_pr_metrics(repo: Any) -> dict[str, Any] | None:  # noqa: ANN401
    try:
        all_prs: list[Any] = []
        for pr in repo.get_pulls(state="all", sort="created", direction="desc"):
            if len(all_prs) >= 100:
                break
            all_prs.append(pr)
        merged_prs = [pr for pr in all_prs if pr.is_merged()]
        merge_times = [
            (pr.merged_at - pr.created_at).total_seconds() / 3600 for pr in merged_prs if pr.merged_at and pr.created_at
        ]
        return {
            "total_prs": len(all_prs),
            "merged_prs": len(merged_prs),
            "acceptance_rate": (len(merged_prs) / len(all_prs) * 100) if all_prs else 0,
            "avg_time_to_merge": (sum(merge_times) / len(merge_times)) if merge_times else None,
        }
    except Exception as exc:
        logger.debug("github_repo_stats_pr_metrics_failed", error_type=type(exc).__name__)
        return None


def _collect_contributors(repo: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    contributors: list[dict[str, Any]] = []
    try:
        for contributor in repo.get_contributors():
            if len(contributors) >= 20:
                break
            contributors.append(
                {
                    "login": str(contributor.login),
                    "contributions": int(contributor.contributions),
                    "url": str(contributor.html_url),
                },
            )
    except Exception as exc:
        logger.debug("github_repo_stats_contributors_failed", error_type=type(exc).__name__)
        return []
    return contributors


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


class GithubTools(Toolkit):
    """Native GitHub tools authenticated by explicit or requester-scoped OAuth credentials."""

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
            previous_client = state.client
            state.client = None
            if previous_client is not None:
                previous_client.close()
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
        runtime_config: Config | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        self._oauth_provider = github_oauth_provider()
        self.base_url = base_url
        self._explicit_access_token = _normalized_access_token(access_token)
        # Stored OAuth tokens are resolved lazily per calling thread; explicit tokens initialize immediately.
        if self._explicit_access_token is not None:
            self.access_token = self._explicit_access_token
            self.g = self.authenticate()

        tools: list[Callable[..., str]] = [
            self.search_repositories,
            self.list_repositories,
            self.get_repository,
            self.get_pull_request,
            self.get_pull_request_changes,
            self.create_issue,
            self.create_repository,
            self.delete_repository,
            self.list_branches,
            self.get_repository_languages,
            self.get_pull_request_count,
            self.get_repository_stars,
            self.get_pull_requests,
            self.get_pull_request_comments,
            self.create_pull_request_comment,
            self.edit_pull_request_comment,
            self.get_pull_request_with_details,
            self.get_repository_with_stats,
            self.list_issues,
            self.get_issue,
            self.comment_on_issue,
            self.close_issue,
            self.reopen_issue,
            self.assign_issue,
            self.label_issue,
            self.list_issue_comments,
            self.edit_issue,
            self.create_pull_request,
            self.create_file,
            self.get_file_content,
            self.update_file,
            self.delete_file,
            self.get_directory_content,
            self.get_branch_content,
            self.create_branch,
            self.set_default_branch,
            self.search_code,
            self.search_issues_and_prs,
            self.create_review_request,
        ]
        super().__init__(name="github", tools=tools)
        self._wrap_oauth_function_entrypoints()

    def _oauth_credential_context(self) -> OAuthCredentialContext:
        return active_oauth_credential_context(
            self._oauth_provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            config=self._config,
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
            if isinstance(exc, OAuthCredentialUnreadableError):
                self.access_token = None
                raise self._connection_required(reason=OAUTH_RESET_REQUIRED_REASON) from exc
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

    def _handle_github_exception(self, tool_name: str, exc: GithubException) -> str:
        status_code = _github_exception_status_code(exc)
        detail = _github_exception_detail(exc)
        logger.warning(
            "github_tool_call_failed",
            provider_id=self._oauth_provider.id,
            tool_name=tool_name,
            error_type="GithubException",
            status_code=status_code,
        )
        if not self._explicit_access_token and status_code == 401:
            self.access_token = None
            return json.dumps(
                oauth_connection_required_payload(
                    self._connection_required(reason=OAUTH_ACCESS_REJECTED_REASON),
                ),
            )
        return _github_provider_error_result(status_code, detail)

    def _wrap_oauth_function_entrypoints(self) -> None:
        for name, entrypoint in tuple(self.functions.items()):

            @wraps(entrypoint)
            def oauth_entrypoint(
                *args: object,
                _entrypoint: Callable[..., str] = entrypoint,
                _tool_name: str = name,
                **kwargs: object,
            ) -> str:
                try:
                    self._ensure_authenticated()
                    return _entrypoint(*args, **kwargs)
                except OAuthConnectionRequired as exc:
                    return json.dumps(oauth_connection_required_payload(exc))
                except GithubException as exc:
                    return self._handle_github_exception(_tool_name, exc)

            self.functions[name] = oauth_entrypoint
            setattr(self, name, oauth_entrypoint)

    def authenticate(self) -> Github:
        """Build the PyGithub client without logging credential values."""
        if not self.access_token:
            raise self._connection_required()
        auth = Auth.Token(self.access_token)
        if self.base_url:
            return Github(base_url=self.base_url, auth=auth)
        return Github(auth=auth)

    def search_repositories(
        self,
        query: str,
        sort: str = "stars",
        order: str = "desc",
        page: int = 1,
        per_page: int = 30,
    ) -> str:
        """Search for repositories on GitHub.

        Note: GitHub's Search API has a maximum limit of 1000 results per query.

        Args:
            query: The search query keywords.
            sort: The field to sort results by ('stars', 'forks', or 'updated'). Defaults to 'stars'.
            order: The order of results ('asc' or 'desc'). Defaults to 'desc'.
            page: Page number of results to return, counting from 1. Defaults to 1.
            per_page: Number of results per page. Max 100. Defaults to 30.

        Returns:
            A JSON-formatted string containing a list of repositories matching the search query.

        """
        per_page = min(per_page, 100)
        repositories = self.g.search_repositories(query=query, sort=sort, order=order)
        repo_list = [
            {
                "full_name": repo.full_name,
                "description": repo.description,
                "url": repo.html_url,
                "stars": repo.stargazers_count,
                "forks": repo.forks_count,
                "language": repo.language,
            }
            for repo in repositories.get_page(page - 1)[:per_page]
        ]
        return json.dumps(repo_list, indent=2)

    def list_repositories(self) -> str:
        """List all repositories for the authenticated user.

        Returns:
            A JSON-formatted string containing a list of repository names.

        """
        repos = self.g.get_user().get_repos()
        return json.dumps([repo.full_name for repo in repos], indent=2)

    def create_repository(
        self,
        name: str,
        private: bool = False,
        description: str | None = None,
        auto_init: bool = False,
        organization: str | None = None,
    ) -> str:
        """Create a new repository on GitHub.

        Args:
            name: The name of the repository.
            private: Whether the repository is private. Defaults to False.
            description: A short description of the repository.
            auto_init: Whether to initialize the repository with a README. Defaults to False.
            organization: Name of organization to create repo in. If None, creates in user account.

        Returns:
            A JSON-formatted string containing the created repository details.

        """
        resolved_description = description if description is not None else ""
        owner: Any = self.g.get_organization(organization) if organization else self.g.get_user()
        repo = owner.create_repo(
            name=name,
            private=private,
            description=resolved_description,
            auto_init=auto_init,
        )
        return json.dumps(
            {
                "name": repo.full_name,
                "url": repo.html_url,
                "private": repo.private,
                "description": repo.description,
            },
            indent=2,
        )

    def get_repository(self, repo_name: str) -> str:
        """Get details of a specific repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').

        Returns:
            A JSON-formatted string containing repository details.

        """
        repo = self.g.get_repo(repo_name)
        return json.dumps(
            {
                "name": repo.full_name,
                "description": repo.description,
                "url": repo.html_url,
                "stars": repo.stargazers_count,
                "forks": repo.forks_count,
                "open_issues": repo.open_issues_count,
                "language": repo.language,
                "license": repo.license.name if repo.license else None,
                "default_branch": repo.default_branch,
            },
            indent=2,
        )

    def get_repository_languages(self, repo_name: str) -> str:
        """Get the languages used in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').

        Returns:
            A JSON-formatted string containing the map of languages to byte counts.

        """
        repo = self.g.get_repo(repo_name)
        return json.dumps(_numeric_languages(repo.get_languages()), indent=2)

    def get_pull_request_count(
        self,
        repo_name: str,
        state: str = "all",
        author: str | None = None,
        base: str | None = None,
        head: str | None = None,
    ) -> str:
        """Get the count of pull requests for a repository based on query parameters.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            state: The state of the PRs to count ('open', 'closed', 'all'). Defaults to 'all'.
            author: Filter PRs by author username.
            base: Filter PRs by base branch name.
            head: Filter PRs by head branch name.

        Returns:
            A JSON-formatted string containing the count of pull requests.

        """
        filters: dict[str, Any] = {"state": state}
        if base is not None:
            filters["base"] = base
        if head is not None:
            filters["head"] = head

        pulls = self.g.get_repo(repo_name).get_pulls(**filters)
        if author is not None:
            count = sum(1 for pull in pulls if pull.user.login == author and state in ("all", pull.state))
        else:
            count = pulls.totalCount
            if count is None:
                count = sum(1 for _pull in pulls)
        return json.dumps({"count": count}, indent=2)

    def get_pull_request(self, repo_name: str, pr_number: int) -> str:
        """Get details of a specific pull request.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            pr_number: The number of the pull request.

        Returns:
            A JSON-formatted string containing pull request details.

        """
        pr = self.g.get_repo(repo_name).get_pull(pr_number)
        return json.dumps(
            {
                "number": pr.number,
                "title": pr.title,
                "user": pr.user.login,
                "body": pr.body,
                "created_at": pr.created_at.isoformat(),
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                "state": pr.state,
                "merged": pr.is_merged(),
                "mergeable": pr.mergeable,
                "url": pr.html_url,
            },
            indent=2,
        )

    def get_pull_request_changes(self, repo_name: str, pr_number: int) -> str:
        """Get the changes (files modified) in a pull request.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            pr_number: The number of the pull request.

        Returns:
            A JSON-formatted string containing the list of changed files.

        """
        pr = self.g.get_repo(repo_name).get_pull(pr_number)
        changes = [
            {
                "filename": file.filename,
                "status": file.status,
                "additions": file.additions,
                "deletions": file.deletions,
                "changes": file.changes,
                "raw_url": file.raw_url,
                "blob_url": file.blob_url,
                "patch": file.patch,
            }
            for file in pr.get_files()
        ]
        return json.dumps(changes, indent=2)

    def create_issue(self, repo_name: str, title: str, body: str | None = None) -> str:
        """Create an issue in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            title: The title of the issue.
            body: The body content of the issue.

        Returns:
            A JSON-formatted string containing the created issue details.

        """
        repo = self.g.get_repo(repo_name)
        issue = repo.create_issue(title=title) if body is None else repo.create_issue(title=title, body=body)
        return json.dumps(
            {
                "id": issue.id,
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "url": issue.html_url,
                "state": issue.state,
                "created_at": issue.created_at.isoformat(),
                "user": issue.user.login,
            },
            indent=2,
        )

    def list_issues(self, repo_name: str, state: str = "open", page: int = 1, per_page: int = 20) -> str:
        """List issues for a repository with pagination.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            state: The state of issues to list ('open', 'closed', 'all'). Defaults to 'open'.
            page: Page number of results to return, counting from 1. Defaults to 1.
            per_page: Number of results per page. Defaults to 20.

        Returns:
            A JSON-formatted string containing a list of issues with pagination metadata.

        """
        all_issues = [issue for issue in self.g.get_repo(repo_name).get_issues(state=state) if not issue.pull_request]
        total_issues = len(all_issues)
        total_pages = (total_issues + per_page - 1) // per_page
        resolved_page = max(1, min(page, total_pages) if total_pages > 0 else 1)
        start = (resolved_page - 1) * per_page
        page_slice = all_issues[start : start + per_page]
        issue_list = [
            {
                "number": issue.number,
                "title": issue.title,
                "user": issue.user.login,
                "created_at": issue.created_at.isoformat(),
                "state": issue.state,
                "url": issue.html_url,
            }
            for issue in page_slice
        ]
        meta = {
            "current_page": resolved_page,
            "per_page": per_page,
            "total_items": total_issues,
            "total_pages": total_pages,
        }
        return json.dumps({"data": issue_list, "meta": meta}, indent=2)

    def get_issue(self, repo_name: str, issue_number: int) -> str:
        """Get details of a specific issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.

        Returns:
            A JSON-formatted string containing issue details.

        """
        issue = self.g.get_repo(repo_name).get_issue(number=issue_number)
        return json.dumps(
            {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "user": issue.user.login,
                "state": issue.state,
                "created_at": issue.created_at.isoformat(),
                "updated_at": issue.updated_at.isoformat(),
                "url": issue.html_url,
                "assignees": [assignee.login for assignee in issue.assignees],
                "labels": [label.name for label in issue.labels],
            },
            indent=2,
        )

    def comment_on_issue(self, repo_name: str, issue_number: int, comment_body: str) -> str:
        """Add a comment to an issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.
            comment_body: The content of the comment.

        Returns:
            A JSON-formatted string containing the comment details.

        """
        comment = self.g.get_repo(repo_name).get_issue(number=issue_number).create_comment(body=comment_body)
        return json.dumps(
            {
                "id": comment.id,
                "body": comment.body,
                "user": comment.user.login,
                "created_at": comment.created_at.isoformat(),
                "url": comment.html_url,
            },
            indent=2,
        )

    def close_issue(self, repo_name: str, issue_number: int) -> str:
        """Close an issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.

        Returns:
            A JSON-formatted string confirming the issue is closed.

        """
        self.g.get_repo(repo_name).get_issue(number=issue_number).edit(state="closed")
        return json.dumps({"message": f"Issue #{issue_number} closed."}, indent=2)

    def reopen_issue(self, repo_name: str, issue_number: int) -> str:
        """Reopen a closed issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.

        Returns:
            A JSON-formatted string confirming the issue is reopened.

        """
        self.g.get_repo(repo_name).get_issue(number=issue_number).edit(state="open")
        return json.dumps({"message": f"Issue #{issue_number} reopened."}, indent=2)

    def assign_issue(self, repo_name: str, issue_number: int, assignees: list[str]) -> str:
        """Assign users to an issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.
            assignees: A list of usernames to assign.

        Returns:
            A JSON-formatted string confirming the assignees.

        """
        self.g.get_repo(repo_name).get_issue(number=issue_number).edit(assignees=cast("list[Any]", assignees))
        return json.dumps({"message": f"Issue #{issue_number} assigned to {assignees}."}, indent=2)

    def label_issue(self, repo_name: str, issue_number: int, labels: list[str]) -> str:
        """Add labels to an issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.
            labels: A list of label names to add.

        Returns:
            A JSON-formatted string confirming the labels.

        """
        self.g.get_repo(repo_name).get_issue(number=issue_number).edit(labels=labels)
        return json.dumps({"message": f"Labels {labels} added to issue #{issue_number}."}, indent=2)

    def list_issue_comments(self, repo_name: str, issue_number: int) -> str:
        """List comments on an issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.

        Returns:
            A JSON-formatted string containing a list of comments.

        """
        comments = self.g.get_repo(repo_name).get_issue(number=issue_number).get_comments()
        comment_list = [
            {
                "id": comment.id,
                "user": comment.user.login,
                "body": comment.body,
                "created_at": comment.created_at.isoformat(),
                "url": comment.html_url,
            }
            for comment in comments
        ]
        return json.dumps(comment_list, indent=2)

    def edit_issue(
        self,
        repo_name: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
    ) -> str:
        """Edit the title or body of an issue.

        Args:
            repo_name: The full name of the repository.
            issue_number: The number of the issue.
            title: The new title for the issue.
            body: The new body content for the issue.

        Returns:
            A JSON-formatted string confirming the issue has been updated.

        """
        if title is None and body is None:
            return json.dumps({"error": f"Provide a title or body to update issue #{issue_number}."})
        issue = self.g.get_repo(repo_name).get_issue(number=issue_number)
        if title is None:
            assert body is not None
            issue.edit(body=body)
        elif body is None:
            issue.edit(title=title)
        else:
            issue.edit(title=title, body=body)
        return json.dumps({"message": f"Issue #{issue_number} updated."}, indent=2)

    def delete_repository(self, repo_name: str) -> str:
        """Delete a repository (requires admin permissions).

        Args:
            repo_name: Full repository name (e.g., 'owner/repo').

        Returns:
            Success confirmation or error message.

        """
        self.g.get_repo(repo_name).delete()
        return json.dumps({"message": f"Repository {repo_name} deleted successfully"}, indent=2)

    def list_branches(self, repo_name: str) -> str:
        """List all branches in a repository.

        Args:
            repo_name: Full repository name (e.g., 'owner/repo').

        Returns:
            JSON list of branch names.

        """
        branches = [branch.name for branch in self.g.get_repo(repo_name).get_branches()]
        return json.dumps(branches, indent=2)

    def get_repository_stars(self, repo_name: str) -> str:
        """Get the number of stars for a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').

        Returns:
            A JSON-formatted string containing the star count.

        """
        repo = self.g.get_repo(repo_name)
        return json.dumps({"stars": repo.stargazers_count}, indent=2)

    def get_pull_requests(
        self,
        repo_name: str,
        state: str = "open",
        sort: str = "created",
        direction: str = "desc",
        limit: int = 50,
    ) -> str:
        """Get pull requests matching query parameters.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            state: State of the PRs to retrieve ('open', 'closed', or 'all'). Defaults to 'open'.
            sort: Sort field ('created', 'updated', 'popularity', 'long-running'). Defaults to 'created'.
            direction: Sort direction ('asc' or 'desc'). Defaults to 'desc'.
            limit: Maximum number of pull requests to return. Defaults to 50.

        Returns:
            A JSON-formatted string containing a list of pull requests.

        """
        pulls = self.g.get_repo(repo_name).get_pulls(state=state, sort=sort, direction=direction)
        pr_list: list[dict[str, Any]] = []
        for pr in pulls:
            if len(pr_list) >= limit:
                break
            pr_list.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "user": pr.user.login,
                    "created_at": pr.created_at.isoformat(),
                    "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                    "state": pr.state,
                    "url": pr.html_url,
                },
            )
        return json.dumps(pr_list, indent=2)

    def get_pull_request_comments(
        self,
        repo_name: str,
        pr_number: int,
        include_issue_comments: bool = True,
    ) -> str:
        """Get all comments on a pull request.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            pr_number: The number of the pull request.
            include_issue_comments: Whether to include general PR comments. Defaults to True.

        Returns:
            A JSON-formatted string containing a list of pull request comments.

        """
        pr = self.g.get_repo(repo_name).get_pull(pr_number)
        comment_list: list[dict[str, Any]] = [
            {
                "id": comment.id,
                "body": comment.body,
                "user": comment.user.login,
                "created_at": comment.created_at.isoformat(),
                "updated_at": comment.updated_at.isoformat(),
                "path": comment.path,
                "position": comment.position,
                "commit_id": comment.commit_id,
                "url": comment.html_url,
                "type": "review_comment",
            }
            for comment in pr.get_comments()
        ]
        if include_issue_comments:
            comment_list.extend(
                {
                    "id": comment.id,
                    "body": comment.body,
                    "user": comment.user.login,
                    "created_at": comment.created_at.isoformat(),
                    "updated_at": comment.updated_at.isoformat(),
                    "url": comment.html_url,
                    "type": "issue_comment",
                }
                for comment in pr.get_issue_comments()
            )
        comment_list.sort(key=lambda item: item["created_at"], reverse=True)
        return json.dumps(comment_list, indent=2)

    def create_pull_request_comment(
        self,
        repo_name: str,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        position: int,
    ) -> str:
        """Create a comment on a specific line of a specific file in a pull request.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            pr_number: The number of the pull request.
            body: The text of the comment.
            commit_id: The SHA of the commit to comment on.
            path: The relative path to the file to comment on.
            position: The line index in the diff to comment on.

        Returns:
            A JSON-formatted string containing the created comment details.

        """
        repo = self.g.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        commit = repo.get_commit(commit_id)
        comment = pr.create_comment(body, commit, path, position)
        return json.dumps(
            {
                "id": comment.id,
                "body": comment.body,
                "user": comment.user.login,
                "created_at": comment.created_at.isoformat(),
                "path": comment.path,
                "position": comment.position,
                "commit_id": comment.commit_id,
                "url": comment.html_url,
            },
            indent=2,
        )

    def edit_pull_request_comment(self, repo_name: str, comment_id: int, body: str) -> str:
        """Edit an existing pull request comment.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            comment_id: The id of the comment to edit.
            body: The new text of the comment.

        Returns:
            A JSON-formatted string containing the updated comment details.

        """
        repo = self.g.get_repo(repo_name)
        target = next((comment for comment in repo.get_pulls_comments() if comment.id == comment_id), None)
        if target is None:
            return f"Could not find comment #{comment_id} in repository: {repo_name}"
        target.edit(body)
        return json.dumps(
            {
                "id": target.id,
                "body": target.body,
                "user": target.user.login,
                "updated_at": target.updated_at.isoformat(),
                "path": target.path,
                "position": target.position,
                "commit_id": target.commit_id,
                "url": target.html_url,
            },
            indent=2,
        )

    def get_pull_request_with_details(self, repo_name: str, pr_number: int) -> str:
        """Get comprehensive details of a pull request including comments, labels, and metadata.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            pr_number: The number of the pull request.

        Returns:
            A JSON-formatted string containing detailed pull request information.

        """
        pr = self.g.get_repo(repo_name).get_pull(pr_number)
        review_comments = [
            {
                "id": comment.id,
                "body": comment.body,
                "user": comment.user.login,
                "created_at": comment.created_at.isoformat(),
                "path": comment.path,
                "position": comment.position,
                "commit_id": comment.commit_id,
                "url": comment.html_url,
                "type": "review_comment",
            }
            for comment in pr.get_comments()
        ]
        issue_comments = [
            {
                "id": comment.id,
                "body": comment.body,
                "user": comment.user.login,
                "created_at": comment.created_at.isoformat(),
                "url": comment.html_url,
                "type": "issue_comment",
            }
            for comment in pr.get_issue_comments()
        ]
        commits = [
            {
                "sha": commit.sha,
                "message": commit.commit.message,
                "author": commit.commit.author.name if commit.commit.author else "Unknown",
                "date": commit.commit.author.date.isoformat() if commit.commit.author else None,
                "url": commit.html_url,
            }
            for commit in pr.get_commits()
        ]
        files_changed = [
            {
                "filename": file.filename,
                "status": file.status,
                "additions": file.additions,
                "deletions": file.deletions,
                "changes": file.changes,
                "patch": file.patch,
            }
            for file in pr.get_files()
        ]
        all_comments = sorted(review_comments + issue_comments, key=lambda item: item["created_at"], reverse=True)
        return json.dumps(
            {
                "number": pr.number,
                "title": pr.title,
                "user": pr.user.login,
                "state": pr.state,
                "created_at": pr.created_at.isoformat(),
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                "html_url": pr.html_url,
                "body": pr.body,
                "base": pr.base.ref,
                "head": pr.head.ref,
                "merged": pr.is_merged(),
                "mergeable": pr.mergeable,
                "additions": pr.additions,
                "deletions": pr.deletions,
                "changed_files": pr.changed_files,
                "labels": [label.name for label in pr.labels],
                "comments_count": {
                    "review_comments": len(review_comments),
                    "issue_comments": len(issue_comments),
                    "total": len(all_comments),
                },
                "comments": all_comments,
                "commits": commits,
                "files_changed": files_changed,
            },
            indent=2,
        )

    def get_repository_with_stats(self, repo_name: str) -> str:
        """Get comprehensive repository information including statistics.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').

        Returns:
            A JSON-formatted string containing detailed repository information and statistics.

        """
        repo = self.g.get_repo(repo_name)
        repo_info: dict[str, Any] = {
            "id": int(repo.id),
            "name": str(repo.name),
            "full_name": str(repo.full_name),
            "owner": str(repo.owner.login),
            "description": str(repo.description) if repo.description else None,
            "html_url": str(repo.html_url),
            "homepage": str(repo.homepage) if repo.homepage else None,
            "language": str(repo.language) if repo.language else None,
            "created_at": _safe_primitive(repo.created_at),
            "updated_at": _safe_primitive(repo.updated_at),
            "pushed_at": _safe_primitive(repo.pushed_at),
            "size": int(repo.size),
            "stargazers_count": int(repo.stargazers_count),
            "watchers_count": int(repo.watchers_count),
            "forks_count": int(repo.forks_count),
            "open_issues_count": int(repo.open_issues_count),
            "default_branch": str(repo.default_branch),
            "topics": [str(topic) for topic in repo.get_topics()],
            "license": str(repo.license.name) if repo.license and repo.license.name else None,
            "private": bool(repo.private),
            "archived": bool(repo.archived),
            "languages": _numeric_languages(repo.get_languages()),
            "actual_open_issues": _collect_actual_open_issues(repo),
            "open_pr_count": _collect_open_pr_count(repo),
            "recent_open_prs": _collect_recent_open_prs(repo),
            "pr_metrics": _collect_pr_metrics(repo),
            "contributors": _collect_contributors(repo),
        }
        return json.dumps(repo_info, indent=2)

    def create_pull_request(
        self,
        repo_name: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        maintainer_can_modify: bool = True,
    ) -> str:
        """Create a new pull request in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            title: The title of the pull request.
            body: The body text of the pull request.
            head: The name of the branch where your changes are implemented.
            base: The name of the branch you want the changes pulled into.
            draft: Whether the pull request is a draft. Defaults to False.
            maintainer_can_modify: Whether maintainers can modify the PR. Defaults to True.

        Returns:
            A JSON-formatted string containing the created pull request details.

        """
        pr = self.g.get_repo(repo_name).create_pull(
            title=title,
            body=body,
            head=head,
            base=base,
            draft=draft,
            maintainer_can_modify=maintainer_can_modify,
        )
        return json.dumps(
            {
                "number": pr.number,
                "title": pr.title,
                "body": pr.body,
                "user": pr.user.login,
                "state": pr.state,
                "created_at": pr.created_at.isoformat(),
                "html_url": pr.html_url,
                "base": pr.base.ref,
                "head": pr.head.ref,
                "mergeable": pr.mergeable,
            },
            indent=2,
        )

    def create_review_request(
        self,
        repo_name: str,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: list[str] | None = None,
    ) -> str:
        """Create a review request for a pull request.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            pr_number: The number of the pull request.
            reviewers: List of user logins that will be requested to review.
            team_reviewers: List of team slugs that will be requested to review. Defaults to None.

        Returns:
            A JSON-formatted string with the success message or error.

        """
        pr = self.g.get_repo(repo_name).get_pull(pr_number)
        pr.create_review_request(reviewers=reviewers, team_reviewers=team_reviewers or [])
        return json.dumps(
            {
                "message": f"Review request created for PR #{pr_number}",
                "requested_reviewers": reviewers,
                "requested_team_reviewers": team_reviewers or [],
            },
            indent=2,
        )

    def create_file(
        self,
        repo_name: str,
        path: str,
        content: str,
        message: str,
        branch: str | None = None,
    ) -> str:
        """Create a new file in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            path: The path to the file in the repository.
            content: The content of the file.
            message: The commit message.
            branch: The branch to commit to. Defaults to repository's default branch.

        Returns:
            A JSON-formatted string containing the file creation result.

        """
        repo = self.g.get_repo(repo_name)
        content_bytes = content.encode("utf-8")
        if branch is None:
            result = repo.create_file(path=path, message=message, content=content_bytes)
        else:
            result = repo.create_file(path=path, message=message, content=content_bytes, branch=branch)
        content_result = cast("_ContentWriteResult", result["content"])
        commit_result = cast("_CommitWriteResult", result["commit"])
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

    def get_file_content(self, repo_name: str, path: str, ref: str | None = None) -> str:
        """Get the content of a file in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            path: The path to the file in the repository.
            ref: The name of the commit/branch/tag. Defaults to the repository's default branch.

        Returns:
            A JSON-formatted string containing the file content and metadata.

        """
        repo = self.g.get_repo(repo_name)
        file_content = repo.get_contents(path, ref=ref) if ref is not None else repo.get_contents(path)
        if isinstance(file_content, list):
            return json.dumps({"error": f"{path} is a directory, not a file"})

        try:
            decoded_content = file_content.decoded_content.decode("utf-8")
        except Exception as exc:
            logger.debug("github_file_decode_failed", error_type=type(exc).__name__)
            decoded_content = "Binary file (content not displayed)"
        else:
            if _is_binary_text(decoded_content):
                decoded_content = "Binary file (content not displayed)"

        return json.dumps(
            {
                "name": file_content.name,
                "path": file_content.path,
                "sha": file_content.sha,
                "size": file_content.size,
                "type": file_content.type,
                "url": file_content.html_url,
                "content": decoded_content,
            },
            indent=2,
        )

    def update_file(
        self,
        repo_name: str,
        path: str,
        content: str,
        message: str,
        sha: str,
        branch: str | None = None,
    ) -> str:
        """Update an existing file in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            path: The path to the file in the repository.
            content: The new content of the file.
            message: The commit message.
            sha: The blob SHA of the file being replaced.
            branch: The branch to commit to. Defaults to the repository's default branch.

        Returns:
            A JSON-formatted string containing the file update result.

        """
        repo = self.g.get_repo(repo_name)
        if branch is None:
            result = repo.update_file(path=path, message=message, content=content.encode("utf-8"), sha=sha)
        else:
            result = repo.update_file(
                path=path,
                message=message,
                content=content.encode("utf-8"),
                sha=sha,
                branch=branch,
            )
        content_result = cast("_ContentWriteResult", result["content"])
        commit_result = cast("_CommitWriteResult", result["commit"])
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

    def delete_file(
        self,
        repo_name: str,
        path: str,
        message: str,
        sha: str,
        branch: str | None = None,
    ) -> str:
        """Delete a file from a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            path: The path to the file in the repository.
            message: The commit message.
            sha: The blob SHA of the file being deleted.
            branch: The branch to commit to. Defaults to the repository's default branch.

        Returns:
            A JSON-formatted string confirming deletion.

        """
        repo = self.g.get_repo(repo_name)
        if branch is None:
            result = repo.delete_file(path=path, message=message, sha=sha)
        else:
            result = repo.delete_file(path=path, message=message, sha=sha, branch=branch)
        commit_result = cast("_CommitWriteResult", result["commit"])
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

    def _get_directory_content(self, repo_name: str, path: str, ref: str | None = None) -> str:
        repo = self.g.get_repo(repo_name)
        contents = repo.get_contents(path, ref=ref) if ref is not None else repo.get_contents(path)
        if not isinstance(contents, list):
            return json.dumps({"error": f"{path} is a file, not a directory"})
        items = [
            {
                "name": content.name,
                "path": content.path,
                "type": content.type,
                "size": content.size,
                "sha": content.sha,
                "url": content.html_url,
                "download_url": content.download_url,
            }
            for content in contents
        ]
        items.sort(key=lambda item: (item["type"] != "dir", str(item["name"]).lower()))
        return json.dumps(items, indent=2)

    def get_directory_content(self, repo_name: str, path: str, ref: str | None = None) -> str:
        """Get the contents of a directory in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            path: The path to the directory in the repository. Use empty string for root.
            ref: The name of the commit/branch/tag. Defaults to repository's default branch.

        Returns:
            A JSON-formatted string containing a list of directory contents.

        """
        return self._get_directory_content(repo_name, path, ref=ref)

    def get_branch_content(self, repo_name: str, branch: str = "main") -> str:
        """Get the root directory content of a specific branch.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            branch: The branch name. Defaults to "main".

        Returns:
            A JSON-formatted string containing a list of branch contents.

        """
        return self._get_directory_content(repo_name, "", ref=branch)

    def create_branch(self, repo_name: str, branch_name: str, source_branch: str | None = None) -> str:
        """Create a new branch in a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            branch_name: The name of the new branch.
            source_branch: The source branch to create from. Defaults to repository's default branch.

        Returns:
            A JSON-formatted string containing information about the created branch.

        """
        repo = self.g.get_repo(repo_name)
        resolved_source = repo.default_branch if source_branch is None else source_branch
        sha = repo.get_git_ref(f"heads/{resolved_source}").object.sha
        new_branch = repo.create_git_ref(f"refs/heads/{branch_name}", sha)
        return json.dumps(
            {
                "name": branch_name,
                "sha": new_branch.object.sha,
                "url": f"{repo.html_url.rstrip('/')}/tree/{quote(branch_name, safe='/')}",
            },
            indent=2,
        )

    def set_default_branch(self, repo_name: str, branch_name: str) -> str:
        """Set the default branch for a repository.

        Args:
            repo_name: The full name of the repository (e.g., 'owner/repo').
            branch_name: The name of the branch to set as default.

        Returns:
            A JSON-formatted string with success message or error.

        """
        repo = self.g.get_repo(repo_name)
        branches = [branch.name for branch in repo.get_branches()]
        if branch_name not in branches:
            return json.dumps({"error": f"Branch '{branch_name}' does not exist"})
        repo.edit(default_branch=branch_name)
        return json.dumps(
            {
                "message": f"Default branch changed to {branch_name}",
                "repository": repo_name,
                "default_branch": branch_name,
            },
            indent=2,
        )

    def search_code(
        self,
        query: str,
        language: str | None = None,
        repo: str | None = None,
        user: str | None = None,
        path: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Search for code in GitHub repositories.

        Args:
            query: The search query.
            language: Filter by language. Defaults to None.
            repo: Filter by repository (e.g., 'owner/repo'). Defaults to None.
            user: Filter by user or organization. Defaults to None.
            path: Filter by file path. Defaults to None.
            filename: Filter by filename. Defaults to None.

        Returns:
            A JSON-formatted string containing the search results.

        """
        parts = [query]
        for key, value in (
            ("language", language),
            ("repo", repo),
            ("user", user),
            ("path", path),
            ("filename", filename),
        ):
            if value:
                parts.append(f"{key}:{value}")
        search_query = " ".join(parts)

        code_results = self.g.search_code(search_query)
        results: list[dict[str, Any]] = []
        for page_index in range(2):
            if len(results) >= 60:
                break
            page_items = code_results.get_page(page_index)
            if not page_items:
                break
            results.extend(
                {
                    "repository": code.repository.full_name,
                    "path": code.path,
                    "name": code.name,
                    "sha": code.sha,
                    "html_url": code.html_url,
                    "git_url": code.git_url,
                    "score": code.score,
                }
                for code in page_items
            )
        return json.dumps(
            {
                "query": search_query,
                "total_count": code_results.totalCount,
                "results_count": len(results),
                "results": results,
            },
            indent=2,
        )

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
        """Search for issues and pull requests on GitHub.

        Args:
            query: The search query.
            state: Filter by state ('open', 'closed'). Defaults to None.
            type_filter: Filter by type ('issue', 'pr'). Defaults to None.
            repo: Filter by repository (e.g., 'owner/repo'). Defaults to None.
            user: Filter by user or organization. Defaults to None.
            label: Filter by label. Defaults to None.
            sort: Sort results by ('created', 'updated', 'comments'). Defaults to "created".
            order: Sort order ('asc', 'desc'). Defaults to "desc".
            page: Page number for pagination. Defaults to 1.
            per_page: Number of results per page. Defaults to 30.

        Returns:
            A JSON-formatted string containing the search results.

        """
        parts = [unescape(query)]
        # GitHub search has no state:all qualifier and returns nothing for it.
        if state and state != "all":
            parts.append(f"state:{state}")
        if type_filter == "issue":
            parts.append("is:issue")
        elif type_filter == "pr":
            parts.append("is:pr")
        for key, value in (("repo", repo), ("user", user), ("label", label)):
            if value:
                parts.append(f"{key}:{value}")
        search_query = " ".join(parts)

        issue_results = self.g.search_issues(search_query, sort=sort, order=order)
        effective_per_page = min(per_page, 100)
        results: list[dict[str, Any]] = []
        try:
            page_items = issue_results.get_page(page - 1)
            results.extend(
                {
                    "number": issue.number,
                    "title": issue.title,
                    "repository": issue.repository.full_name,
                    "state": issue.state,
                    "created_at": issue.created_at.isoformat(),
                    "updated_at": issue.updated_at.isoformat(),
                    "html_url": issue.html_url,
                    "user": issue.user.login,
                    "is_pull_request": issue.pull_request is not None,
                    "comments": issue.comments,
                    "labels": [item.name for item in issue.labels],
                }
                for issue in page_items[:effective_per_page]
            )
        except IndexError:
            pass
        return json.dumps(
            {
                "query": search_query,
                "total_count": issue_results.totalCount,
                "page": page,
                "per_page": effective_per_page,
                "results_count": len(results),
                "results": results,
            },
            indent=2,
        )
