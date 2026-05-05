"""Tests for the Google Drive OAuth-backed tool."""

# ruff: noqa: D102, D103, TC003

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

from agno.agent import Agent
from agno.agent._tools import parse_tools
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.tools.function import Function

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.oauth.google_drive import _GOOGLE_DRIVE_OAUTH_SCOPES
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target


class MinimalModel(Model):
    """Minimal model surface for Agno tool parsing tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


def _runtime_paths_with_google_drive_client(
    tmp_path: Path,
    process_env: dict[str, str] | None = None,
    *,
    redirect_uri: str | None = None,
) -> constants.RuntimePaths:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env or {},
    )
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "_source": "ui",
    }
    if redirect_uri is not None:
        credentials["redirect_uri"] = redirect_uri
    get_runtime_credentials_manager(runtime_paths).save_credentials("google_drive_oauth_client", credentials)
    return runtime_paths


def test_google_drive_missing_credentials_returns_connect_instruction(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=execution_identity,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))
    model_entrypoint = tool.functions["google_drive_search_files"].entrypoint
    assert model_entrypoint is not None
    model_result = json.loads(model_entrypoint(query="name contains 'plan'", max_results=1))

    assert "Google Drive is not connected for this agent" in result["error"]
    assert "https://mindroom.example.test/api/oauth/google_drive/authorize?connect_token=" in result["error"]
    assert "@alice:example.org" not in result["error"]
    assert model_result["oauth_connection_required"] is True
    assert model_result["provider"] == "google_drive"


def test_google_drive_model_functions_do_not_collide_with_local_file_tools(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    toolkits = [
        get_tool_by_name(
            tool_name,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=None,
            disable_sandbox_proxy=True,
        )
        for tool_name in ("file", "coding", "google_drive")
    ]
    model = MinimalModel(id="fake-model", provider="fake")
    agent = Agent(id="code", model=model)

    parsed_tools = parse_tools(agent, toolkits, model, async_mode=True)
    function_names = {function.name for function in parsed_tools if isinstance(function, Function)}

    assert {"read_file", "list_files", "search_files"} <= function_names
    assert {
        "google_drive_list_files",
        "google_drive_search_files",
        "google_drive_read_file",
    } <= function_names


def test_google_drive_connect_instruction_uses_redirect_uri_public_origin(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(
        tmp_path,
        redirect_uri="https://mindroom.example.test/api/oauth/google_drive/callback",
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=execution_identity,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert "https://mindroom.example.test/api/oauth/google_drive/authorize?connect_token=" in result["error"]
    assert "http://localhost:8765" not in result["error"]


def test_google_drive_credentials_restore_stored_expiry(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )
    expires_at = datetime(2030, 1, 1, tzinfo=UTC).timestamp()

    creds = tool._credentials_from_token_data(
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "client_id": "client-id",
            "expires_at": expires_at,
        },
    )

    assert creds.expiry.replace(tzinfo=UTC) == datetime(2030, 1, 1, tzinfo=UTC)


def test_google_drive_service_account_env_uses_upstream_auth(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(
        tmp_path,
        process_env={
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json"),
        },
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )
    tool.service_account_path = None

    assert tool._should_fallback_to_original_auth() is True


def test_google_drive_loads_tokens_from_oauth_service(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    expected_value = "access-token"
    credentials_manager.save_credentials(
        "google_drive",
        {
            "list_files": False,
            "_source": "ui",
        },
    )
    credentials_manager.save_credentials(
        "google_drive_oauth",
        {
            "token": expected_value,
            "refresh_token": "refresh-token",
            "_source": "oauth",
        },
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == expected_value
    assert "list_files" not in token_data


def test_google_drive_rejects_stored_token_missing_required_scopes(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive_oauth",
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "scopes": ["openid"],
            "_source": "oauth",
        },
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    assert tool.creds is None
    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert "Google Drive is not connected for this agent" in result["error"]


def test_google_drive_rejects_stored_token_disallowed_by_new_identity_policy(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(
        tmp_path,
        process_env={
            "GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS": "example.com",
        },
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive_oauth",
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "scopes": list(_GOOGLE_DRIVE_OAUTH_SCOPES),
            "_source": "oauth",
            "_oauth_provider": "google_drive",
            "_oauth_claims": {"email": "alice@blocked.example", "email_verified": True},
            "_oauth_claims_verified": True,
        },
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    assert tool.creds is None
    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_drive"


def test_google_drive_rejects_stored_token_missing_claims_when_identity_policy_configured(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(
        tmp_path,
        process_env={
            "GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS": "example.com",
        },
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive_oauth",
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "scopes": list(_GOOGLE_DRIVE_OAUTH_SCOPES),
            "_source": "oauth",
            "_oauth_provider": "google_drive",
        },
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    assert tool.creds is None
    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_drive"


def test_google_drive_stored_token_without_client_config_connects_on_invocation(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive_oauth",
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "scopes": list(_GOOGLE_DRIVE_OAUTH_SCOPES),
            "_source": "oauth",
        },
    )

    tool = get_tool_by_name(
        "google_drive",
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
        disable_sandbox_proxy=True,
    )
    assert isinstance(tool, GoogleDriveTools)

    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_drive"
    assert result["connect_url"].startswith("https://mindroom.example.test/api/oauth/google_drive/authorize")


def test_google_drive_mismatched_client_id_connects_on_invocation(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(
        tmp_path,
        {"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive_oauth",
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "client_id": "old-client-id",
            "scopes": list(_GOOGLE_DRIVE_OAUTH_SCOPES),
            "_source": "oauth",
        },
    )

    tool = get_tool_by_name(
        "google_drive",
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
        disable_sandbox_proxy=True,
    )
    assert isinstance(tool, GoogleDriveTools)

    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_drive"


def test_google_drive_saved_numeric_config_is_coerced_before_tool_init(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive",
        {
            "max_read_size": "42",
            "_source": "ui",
        },
    )

    tool = get_tool_by_name(
        "google_drive",
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
        disable_sandbox_proxy=True,
    )

    assert isinstance(tool, GoogleDriveTools)
    assert tool.max_read_size == 42


def test_google_drive_blank_numeric_config_uses_tool_default(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_drive",
        {
            "max_read_size": "",
            "_source": "ui",
        },
    )

    tool = get_tool_by_name(
        "google_drive",
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
        disable_sandbox_proxy=True,
    )

    assert isinstance(tool, GoogleDriveTools)
    assert tool.max_read_size == 10485760
