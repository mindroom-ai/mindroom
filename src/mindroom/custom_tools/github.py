"""GitHub tools backed by explicit or MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import logging
import threading
from functools import wraps
from html import unescape
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, get_args
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


def _format_content_write_result(
    content_result: _ContentWriteResult,
    commit_result: _CommitWriteResult,
    message: str,
) -> str:
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


_RepoReadAction = Literal[
    "list_repositories",
    "search_repositories",
    "get_repository",
    "get_repository_with_stats",
    "get_repository_languages",
    "get_repository_stars",
    "list_branches",
    "get_file_content",
    "get_directory_content",
    "get_branch_content",
    "search_code",
]
_IssuePrReadAction = Literal[
    "list_issues",
    "get_issue",
    "list_issue_comments",
    "get_pull_requests",
    "get_pull_request",
    "get_pull_request_changes",
    "get_pull_request_comments",
    "get_pull_request_with_details",
    "get_pull_request_count",
    "search_issues_and_prs",
]
_IssueWriteAction = Literal[
    "create_issue",
    "edit_issue",
    "comment_on_issue",
    "close_issue",
    "reopen_issue",
    "assign_issue",
    "label_issue",
]
_PrWriteAction = Literal[
    "create_pull_request",
    "create_pull_request_comment",
    "edit_pull_request_comment",
    "create_review_request",
]
_RepoWriteAction = Literal[
    "create_repository",
    "delete_repository",
    "create_branch",
    "set_default_branch",
    "create_file",
    "update_file",
    "delete_file",
]


_REPO_READ_ACTIONS = frozenset(get_args(_RepoReadAction))
_ISSUE_PR_READ_ACTIONS = frozenset(get_args(_IssuePrReadAction))
_ISSUE_WRITE_ACTIONS = frozenset(get_args(_IssueWriteAction))
_PR_WRITE_ACTIONS = frozenset(get_args(_PrWriteAction))
_REPO_WRITE_ACTIONS = frozenset(get_args(_RepoWriteAction))


def _missing_arg_error(field: str, action: str) -> str:
    return json.dumps({"error": f"{field} is required for {action}"})


def _unsupported_action_error(group: str, action: str) -> str:
    return json.dumps({"error": f"Unsupported GitHub {group} action: {action}"})


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
            self.github_read_repo,
            self.github_read_issues_prs,
            self.github_write_issues,
            self.github_write_pr,
            self.github_write_repo,
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

    def _search_repositories(self, query: str, sort: str, order: str, page: int, per_page: int) -> str:
        resolved_per_page = min(per_page, 100)
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
            for repo in repositories.get_page(page - 1)[:resolved_per_page]
        ]
        return json.dumps(repo_list, indent=2)

    def _get_repository(self, repo_name: str) -> str:
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

    def _get_repository_with_stats(self, repo_name: str) -> str:
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

    def _get_file_content(self, repo_name: str, path: str, ref: str | None) -> str:
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

    def _get_directory_content(self, repo_name: str, path: str, ref: str | None) -> str:
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

    def _search_code(
        self,
        query: str,
        *,
        language: str | None,
        repo: str | None,
        user: str | None,
        path: str | None,
        filename: str | None,
    ) -> str:
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

    def _dispatch_repo_read(self, action: _RepoReadAction, repo_name: str, path: str | None, ref: str | None) -> str:
        simple_handlers: dict[str, Callable[[str], str]] = {
            "get_repository": self._get_repository,
            "get_repository_with_stats": self._get_repository_with_stats,
            "get_repository_languages": lambda r: json.dumps(
                _numeric_languages(self.g.get_repo(r).get_languages()),
                indent=2,
            ),
            "get_repository_stars": lambda r: json.dumps({"stars": self.g.get_repo(r).stargazers_count}, indent=2),
            "list_branches": lambda r: json.dumps(
                [branch.name for branch in self.g.get_repo(r).get_branches()],
                indent=2,
            ),
        }
        if (handler := simple_handlers.get(action)) is not None:
            return handler(repo_name)
        if action == "get_file_content":
            return self._get_file_content(repo_name, path, ref) if path else _missing_arg_error("path", action)
        if action == "get_directory_content":
            return (
                self._get_directory_content(repo_name, path, ref)
                if path is not None
                else _missing_arg_error("path", action)
            )
        if action == "get_branch_content":
            return self._get_directory_content(repo_name, "", ref or "main")
        return json.dumps({"error": f"Unsupported GitHub repo read action: {action}"})

    def github_read_repo(
        self,
        action: _RepoReadAction,
        repo_name: str | None = None,
        query: str | None = None,
        path: str | None = None,
        ref: str | None = None,
        language: str | None = None,
        user: str | None = None,
        filename: str | None = None,
        sort: str = "stars",
        order: str = "desc",
        page: int = 1,
        per_page: int = 30,
    ) -> str:
        """Read GitHub repositories, branches, files, directories, and code search results.

        - `list_repositories`: list authenticated user's repository full names
        - `search_repositories`: search repos by `query` (`sort`, `order`, `page`, `per_page`)
        - `get_repository`: repository metadata for `repo_name` (`owner/repo`)
        - `get_repository_with_stats`: detailed stats, languages, PR metrics, and contributors for `repo_name`
        - `get_repository_languages`: language byte counts for `repo_name`
        - `get_repository_stars`: star count for `repo_name`
        - `list_branches`: branch names for `repo_name`
        - `get_file_content`: decoded file text and SHA for `repo_name` and `path` (optional `ref`)
        - `get_directory_content`: directory listing for `repo_name` and `path` (optional `ref`)
        - `get_branch_content`: root directory listing for `repo_name` at `ref` (default `main`)
        - `search_code`: search code by `query` (optional `repo_name`, `language`, `user`, `path`, `filename`)
        """
        if action not in _REPO_READ_ACTIONS:
            return _unsupported_action_error("repo read", action)
        if action == "list_repositories":
            return json.dumps([repo.full_name for repo in self.g.get_user().get_repos()], indent=2)
        if action == "search_repositories":
            return (
                self._search_repositories(query, sort, order, page, per_page)
                if query
                else _missing_arg_error("query", action)
            )
        if action == "search_code":
            return (
                self._search_code(query, language=language, repo=repo_name, user=user, path=path, filename=filename)
                if query
                else _missing_arg_error("query", action)
            )
        if not repo_name:
            return _missing_arg_error("repo_name", action)
        return self._dispatch_repo_read(action, repo_name, path, ref)

    def _list_issues(self, repo_name: str, state: str, page: int, per_page: int) -> str:
        all_issues = [issue for issue in self.g.get_repo(repo_name).get_issues(state=state) if not issue.pull_request]
        total_issues = len(all_issues)
        total_pages = (total_issues + per_page - 1) // per_page
        resolved_page = max(1, min(page, total_pages) if total_pages > 0 else 1)
        start = (resolved_page - 1) * per_page
        issue_list = [
            {
                "number": issue.number,
                "title": issue.title,
                "user": issue.user.login,
                "created_at": issue.created_at.isoformat(),
                "state": issue.state,
                "url": issue.html_url,
            }
            for issue in all_issues[start : start + per_page]
        ]
        meta = {
            "current_page": resolved_page,
            "per_page": per_page,
            "total_items": total_issues,
            "total_pages": total_pages,
        }
        return json.dumps({"data": issue_list, "meta": meta}, indent=2)

    def _get_issue(self, repo_name: str, issue_number: int) -> str:
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

    def _list_issue_comments(self, repo_name: str, issue_number: int) -> str:
        comments = self.g.get_repo(repo_name).get_issue(number=issue_number).get_comments()
        return json.dumps(
            [
                {
                    "id": comment.id,
                    "user": comment.user.login,
                    "body": comment.body,
                    "created_at": comment.created_at.isoformat(),
                    "url": comment.html_url,
                }
                for comment in comments
            ],
            indent=2,
        )

    def _get_pull_requests(self, repo_name: str, state: str, sort: str, direction: str, limit: int) -> str:
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

    def _get_pull_request(self, repo_name: str, pr_number: int) -> str:
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

    def _get_pull_request_changes(self, repo_name: str, pr_number: int) -> str:
        pr = self.g.get_repo(repo_name).get_pull(pr_number)
        return json.dumps(
            [
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
            ],
            indent=2,
        )

    def _get_pull_request_comments(self, repo_name: str, pr_number: int, include_issue_comments: bool) -> str:
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

    def _get_pull_request_with_details(self, repo_name: str, pr_number: int) -> str:
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

    def _get_pull_request_count(
        self,
        repo_name: str,
        *,
        state: str,
        author: str | None,
        base: str | None,
        head: str | None,
    ) -> str:
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

    def _search_issues_and_prs(
        self,
        query: str,
        *,
        state: str | None,
        type_filter: str | None,
        repo: str | None,
        user: str | None,
        label: str | None,
        sort: str,
        order: str,
        page: int,
        per_page: int,
    ) -> str:
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

    def _dispatch_numbered_issue_pr_read(
        self,
        action: _IssuePrReadAction,
        repo_name: str,
        number: int,
        include_issue_comments: bool,
    ) -> str | None:
        handlers: dict[str, Callable[[], str]] = {
            "get_issue": lambda: self._get_issue(repo_name, number),
            "list_issue_comments": lambda: self._list_issue_comments(repo_name, number),
            "get_pull_request": lambda: self._get_pull_request(repo_name, number),
            "get_pull_request_changes": lambda: self._get_pull_request_changes(repo_name, number),
            "get_pull_request_comments": lambda: self._get_pull_request_comments(
                repo_name,
                number,
                include_issue_comments,
            ),
            "get_pull_request_with_details": lambda: self._get_pull_request_with_details(repo_name, number),
        }
        handler = handlers.get(action)
        return None if handler is None else handler()

    def github_read_issues_prs(  # noqa: PLR0911
        self,
        action: _IssuePrReadAction,
        repo_name: str | None = None,
        number: int | None = None,
        query: str | None = None,
        state: str | None = None,
        type_filter: str | None = None,
        user: str | None = None,
        label: str | None = None,
        base: str | None = None,
        head: str | None = None,
        include_issue_comments: bool = True,
        sort: str = "created",
        order: str = "desc",
        page: int = 1,
        per_page: int | None = None,
    ) -> str:
        """Read and search GitHub issues and pull requests.

        - `list_issues`: issues in `repo_name` (`state` default `open`, `page`, `per_page` default 20)
        - `get_issue`: issue details for `repo_name` and `number`
        - `list_issue_comments`: comments on issue `number` in `repo_name`
        - `get_pull_requests`: PRs in `repo_name` (`state` default `open`, `sort`, `order`, `per_page` default 50)
        - `get_pull_request`: PR metadata for `repo_name` and `number`
        - `get_pull_request_changes`: changed files and patches for PR `number` in `repo_name`
        - `get_pull_request_comments`: comments on PR `number` in `repo_name` (`include_issue_comments`)
        - `get_pull_request_with_details`: full PR details, comments, commits, and files for `repo_name` and `number`
        - `get_pull_request_count`: count PRs in `repo_name` (`state` default `all`, optional `user` author, `base`, `head`)
        - `search_issues_and_prs`: search by `query` (optional `repo_name`, `state`, `type_filter` `issue`/`pr`, `user`, `label`, `sort`, `order`, `page`, `per_page` default 30)
        """
        if action not in _ISSUE_PR_READ_ACTIONS:
            return _unsupported_action_error("issue/PR read", action)
        if action == "search_issues_and_prs":
            if not query:
                return _missing_arg_error("query", action)
            return self._search_issues_and_prs(
                query,
                state=state,
                type_filter=type_filter,
                repo=repo_name,
                user=user,
                label=label,
                sort=sort,
                order=order,
                page=page,
                per_page=30 if per_page is None else per_page,
            )
        if not repo_name:
            return _missing_arg_error("repo_name", action)
        if action == "list_issues":
            return self._list_issues(repo_name, state or "open", page, 20 if per_page is None else per_page)
        if action == "get_pull_requests":
            return self._get_pull_requests(
                repo_name,
                state or "open",
                sort,
                order,
                50 if per_page is None else per_page,
            )
        if action == "get_pull_request_count":
            return self._get_pull_request_count(repo_name, state=state or "all", author=user, base=base, head=head)
        if number is None:
            return _missing_arg_error("number", action)
        if (
            result := self._dispatch_numbered_issue_pr_read(action, repo_name, number, include_issue_comments)
        ) is not None:
            return result
        return json.dumps({"error": f"Unsupported GitHub issue/PR read action: {action}"})

    def _create_issue(self, repo_name: str, title: str, body: str | None) -> str:
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

    def _edit_issue(self, repo_name: str, issue_number: int, title: str | None, body: str | None) -> str:
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

    def _comment_on_issue(self, repo_name: str, issue_number: int, body: str) -> str:
        comment = self.g.get_repo(repo_name).get_issue(number=issue_number).create_comment(body=body)
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

    def _mutate_existing_issue(
        self,
        action: _IssueWriteAction,
        repo_name: str,
        issue_number: int,
        *,
        title: str | None,
        body: str | None,
        assignees: list[str] | None,
        labels: list[str] | None,
    ) -> str:
        if action == "edit_issue":
            return self._edit_issue(repo_name, issue_number, title, body)
        if action == "comment_on_issue":
            return (
                self._comment_on_issue(repo_name, issue_number, body)
                if body is not None
                else _missing_arg_error("body", action)
            )
        if action == "assign_issue" and assignees is None:
            return _missing_arg_error("assignees", action)
        if action == "label_issue" and labels is None:
            return _missing_arg_error("labels", action)
        issue = self.g.get_repo(repo_name).get_issue(number=issue_number)
        if action in ("close_issue", "reopen_issue"):
            state = "closed" if action == "close_issue" else "open"
            issue.edit(state=state)
            message = f"Issue #{issue_number} {'closed' if state == 'closed' else 'reopened'}."
        elif action == "assign_issue":
            assert assignees is not None
            issue.edit(assignees=cast("list[Any]", assignees))
            message = f"Issue #{issue_number} assigned to {assignees}."
        elif action == "label_issue":
            assert labels is not None
            issue.edit(labels=labels)
            message = f"Labels {labels} added to issue #{issue_number}."
        else:
            return json.dumps({"error": f"Unsupported GitHub issue write action: {action}"})
        return json.dumps({"message": message}, indent=2)

    def github_write_issues(
        self,
        action: _IssueWriteAction,
        repo_name: str,
        issue_number: int | None = None,
        title: str | None = None,
        body: str | None = None,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
    ) -> str:
        """Create, edit, comment on, close, reopen, assign, or label GitHub issues.

        - `create_issue`: create issue in `repo_name` (`title`, optional `body`)
        - `edit_issue`: update `title` and/or `body` on `issue_number`
        - `comment_on_issue`: post `body` comment on `issue_number`
        - `close_issue`: close `issue_number`
        - `reopen_issue`: reopen `issue_number`
        - `assign_issue`: assign `assignees` to `issue_number`
        - `label_issue`: set `labels` on `issue_number`
        """
        if action not in _ISSUE_WRITE_ACTIONS:
            return _unsupported_action_error("issue write", action)
        if action == "create_issue":
            return self._create_issue(repo_name, title, body) if title else _missing_arg_error("title", action)
        if issue_number is None:
            return _missing_arg_error("issue_number", action)
        return self._mutate_existing_issue(
            action,
            repo_name,
            issue_number,
            title=title,
            body=body,
            assignees=assignees,
            labels=labels,
        )

    def _create_pull_request(
        self,
        repo_name: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
        maintainer_can_modify: bool,
    ) -> str:
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

    def _create_pull_request_comment(
        self,
        repo_name: str,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        position: int,
    ) -> str:
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

    def _edit_pull_request_comment(self, repo_name: str, comment_id: int, body: str) -> str:
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

    def _create_review_request(
        self,
        repo_name: str,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: list[str] | None,
    ) -> str:
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

    def github_write_pr(  # noqa: PLR0911
        self,
        action: _PrWriteAction,
        repo_name: str,
        pr_number: int | None = None,
        title: str | None = None,
        body: str | None = None,
        head: str | None = None,
        base: str | None = None,
        draft: bool = False,
        maintainer_can_modify: bool = True,
        commit_id: str | None = None,
        path: str | None = None,
        position: int | None = None,
        comment_id: int | None = None,
        reviewers: list[str] | None = None,
        team_reviewers: list[str] | None = None,
    ) -> str:
        """Create pull requests, review comments, and review requests on GitHub.

        - `create_pull_request`: open PR in `repo_name` (`title`, `body`, `head`, `base`, optional `draft`, `maintainer_can_modify`)
        - `create_pull_request_comment`: post diff review comment on `pr_number` (`body`, `commit_id`, `path`, `position`)
        - `edit_pull_request_comment`: update `comment_id` in `repo_name` with `body`
        - `create_review_request`: request `reviewers` (and optional `team_reviewers`) on `pr_number`
        """
        if action not in _PR_WRITE_ACTIONS:
            return _unsupported_action_error("PR write", action)
        if action == "create_pull_request":
            if not title or not head or not base:
                return _missing_arg_error("title, head, and base", action)
            return self._create_pull_request(repo_name, title, body or "", head, base, draft, maintainer_can_modify)
        if action == "create_pull_request_comment":
            if pr_number is None or body is None or not commit_id or not path or position is None:
                return _missing_arg_error("pr_number, body, commit_id, path, and position", action)
            return self._create_pull_request_comment(repo_name, pr_number, body, commit_id, path, position)
        if action == "edit_pull_request_comment":
            if comment_id is None or body is None:
                return _missing_arg_error("comment_id and body", action)
            return self._edit_pull_request_comment(repo_name, comment_id, body)
        if action == "create_review_request":
            if pr_number is None or reviewers is None:
                return _missing_arg_error("pr_number and reviewers", action)
            return self._create_review_request(repo_name, pr_number, reviewers, team_reviewers)
        return json.dumps({"error": f"Unsupported GitHub PR write action: {action}"})

    def _create_repository(
        self,
        name: str,
        private: bool,
        description: str | None,
        auto_init: bool,
        organization: str | None,
    ) -> str:
        owner: Any = self.g.get_organization(organization) if organization else self.g.get_user()
        repo = owner.create_repo(
            name=name,
            private=private,
            description=description if description is not None else "",
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

    def _create_branch(self, repo_name: str, branch: str, source_branch: str | None) -> str:
        repo = self.g.get_repo(repo_name)
        resolved_source = repo.default_branch if source_branch is None else source_branch
        sha = repo.get_git_ref(f"heads/{resolved_source}").object.sha
        new_branch = repo.create_git_ref(f"refs/heads/{branch}", sha)
        return json.dumps(
            {
                "name": branch,
                "sha": new_branch.object.sha,
                "url": f"{repo.html_url.rstrip('/')}/tree/{quote(branch, safe='/')}",
            },
            indent=2,
        )

    def _set_default_branch(self, repo_name: str, branch: str) -> str:
        repo = self.g.get_repo(repo_name)
        if branch not in [item.name for item in repo.get_branches()]:
            return json.dumps({"error": f"Branch '{branch}' does not exist"})
        repo.edit(default_branch=branch)
        return json.dumps(
            {
                "message": f"Default branch changed to {branch}",
                "repository": repo_name,
                "default_branch": branch,
            },
            indent=2,
        )

    def _write_repo_file(
        self,
        action: Literal["create_file", "update_file", "delete_file"],
        repo_name: str,
        path: str,
        message: str,
        *,
        content: str | None,
        sha: str | None,
        branch: str | None,
    ) -> str:
        repo = self.g.get_repo(repo_name)
        if action == "create_file":
            if content is None:
                return _missing_arg_error("content", action)
            content_bytes = content.encode("utf-8")
            result = (
                repo.create_file(path=path, message=message, content=content_bytes)
                if branch is None
                else repo.create_file(path=path, message=message, content=content_bytes, branch=branch)
            )
            return _format_content_write_result(
                cast("_ContentWriteResult", result["content"]),
                cast("_CommitWriteResult", result["commit"]),
                message,
            )
        if not sha:
            return _missing_arg_error("sha", action)
        if action == "update_file":
            if content is None:
                return _missing_arg_error("content", action)
            content_bytes = content.encode("utf-8")
            result = (
                repo.update_file(path=path, message=message, content=content_bytes, sha=sha)
                if branch is None
                else repo.update_file(path=path, message=message, content=content_bytes, sha=sha, branch=branch)
            )
            return _format_content_write_result(
                cast("_ContentWriteResult", result["content"]),
                cast("_CommitWriteResult", result["commit"]),
                message,
            )
        result = (
            repo.delete_file(path=path, message=message, sha=sha)
            if branch is None
            else repo.delete_file(path=path, message=message, sha=sha, branch=branch)
        )
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

    def github_write_repo(  # noqa: PLR0911
        self,
        action: _RepoWriteAction,
        repo_name: str | None = None,
        name: str | None = None,
        branch: str | None = None,
        source_branch: str | None = None,
        path: str | None = None,
        content: str | None = None,
        message: str | None = None,
        sha: str | None = None,
        private: bool = False,
        description: str | None = None,
        auto_init: bool = False,
        organization: str | None = None,
    ) -> str:
        """Create/delete repositories, manage branches, and create/update/delete repository files.

        - `create_repository`: create repo `name` (optional `private`, `description`, `auto_init`, `organization`)
        - `delete_repository`: permanently delete `repo_name`
        - `create_branch`: create `branch` in `repo_name` (optional `source_branch`, defaults to default branch)
        - `set_default_branch`: set `repo_name` default branch to `branch`
        - `create_file`: create `path` in `repo_name` (`content`, `message`, optional `branch`)
        - `update_file`: update `path` in `repo_name` (`content`, `message`, `sha`, optional `branch`)
        - `delete_file`: delete `path` in `repo_name` (`message`, `sha`, optional `branch`)
        """
        if action not in _REPO_WRITE_ACTIONS:
            return _unsupported_action_error("repo write", action)
        if action == "create_repository":
            return (
                self._create_repository(name, private, description, auto_init, organization)
                if name
                else _missing_arg_error("name", action)
            )
        if not repo_name:
            return _missing_arg_error("repo_name", action)
        if action == "delete_repository":
            self.g.get_repo(repo_name).delete()
            return json.dumps({"message": f"Repository {repo_name} deleted successfully"}, indent=2)
        if action in ("create_branch", "set_default_branch"):
            if not branch:
                return _missing_arg_error("branch", action)
            return (
                self._create_branch(repo_name, branch, source_branch)
                if action == "create_branch"
                else self._set_default_branch(repo_name, branch)
            )
        if action in ("create_file", "update_file", "delete_file"):
            if not path or not message:
                return _missing_arg_error("path and message", action)
            return self._write_repo_file(action, repo_name, path, message, content=content, sha=sha, branch=branch)
        return json.dumps({"error": f"Unsupported GitHub repo write action: {action}"})
