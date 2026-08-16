"""Tests for the requester-scoped GitHub OAuth toolkit."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Never
from unittest.mock import patch

import pytest
from agno.tools import github as agno_github_module
from github import BadCredentialsException, Github, GithubException

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import (
    CredentialsManager,
    get_runtime_credentials_manager,
    save_scoped_credentials,
)
from mindroom.custom_tools.github import GithubTools
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext, load_oauth_credentials
from mindroom.oauth.credential_store import oauth_credential_transaction
from mindroom.oauth.providers import OAuthProviderError, OAuthRefreshRejectedError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target, tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope

DEFAULT_REFRESH_TOKEN = "github-refresh"  # noqa: S105
MANUAL_ACCESS_TOKEN = "manual-access"  # noqa: S105
OLD_REFRESH_TOKEN = "old-refresh"  # noqa: S105
ROTATED_REFRESH_TOKEN = "rotated-refresh"  # noqa: S105
ENV_ACCESS_TOKEN = "environment-access"  # noqa: S105


def _publish_oauth_credentials(
    context: OAuthCredentialContext,
    credentials: dict[str, object],
) -> None:
    """Publish test credentials through the SQLite transaction owner."""

    async def publish() -> None:
        async with oauth_credential_transaction(context) as transaction:
            transaction.publish(credentials, advance_connection_generation=True)
            await transaction.commit()

    asyncio.run(publish())


@dataclass(frozen=True)
class _FakeRepo:
    full_name: str


class _FakeUser:
    def get_repos(self) -> list[_FakeRepo]:
        return [_FakeRepo("example/project")]


class _FakeGithub:
    def get_user(self) -> _FakeUser:
        return _FakeUser()


@dataclass(frozen=True)
class _TokenGithub:
    token: str


class _FakeIssueSearchResults:
    totalCount = 0  # noqa: N815

    def get_page(self, page: int) -> list[object]:
        assert page == 0
        return []


class _SearchGithub:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search_issues(self, query: str, *, sort: str, order: str) -> _FakeIssueSearchResults:
        assert sort == "created"
        assert order == "desc"
        self.queries.append(query)
        return _FakeIssueSearchResults()


class _FakePaginatedItems:
    totalCount = 0  # noqa: N815

    def __iter__(self) -> Iterator[object]:
        return iter(())


@dataclass(frozen=True)
class _FakeRepoOwner:
    login: str


class _FakeRepositoryStats:
    id = 1
    name = "project"
    full_name = "example/project"
    owner = _FakeRepoOwner("example")
    description = "Example repository"
    html_url = "https://github.example.test/example/project"
    homepage = None
    language = "Python"
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at = datetime(2026, 1, 2, tzinfo=UTC)
    pushed_at = datetime(2026, 1, 3, tzinfo=UTC)
    size = 123
    stargazers_count = 4
    watchers_count = 5
    forks_count = 6
    open_issues_count = 7
    default_branch = "main"
    license = None
    private = False
    archived = False

    def get_topics(self) -> list[str]:
        return ["example"]

    def get_languages(self) -> dict[str, int]:
        return {"Python": 123}

    def get_issues(self, *, state: str) -> list[object]:
        assert state == "open"
        return []

    def get_pulls(
        self,
        *,
        state: str,
        sort: str | None = None,
        direction: str | None = None,
    ) -> _FakePaginatedItems:
        assert state in {"all", "open"}
        assert (sort, direction) in {(None, None), ("created", "desc")}
        return _FakePaginatedItems()

    def get_contributors(self) -> list[object]:
        return []


class _StatsGithub:
    def get_repo(self, repo_name: str) -> _FakeRepositoryStats:
        assert repo_name == "example/project"
        return _FakeRepositoryStats()


@dataclass(frozen=True)
class _FakeContentWriteResult:
    path: str = "notes.txt"
    sha: str = "content-sha"
    html_url: str = "https://github.example.test/example/project/blob/main/notes.txt"


@dataclass(frozen=True)
class _FakeCommitWriteResult:
    sha: str = "commit-sha"
    html_url: str = "https://github.example.test/example/project/commit/commit-sha"
    commit: None = None


class _FakeWriteRepository:
    def __init__(self) -> None:
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def update_file(self, **kwargs: object) -> dict[str, object]:
        self.update_calls.append(kwargs)
        return {
            "content": _FakeContentWriteResult(),
            "commit": _FakeCommitWriteResult(),
        }

    def delete_file(self, **kwargs: object) -> dict[str, object]:
        self.delete_calls.append(kwargs)
        return {
            "content": None,
            "commit": _FakeCommitWriteResult(),
        }


class _WriteGithub:
    def __init__(self) -> None:
        self.repo = _FakeWriteRepository()

    def get_repo(self, repo_name: str) -> _FakeWriteRepository:
        assert repo_name == "example/project"
        return self.repo


class _FakeEditableIssue:
    def __init__(self) -> None:
        self.edit_calls: list[dict[str, object]] = []

    def edit(self, **kwargs: object) -> None:
        self.edit_calls.append(kwargs)


@dataclass(frozen=True)
class _FakePullUser:
    login: str


@dataclass(frozen=True)
class _FakePull:
    user: _FakePullUser
    state: str


class _FakePullsWithoutTotal:
    totalCount = None  # noqa: N815

    def __init__(self) -> None:
        self.items = [
            _FakePull(user=_FakePullUser("alice"), state="open"),
            _FakePull(user=_FakePullUser("bob"), state="open"),
        ]

    def __iter__(self) -> Iterator[_FakePull]:
        return iter(self.items)


class _FakeIssueAndPullRepository:
    def __init__(self) -> None:
        self.issue = _FakeEditableIssue()
        self.pull_calls: list[dict[str, object]] = []

    def get_issue(self, *, number: int) -> _FakeEditableIssue:
        assert number == 7
        return self.issue

    def get_pulls(self, **kwargs: object) -> _FakePullsWithoutTotal:
        self.pull_calls.append(kwargs)
        return _FakePullsWithoutTotal()


class _IssueAndPullGithub:
    def __init__(self) -> None:
        self.repo = _FakeIssueAndPullRepository()

    def get_repo(self, repo_name: str) -> _FakeIssueAndPullRepository:
        assert repo_name == "example/project"
        return self.repo


class _RevokedTokenGithub:
    def get_user(self) -> _FakeUser:
        raise BadCredentialsException(401, {"message": "Bad credentials"})


class _ProviderControlledFailureGithub:
    def __init__(self, status_code: int, sentinel: str) -> None:
        self.status_code = status_code
        self.sentinel = sentinel

    def _raise_provider_error(self) -> Never:
        raise GithubException(self.status_code, {"message": self.sentinel})

    def get_user(self) -> _FakeUser:
        self._raise_provider_error()

    def get_repo(self, _repo_name: str) -> _ProviderControlledFailureGithub:
        return self

    def update_file(self, **_kwargs: object) -> dict[str, object]:
        self._raise_provider_error()

    def delete_file(self, **_kwargs: object) -> dict[str, object]:
        self._raise_provider_error()

    def get_issue(self, *, number: int) -> _FakeEditableIssue:
        _ = number
        self._raise_provider_error()

    def get_pulls(self, **_kwargs: object) -> _FakePullsWithoutTotal:
        self._raise_provider_error()


class _CapturingLogger:
    def __init__(self) -> None:
        self.warning_calls: list[tuple[str, dict[str, object]]] = []
        self.exception_calls: list[tuple[str, dict[str, object], str]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warning_calls.append((event, kwargs))

    def exception(self, event: str, **kwargs: object) -> None:
        self.exception_calls.append((event, kwargs, repr(sys.exception())))


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            **(extra_env or {}),
        },
    )


def _worker_target_for_scope(
    requester_id: str,
    worker_scope: WorkerScope | None,
) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
    )
    return resolve_worker_target(worker_scope, "code", execution_identity=identity)


def _worker_target(requester_id: str) -> ResolvedWorkerTarget:
    return _worker_target_for_scope(requester_id, "user_agent")


def _oauth_target(requester_id: str) -> ResolvedWorkerTarget:
    return _worker_target_for_scope(requester_id, "user")


def _tool_class() -> type[Any]:
    return GithubTools


def _save_client_config(runtime_paths: RuntimePaths) -> CredentialsManager:
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "github_oauth_client",
        {
            "client_id": "github-client-id",
            "client_secret": "github-client-secret",
        },
    )
    return manager


def _oauth_credentials(
    token: str,
    *,
    refresh_token: str = DEFAULT_REFRESH_TOKEN,
    expires_at: float = 4_102_444_800.0,
) -> dict[str, object]:
    return {
        "token": token,
        "refresh_token": refresh_token,
        "client_id": "github-client-id",
        "scopes": [],
        "expires_at": expires_at,
        "_source": "oauth",
        "_oauth_provider": "github",
    }


def _build_tool(
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget,
    *,
    access_token: str | None = None,
    base_url: str | None = None,
) -> Any:  # noqa: ANN401
    return _tool_class()(
        access_token=access_token,
        base_url=base_url,
        runtime_paths=runtime_paths,
        credentials_manager=manager,
        worker_target=worker_target,
    )


def test_missing_credentials_return_requester_bound_connection_links(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    alice = _build_tool(runtime_paths, manager, _worker_target("@alice:example.test"))
    bob = _build_tool(runtime_paths, manager, _worker_target("@bob:example.test"))

    alice_result = json.loads(alice.list_repositories())
    bob_result = json.loads(bob.list_repositories())

    assert alice_result["oauth_connection_required"] is True
    assert alice_result["provider"] == "github"
    assert "/api/oauth/github/authorize?connect_token=" in alice_result["connect_url"]
    assert bob_result["connect_url"] != alice_result["connect_url"]
    assert "@alice:example.test" not in json.dumps(alice_result)
    assert "@bob:example.test" not in json.dumps(bob_result)


def test_requesters_cannot_use_each_others_github_oauth_credentials(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    alice_target = _worker_target("@alice:example.test")
    bob_target = _worker_target("@bob:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("alice-access"),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    alice = _build_tool(runtime_paths, manager, alice_target)
    alice.g = _FakeGithub()
    bob = _build_tool(runtime_paths, manager, bob_target)

    assert json.loads(alice.list_repositories()) == ["example/project"]
    assert json.loads(bob.list_repositories())["oauth_connection_required"] is True


def test_github_workers_keep_token_and_client_ownership_thread_local(tmp_path: Path) -> None:
    """An old call cannot overwrite a newer worker's authoritative token client."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    oauth_target = _oauth_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("account-a-token"),
        credentials_manager=manager,
        worker_target=oauth_target,
    )
    tool = _build_tool(runtime_paths, manager, target)
    tool.authenticate = lambda: _TokenGithub(tool.access_token)
    old_ready = threading.Event()
    release_old = threading.Event()

    def old_call() -> tuple[str, str]:
        tool._ensure_authenticated()
        old_ready.set()
        assert release_old.wait(timeout=5)
        return tool.access_token, tool.g.token

    def current_state() -> tuple[str, str]:
        tool._ensure_authenticated()
        return tool.access_token, tool.g.token

    with (
        ThreadPoolExecutor(max_workers=1) as old_worker,
        ThreadPoolExecutor(max_workers=1) as new_worker,
    ):
        old_future = old_worker.submit(old_call)
        assert old_ready.wait(timeout=5)
        credential_context = tool._oauth_credential_context()
        _publish_oauth_credentials(credential_context, _oauth_credentials("account-b-token"))
        new_state = new_worker.submit(current_state).result(timeout=5)
        release_old.set()
        old_state = old_future.result(timeout=5)
        retained_new_state = new_worker.submit(current_state).result(timeout=5)

    assert old_state == ("account-a-token", "account-a-token")
    assert new_state == ("account-b-token", "account-b-token")
    assert retained_new_state == new_state


def test_active_requester_overrides_tool_construction_identity(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    alice_target = _worker_target("@alice:example.test")
    bob_target = _worker_target("@bob:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("alice-access"),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    tool = _build_tool(runtime_paths, manager, alice_target)
    tool.g = _FakeGithub()

    with tool_execution_identity(bob_target.execution_identity):
        result = json.loads(tool.list_repositories())

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "github"


@pytest.mark.parametrize("worker_scope", [None, "shared", "user_agent"])
def test_github_oauth_is_requester_scoped_when_agent_runtime_is_not(
    tmp_path: Path,
    worker_scope: WorkerScope | None,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    alice_target = _worker_target_for_scope("@alice:example.test", worker_scope)
    bob_target = _worker_target_for_scope("@bob:example.test", worker_scope)
    alice_oauth_target = _oauth_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("alice-access"),
        credentials_manager=manager,
        worker_target=alice_oauth_target,
    )
    alice = _build_tool(runtime_paths, manager, alice_target)
    alice.g = _FakeGithub()
    bob = _build_tool(runtime_paths, manager, bob_target)
    bob.g = _FakeGithub()

    assert json.loads(alice.list_repositories()) == ["example/project"]
    assert json.loads(bob.list_repositories())["oauth_connection_required"] is True


def test_unscoped_agent_connection_links_are_requester_bound(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    alice = _build_tool(
        runtime_paths,
        manager,
        _worker_target_for_scope("@alice:example.test", None),
    )
    bob = _build_tool(
        runtime_paths,
        manager,
        _worker_target_for_scope("@bob:example.test", None),
    )

    alice_result = json.loads(alice.list_repositories())
    bob_result = json.loads(bob.list_repositories())

    assert "/api/oauth/github/authorize?connect_token=" in alice_result["connect_url"]
    assert bob_result["connect_url"] != alice_result["connect_url"]


def test_explicit_access_token_takes_precedence_over_scoped_oauth(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("oauth-access", expires_at=1.0),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    tool = _build_tool(runtime_paths, manager, target, access_token=MANUAL_ACCESS_TOKEN)
    tool.g = _FakeGithub()

    assert json.loads(tool.list_repositories()) == ["example/project"]
    assert tool.access_token == MANUAL_ACCESS_TOKEN


def test_environment_access_token_remains_an_explicit_fallback(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, {"GITHUB_ACCESS_TOKEN": ENV_ACCESS_TOKEN})
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(runtime_paths, manager, _worker_target("@alice:example.test"))
    tool.g = _FakeGithub()

    assert json.loads(tool.list_repositories()) == ["example/project"]
    assert tool.access_token == ENV_ACCESS_TOKEN


@pytest.mark.parametrize(
    ("access_token", "extra_env"),
    [
        ("   ", None),
        (None, {"GITHUB_ACCESS_TOKEN": "   "}),
    ],
)
def test_whitespace_explicit_tokens_fall_back_to_scoped_oauth(
    tmp_path: Path,
    access_token: str | None,
    extra_env: dict[str, str] | None,
) -> None:
    runtime_paths = _runtime_paths(tmp_path, extra_env)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("oauth-access"),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )

    tool = _build_tool(runtime_paths, manager, target, access_token=access_token)
    tool.g = _FakeGithub()

    assert json.loads(tool.list_repositories()) == ["example/project"]
    assert tool.access_token == "oauth-access"  # noqa: S105


def test_whitespace_stored_oauth_token_requires_connection(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("   ", refresh_token=""),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )

    result = json.loads(_build_tool(runtime_paths, manager, target).list_repositories())

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "github"


def test_base_url_is_forwarded_to_pygithub(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool_class = _tool_class()
    captured: dict[str, object] = {}

    def github_factory(**kwargs: object) -> _FakeGithub:
        captured.update(kwargs)
        return _FakeGithub()

    with patch("mindroom.custom_tools.github.Github", side_effect=github_factory):
        tool = tool_class(
            access_token=MANUAL_ACCESS_TOKEN,
            base_url="https://github.example.test/api/v3",
            runtime_paths=runtime_paths,
            credentials_manager=manager,
            worker_target=_worker_target("@alice:example.test"),
        )

    assert json.loads(tool.list_repositories()) == ["example/project"]
    assert captured["base_url"] == "https://github.example.test/api/v3"


def test_pygithub_language_payload_remains_numeric_for_agno_stats() -> None:
    client = Github(lazy=True)
    try:
        repo = client.get_repo("example/project", lazy=True)
        with patch.object(
            repo._requester,
            "requestJson",
            return_value=(200, {}, '{"Python": 123}'),
        ):
            assert repo.get_languages() == {"Python": 123}
    finally:
        client.close()


def test_repository_stats_consumes_numeric_language_payload(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    tool.g = _StatsGithub()

    result = json.loads(tool.get_repository_with_stats("example/project"))

    assert result["languages"] == {"Python": 123}
    assert result["stargazers_count"] == 4
    assert result["open_pr_count"] == 0
    assert result["pr_metrics"] == {
        "total_prs": 0,
        "merged_prs": 0,
        "acceptance_rate": 0,
        "avg_time_to_merge": None,
    }


def test_issue_search_decodes_html_escaped_comparison_operators(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    github = _SearchGithub()
    tool.g = github

    result = json.loads(tool.search_issues_and_prs("created:&gt;=2026-08-07"))

    assert github.queries == ["created:>=2026-08-07"]
    assert result["query"] == "created:>=2026-08-07"


def test_update_file_reports_success_without_refetching_commit_details(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    github = _WriteGithub()
    tool.g = github

    result = json.loads(
        tool.update_file(
            repo_name="example/project",
            path="notes.txt",
            content="updated notes",
            message="Update notes",
            sha="old-content-sha",
            branch="main",
        ),
    )

    assert github.repo.update_calls == [
        {
            "path": "notes.txt",
            "message": "Update notes",
            "content": b"updated notes",
            "sha": "old-content-sha",
            "branch": "main",
        },
    ]
    assert result == {
        "path": "notes.txt",
        "sha": "content-sha",
        "url": "https://github.example.test/example/project/blob/main/notes.txt",
        "commit": {
            "sha": "commit-sha",
            "message": "Update notes",
            "url": "https://github.example.test/example/project/commit/commit-sha",
        },
    }


def test_delete_file_reports_success_without_refetching_commit_details(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    github = _WriteGithub()
    tool.g = github

    result = json.loads(
        tool.delete_file(
            repo_name="example/project",
            path="notes.txt",
            message="Delete notes",
            sha="content-sha",
            branch="main",
        ),
    )

    assert github.repo.delete_calls == [
        {
            "path": "notes.txt",
            "message": "Delete notes",
            "sha": "content-sha",
            "branch": "main",
        },
    ]
    assert result == {
        "message": "File notes.txt deleted successfully",
        "commit": {
            "sha": "commit-sha",
            "message": "Delete notes",
            "url": "https://github.example.test/example/project/commit/commit-sha",
        },
    }


def test_edit_issue_omits_unspecified_fields(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    github = _IssueAndPullGithub()
    tool.g = github

    result = json.loads(
        tool.edit_issue(
            repo_name="example/project",
            issue_number=7,
            title="Updated title",
        ),
    )

    assert github.repo.issue.edit_calls == [{"title": "Updated title"}]
    assert result == {"message": "Issue #7 updated."}


def test_edit_issue_rejects_empty_update(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    github = _IssueAndPullGithub()
    tool.g = github

    result = json.loads(tool.edit_issue(repo_name="example/project", issue_number=7))

    assert result == {"error": "Provide a title or body to update issue #7."}
    assert github.repo.issue.edit_calls == []


def test_pull_request_count_falls_back_when_total_count_is_missing(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )
    github = _IssueAndPullGithub()
    tool.g = github

    result = json.loads(tool.get_pull_request_count("example/project", state="open"))

    assert result == {"count": 2}
    assert github.repo.pull_calls == [{"state": "open"}]


def test_expired_oauth_credentials_refresh_and_persist_rotation(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool_class = _tool_class()
    target = _worker_target("@alice:example.test")
    oauth_target = _oauth_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("old-access", refresh_token=OLD_REFRESH_TOKEN, expires_at=1.0),
        credentials_manager=manager,
        worker_target=oauth_target,
    )
    refreshed = _oauth_credentials("rotated-access", refresh_token=ROTATED_REFRESH_TOKEN)

    def refresh_credentials(*_args: object, **_kwargs: object) -> dict[str, object]:
        _publish_oauth_credentials(tool._oauth_credential_context(), refreshed)
        return refreshed

    with (
        patch("mindroom.custom_tools.github.refresh_oauth_credentials_blocking", side_effect=refresh_credentials),
        patch("mindroom.custom_tools.github.Github", return_value=_FakeGithub()),
    ):
        tool = tool_class(
            runtime_paths=runtime_paths,
            credentials_manager=manager,
            worker_target=target,
        )
        result = json.loads(tool.list_repositories())

    stored = load_oauth_credentials(tool._oauth_credential_context())
    assert result == ["example/project"]
    assert stored is not None
    assert stored["token"] == "rotated-access"  # noqa: S105
    assert stored["refresh_token"] == "rotated-refresh"  # noqa: S105


def test_oauth_refresh_works_when_agno_calls_sync_tool_on_running_loop(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("oauth-access"),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    tool = _build_tool(runtime_paths, manager, target)
    tool.g = _FakeGithub()

    async def call_sync_tool_entrypoint() -> str:
        return tool.list_repositories()

    result = asyncio.run(call_sync_tool_entrypoint())

    assert json.loads(result) == ["example/project"]


def test_terminal_refresh_failure_returns_safe_connection_payload(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool_class = _tool_class()
    target = _worker_target("@alice:example.test")
    leaked_secret = "refresh-secret-that-must-not-leak"  # noqa: S105
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("old-access", refresh_token=leaked_secret, expires_at=1.0),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    logger = _CapturingLogger()

    def reject_refresh(*_args: object, **_kwargs: object) -> None:
        msg = f"OAuth token refresh failed: invalid_grant {leaked_secret}"
        raise OAuthRefreshRejectedError(msg, oauth_error="invalid_grant")

    with (
        patch("mindroom.custom_tools.github.refresh_oauth_credentials_blocking", side_effect=reject_refresh),
        patch("mindroom.custom_tools.github.logger", logger),
    ):
        tool = tool_class(
            runtime_paths=runtime_paths,
            credentials_manager=manager,
            worker_target=target,
        )
        result = tool.list_repositories()

    payload = json.loads(result)
    assert payload["oauth_connection_required"] is True
    assert payload["provider"] == "github"
    assert payload["reason"] == "refresh_rejected"
    assert "session for this agent expired or is no longer valid" in payload["error"]
    assert leaked_secret not in result
    assert leaked_secret not in repr(logger.warning_calls)
    assert tool.access_token is None


def test_transient_refresh_failure_is_retryable_without_reconnect_payload(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool_class = _tool_class()
    target = _worker_target("@alice:example.test")
    oauth_target = _oauth_target("@alice:example.test")
    leaked_detail = "temporary provider failure with secret detail"
    original = _oauth_credentials("old-access", refresh_token=OLD_REFRESH_TOKEN, expires_at=1.0)
    save_scoped_credentials(
        "github_oauth",
        original,
        credentials_manager=manager,
        worker_target=oauth_target,
    )

    tool = tool_class(
        runtime_paths=runtime_paths,
        credentials_manager=manager,
        worker_target=target,
    )
    adopted = load_oauth_credentials(tool._oauth_credential_context())
    with (
        patch(
            "mindroom.custom_tools.github.refresh_oauth_credentials_blocking",
            side_effect=OAuthProviderError(leaked_detail, oauth_error="temporarily_unavailable"),
        ),
        pytest.raises(OAuthProviderError) as exc_info,
    ):
        tool.list_repositories()

    assert type(exc_info.value) is OAuthProviderError
    assert str(exc_info.value) == "OAuth credential refresh failed"
    assert leaked_detail not in str(exc_info.value)
    assert "/api/oauth/" not in str(exc_info.value)
    assert load_oauth_credentials(tool._oauth_credential_context()) == adopted


def test_revoked_unexpired_oauth_token_returns_connection_payload(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    revoked_token = "revoked-access-that-must-not-leak"  # noqa: S105
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials(revoked_token),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    tool = _build_tool(runtime_paths, manager, target)
    tool.g = _RevokedTokenGithub()

    result = tool.list_repositories()
    payload = json.loads(result)

    assert payload["oauth_connection_required"] is True
    assert payload["provider"] == "github"
    assert payload["reason"] == "access_rejected"
    assert "session for this agent expired or is no longer valid" in payload["error"]
    assert "/api/oauth/github/authorize?connect_token=" in payload["connect_url"]
    assert revoked_token not in result
    assert tool.access_token is None


@pytest.mark.parametrize("status_code", [401, 500])
@pytest.mark.parametrize(
    "operation",
    [
        "list_repositories",
        "update_file",
        "delete_file",
        "edit_issue",
        "get_pull_request_count",
    ],
)
def test_github_provider_failures_do_not_expose_provider_controlled_text(
    tmp_path: Path,
    status_code: int,
    operation: str,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("managed-access"),
        credentials_manager=manager,
        worker_target=_oauth_target("@alice:example.test"),
    )
    tool = _build_tool(runtime_paths, manager, target)
    sentinel = f"provider-controlled-secret-{operation}-{status_code}"
    tool.g = _ProviderControlledFailureGithub(status_code, sentinel)
    mindroom_logger = _CapturingLogger()
    agno_log_output = io.StringIO()
    agno_handler = logging.StreamHandler(agno_log_output)
    agno_handler.setFormatter(logging.Formatter("%(message)s"))
    agno_github_module.logger.addHandler(agno_handler)

    try:
        with patch("mindroom.custom_tools.github.logger", mindroom_logger):
            if operation == "list_repositories":
                result = tool.list_repositories()
            elif operation == "update_file":
                result = tool.update_file("example/project", "notes.txt", "body", "Update", "old-sha")
            elif operation == "delete_file":
                result = tool.delete_file("example/project", "notes.txt", "Delete", "old-sha")
            elif operation == "edit_issue":
                result = tool.edit_issue("example/project", 7, title="Updated")
            else:
                result = tool.get_pull_request_count("example/project")
    finally:
        agno_github_module.logger.removeHandler(agno_handler)
        agno_handler.close()

    payload = json.loads(result)
    if status_code == 401:
        assert payload["oauth_connection_required"] is True
        assert payload["reason"] == "access_rejected"
    else:
        assert payload == {"error": "GitHub request failed"}
    captured_logs = (
        agno_log_output.getvalue(),
        repr(mindroom_logger.warning_calls),
        repr(mindroom_logger.exception_calls),
    )
    assert all(sentinel not in output for output in captured_logs)
    assert sentinel not in result
    assert any(kwargs.get("status_code") == status_code for _event, kwargs in mindroom_logger.warning_calls)


def test_wrapper_preserves_all_registered_github_function_names(tmp_path: Path) -> None:
    from mindroom import tools as _mindroom_tools  # noqa: F401, PLC0415
    from mindroom.tool_system.catalog import TOOL_METADATA  # noqa: PLC0415

    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool = _build_tool(
        runtime_paths,
        manager,
        _worker_target("@alice:example.test"),
        access_token=MANUAL_ACCESS_TOKEN,
    )

    assert set(tool.functions) == set(TOOL_METADATA["github"].function_names)
