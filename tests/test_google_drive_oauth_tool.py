"""Tests for the Google Drive OAuth-backed tool."""

# ruff: noqa: D102, D103, TC003

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    import pytest


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


class _FakeDriveRequest:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def execute(self) -> dict[str, object]:
        return self._response


class _FakeDriveFilesResource:
    def __init__(self) -> None:
        self.list_kwargs: dict[str, object] | None = None
        self.get_kwargs: dict[str, object] | None = None
        self.get_media_kwargs: dict[str, object] | None = None
        self.export_media_kwargs: dict[str, object] | None = None
        self.file_metadata: dict[str, object] = {
            "name": "Shared folder",
            "mimeType": "application/vnd.google-apps.folder",
            "webViewLink": "https://drive.google.com/drive/folders/example",
        }

    def list(self, **kwargs: object) -> _FakeDriveRequest:
        self.list_kwargs = kwargs
        return _FakeDriveRequest({"files": [], "nextPageToken": None, "incompleteSearch": True})

    def get(self, **kwargs: object) -> _FakeDriveRequest:
        self.get_kwargs = kwargs
        return _FakeDriveRequest(
            {
                "id": kwargs["fileId"],
                **self.file_metadata,
            },
        )

    def get_media(self, **kwargs: object) -> _FakeDriveRequest:
        self.get_media_kwargs = kwargs
        return _FakeDriveRequest({})

    def export_media(self, **kwargs: object) -> _FakeDriveRequest:
        self.export_media_kwargs = kwargs
        return _FakeDriveRequest({})


class _FakeDriveService:
    def __init__(self) -> None:
        self.files_resource = _FakeDriveFilesResource()

    def files(self) -> _FakeDriveFilesResource:
        return self.files_resource


class _ValidCredentials:
    valid = True


class _FakeMediaIoBaseDownload:
    def __init__(self, file_handle: object, _request: object) -> None:
        self._file_handle = file_handle
        self._done = False

    def next_chunk(self) -> tuple[None, bool]:
        if not self._done:
            self._file_handle.write(b"hello")
            self._done = True
        return None, self._done


def _google_drive_download_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    download_dir: Path | None = None,
) -> tuple[GoogleDriveTools, _FakeDriveService]:
    monkeypatch.setattr("mindroom.custom_tools.google_drive.MediaIoBaseDownload", _FakeMediaIoBaseDownload)
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=_ValidCredentials(),
        download_file=True,
        download_dir=download_dir or tmp_path,
    )
    service = _FakeDriveService()
    tool.service = service
    return tool, service


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


def test_google_drive_search_includes_shared_drive_parameters(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    cursor = "next-page"
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=_ValidCredentials(),
    )
    service = _FakeDriveService()
    tool.service = service

    result = json.loads(
        tool.search_files(
            query="'folder-id' in parents",
            max_results=3,
            page_token=cursor,
        ),
    )

    assert result["count"] == 0
    assert result["incompleteSearch"] is True
    assert service.files_resource.list_kwargs == {
        "q": "('folder-id' in parents) and trashed=false",
        "pageSize": 3,
        "orderBy": "modifiedTime desc",
        "fields": f"incompleteSearch, {tool.SEARCH_FIELDS}",
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
        "corpora": "allDrives",
        "pageToken": cursor,
    }


def test_google_drive_read_metadata_supports_shared_drive_files(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=_ValidCredentials(),
    )
    service = _FakeDriveService()
    tool.service = service

    result = json.loads(tool.read_file("shared-drive-folder-id"))

    assert result["error"] == "Cannot read application/vnd.google-apps.folder as text. Use download_file instead."
    assert service.files_resource.get_kwargs == {
        "fileId": "shared-drive-folder-id",
        "fields": tool.READ_METADATA_FIELDS,
        "supportsAllDrives": True,
    }


def test_google_drive_read_media_supports_shared_drive_files(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths_with_google_drive_client(tmp_path)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=_ValidCredentials(),
    )
    service = _FakeDriveService()
    service.files_resource.file_metadata = {
        "name": "notes.txt",
        "mimeType": "text/plain",
        "size": "5",
        "webViewLink": "https://drive.google.com/file/d/example",
    }
    tool.service = service
    tool._download_bytes = lambda _request: b"hello"

    result = json.loads(tool.read_file("shared-drive-file-id"))

    assert result["content"] == "hello"
    assert service.files_resource.get_media_kwargs == {
        "fileId": "shared-drive-file-id",
        "supportsAllDrives": True,
    }
    assert service.files_resource.export_media_kwargs is None


def test_google_drive_download_media_supports_shared_drive_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch)
    service.files_resource.file_metadata = {
        "name": "notes.txt",
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/d/example",
    }

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["status"] == "downloaded"
    assert Path(result["path"]) == tmp_path / "notes.txt"
    assert (tmp_path / "notes.txt").read_text() == "hello"
    assert service.files_resource.get_media_kwargs == {
        "fileId": "shared-drive-file-id",
        "supportsAllDrives": True,
    }
    assert service.files_resource.export_media_kwargs is None


def test_google_drive_download_rejects_parent_directory_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch, download_dir=tmp_path / "downloads")
    service.files_resource.file_metadata = {
        "name": "../escape.txt",
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/d/example",
    }
    outside_path = tmp_path / "escape.txt"

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["error"] == "Unsafe Google Drive filename: ../escape.txt"
    assert not outside_path.exists()
    assert service.files_resource.get_media_kwargs is None


def test_google_drive_download_rejects_absolute_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch, download_dir=tmp_path / "downloads")
    outside_path = tmp_path / "absolute.txt"
    service.files_resource.file_metadata = {
        "name": str(outside_path),
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/d/example",
    }

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["error"] == f"Unsafe Google Drive filename: {outside_path}"
    assert not outside_path.exists()
    assert service.files_resource.get_media_kwargs is None


def test_google_drive_download_rejects_windows_separators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch, download_dir=tmp_path / "downloads")
    service.files_resource.file_metadata = {
        "name": "folder\\notes.txt",
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/d/example",
    }

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["error"] == "Unsafe Google Drive filename: folder\\notes.txt"
    assert not (tmp_path / "downloads" / "folder\\notes.txt").exists()
    assert service.files_resource.get_media_kwargs is None


def test_google_drive_download_rejects_missing_filename_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch, download_dir=tmp_path / "downloads")
    service.files_resource.file_metadata = {
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/d/example",
    }

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["error"] == "Google Drive file metadata is missing a filename"
    assert not (tmp_path / "downloads").exists()
    assert service.files_resource.get_media_kwargs is None
    assert service.files_resource.export_media_kwargs is None


def test_google_drive_download_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("outside")
    (download_dir / "notes.txt").symlink_to(outside_path)
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch, download_dir=download_dir)
    service.files_resource.file_metadata = {
        "name": "notes.txt",
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/d/example",
    }

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["error"] == "Google Drive download target escapes the download directory"
    assert outside_path.read_text() == "outside"
    assert service.files_resource.get_media_kwargs is None
    assert service.files_resource.export_media_kwargs is None


def test_google_drive_download_adds_export_extension_inside_download_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch)
    service.files_resource.file_metadata = {
        "name": "proposal",
        "mimeType": "application/vnd.google-apps.document",
        "webViewLink": "https://drive.google.com/document/d/example",
    }
    tool._download_bytes = lambda _request: b"docx"

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["status"] == "exported"
    assert Path(result["path"]) == tmp_path / "proposal.docx"
    assert (tmp_path / "proposal.docx").read_bytes() == b"docx"
    assert service.files_resource.export_media_kwargs == {
        "fileId": "shared-drive-file-id",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    assert service.files_resource.get_media_kwargs is None


def test_google_drive_download_preserves_existing_export_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, service = _google_drive_download_tool(tmp_path, monkeypatch)
    service.files_resource.file_metadata = {
        "name": "proposal.docx",
        "mimeType": "application/vnd.google-apps.document",
        "webViewLink": "https://drive.google.com/document/d/example",
    }
    tool._download_bytes = lambda _request: b"docx"

    result = json.loads(tool.download_file("shared-drive-file-id"))

    assert result["status"] == "exported"
    assert Path(result["path"]) == tmp_path / "proposal.docx"
    assert (tmp_path / "proposal.docx").read_bytes() == b"docx"
    assert not (tmp_path / "proposal.docx.docx").exists()
    assert service.files_resource.export_media_kwargs == {
        "fileId": "shared-drive-file-id",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    assert service.files_resource.get_media_kwargs is None
