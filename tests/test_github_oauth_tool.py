"""Tests for the requester-scoped GitHub OAuth toolkit."""

# ruff: noqa: D103

from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import (
    CredentialsManager,
    get_runtime_credentials_manager,
    load_scoped_credentials,
    save_scoped_credentials,
)
from mindroom.oauth.providers import OAuthRefreshRejectedError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

DEFAULT_REFRESH_TOKEN = "github-refresh"  # noqa: S105
MANUAL_ACCESS_TOKEN = "manual-access"  # noqa: S105
OLD_REFRESH_TOKEN = "old-refresh"  # noqa: S105
ROTATED_REFRESH_TOKEN = "rotated-refresh"  # noqa: S105
ENV_ACCESS_TOKEN = "environment-access"  # noqa: S105


@dataclass(frozen=True)
class _FakeRepo:
    full_name: str


class _FakeUser:
    def get_repos(self) -> list[_FakeRepo]:
        return [_FakeRepo("example/project")]


class _FakeGithub:
    def get_user(self) -> _FakeUser:
        return _FakeUser()


class _CapturingLogger:
    def __init__(self) -> None:
        self.warning_calls: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warning_calls.append((event, kwargs))


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            **(extra_env or {}),
        },
    )


def _worker_target(requester_id: str) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
    )
    return resolve_worker_target("user_agent", "code", execution_identity=identity)


def _tool_class() -> type[Any]:
    spec = importlib.util.find_spec("mindroom.custom_tools.github")
    assert spec is not None, "the MindRoom GitHub wrapper module is missing"
    module = importlib.import_module("mindroom.custom_tools.github")
    return module.GithubTools


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
        worker_target=alice_target,
    )
    alice = _build_tool(runtime_paths, manager, alice_target)
    alice.g = _FakeGithub()
    bob = _build_tool(runtime_paths, manager, bob_target)

    assert json.loads(alice.list_repositories()) == ["example/project"]
    assert json.loads(bob.list_repositories())["oauth_connection_required"] is True


def test_explicit_access_token_takes_precedence_over_scoped_oauth(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("oauth-access", expires_at=1.0),
        credentials_manager=manager,
        worker_target=target,
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


def test_expired_oauth_credentials_refresh_and_persist_rotation(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = _save_client_config(runtime_paths)
    tool_class = _tool_class()
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github_oauth",
        _oauth_credentials("old-access", refresh_token=OLD_REFRESH_TOKEN, expires_at=1.0),
        credentials_manager=manager,
        worker_target=target,
    )
    refreshed = _oauth_credentials("rotated-access", refresh_token=ROTATED_REFRESH_TOKEN)

    async def refresh_credentials(*_args: object, **_kwargs: object) -> dict[str, object]:
        save_scoped_credentials(
            "github_oauth",
            refreshed,
            credentials_manager=manager,
            worker_target=target,
        )
        return refreshed

    with (
        patch("mindroom.custom_tools.github.refresh_scoped_oauth_credentials", side_effect=refresh_credentials),
        patch("mindroom.custom_tools.github.Github", return_value=_FakeGithub()),
    ):
        tool = tool_class(
            runtime_paths=runtime_paths,
            credentials_manager=manager,
            worker_target=target,
        )
        result = json.loads(tool.list_repositories())

    stored = load_scoped_credentials(
        "github_oauth",
        credentials_manager=manager,
        worker_target=target,
    )
    assert result == ["example/project"]
    assert stored is not None
    assert stored["token"] == "rotated-access"  # noqa: S105
    assert stored["refresh_token"] == "rotated-refresh"  # noqa: S105


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
        worker_target=target,
    )
    logger = _CapturingLogger()

    async def reject_refresh(*_args: object, **_kwargs: object) -> None:
        msg = f"OAuth token refresh failed: invalid_grant {leaked_secret}"
        raise OAuthRefreshRejectedError(msg, oauth_error="invalid_grant")

    with (
        patch("mindroom.custom_tools.github.refresh_scoped_oauth_credentials", side_effect=reject_refresh),
        patch("mindroom.custom_tools.github.logger", logger),
    ):
        result = tool_class(
            runtime_paths=runtime_paths,
            credentials_manager=manager,
            worker_target=target,
        ).list_repositories()

    payload = json.loads(result)
    assert payload["oauth_connection_required"] is True
    assert payload["provider"] == "github"
    assert leaked_secret not in result
    assert leaked_secret not in repr(logger.warning_calls)


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
