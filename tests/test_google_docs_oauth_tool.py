"""Tests for the Google Docs OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from googleapiclient.errors import HttpError
from httplib2 import Response

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.config.main import Config
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.google_docs import GoogleDocsTools
from mindroom.oauth.google_docs import google_docs_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class _ValidCredentials:
    valid = True


class _FakeDocsRequest:
    def __init__(self, response: dict[str, object], error: HttpError | None = None) -> None:
        self._response = response
        self._error = error

    def execute(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeDocumentsResource:
    def __init__(self) -> None:
        self.create_bodies: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.batch_update_calls: list[dict[str, object]] = []
        self.batch_update_error: HttpError | None = None

    def create(self, *, body: dict[str, object]) -> _FakeDocsRequest:
        self.create_bodies.append(body)
        return _FakeDocsRequest(
            {
                "documentId": "created-doc",
                "title": body["title"],
            },
        )

    def get(self, **kwargs: object) -> _FakeDocsRequest:
        self.get_calls.append(kwargs)
        return _FakeDocsRequest(
            {
                "documentId": kwargs["documentId"],
                "title": "Plan",
                "tabs": [
                    {
                        "tabProperties": {"tabId": "tab-1", "title": "Tab 1"},
                        "documentTab": {
                            "body": {
                                "content": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": 6,
                                        "paragraph": {
                                            "elements": [
                                                {
                                                    "textRun": {"content": "Hello"},
                                                },
                                            ],
                                        },
                                    },
                                ],
                            },
                        },
                    },
                ],
            },
        )

    def batchUpdate(self, **kwargs: object) -> _FakeDocsRequest:  # noqa: N802
        self.batch_update_calls.append(kwargs)
        return _FakeDocsRequest(
            {
                "documentId": kwargs["documentId"],
                "replies": [{"replaceAllText": {"occurrencesChanged": 2}}],
            },
            self.batch_update_error,
        )


class _FakeDocsService:
    def __init__(self) -> None:
        self.documents_resource = _FakeDocumentsResource()

    def documents(self) -> _FakeDocumentsResource:
        return self.documents_resource


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            **(extra_env or {}),
        },
    )


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    return resolve_worker_target("user_agent", "general", execution_identity=identity)


def _connected_tool(tmp_path: Path) -> tuple[GoogleDocsTools, _FakeDocsService]:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_ValidCredentials(),
    )
    service = _FakeDocsService()
    tool.service = service
    return tool, service


def test_google_docs_missing_credentials_returns_scoped_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    result = json.loads(tool.google_docs_get_document("document-id"))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_docs"
    assert "https://mindroom.example.test/api/oauth/google_docs/authorize?connect_token=" in result["connect_url"]
    assert "@alice:example.org" not in result["connect_url"]


def test_google_docs_tokens_stay_separate_from_dashboard_settings(tmp_path: Path) -> None:
    manager = CredentialsManager(tmp_path / "credentials")
    manager.save_credentials("google_docs", {"edit_document": False, "_source": "ui"})
    manager.save_credentials(
        "google_docs_oauth",
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
    )
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == "access-token"  # noqa: S105
    assert "edit_document" not in token_data


def test_google_docs_create_document_can_insert_initial_text(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_docs_create_document("Launch plan", "First draft"))

    assert service.documents_resource.create_bodies == [{"title": "Launch plan"}]
    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "created-doc",
            "body": {
                "requests": [
                    {
                        "insertText": {
                            "endOfSegmentLocation": {},
                            "text": "First draft",
                        },
                    },
                ],
            },
        },
    ]
    assert result["document"]["documentId"] == "created-doc"
    assert result["documentUrl"] == "https://docs.google.com/document/d/created-doc/edit"
    assert result["initialTextUpdate"]["documentId"] == "created-doc"


def test_google_docs_create_document_preserves_id_when_initial_text_fails(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.documents_resource.batch_update_error = HttpError(
        Response({"status": "400", "reason": "Bad Request"}),
        b'{"error":{"message":"bad request"}}',
    )

    result = json.loads(tool.google_docs_create_document("Launch plan", "First draft"))

    assert result["document"]["documentId"] == "created-doc"
    assert result["documentUrl"] == "https://docs.google.com/document/d/created-doc/edit"
    assert "400" in result["initialTextError"]


def test_google_docs_get_document_returns_tab_aware_structure(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_docs_get_document("document-id"))

    assert service.documents_resource.get_calls == [
        {
            "documentId": "document-id",
            "includeTabsContent": True,
        },
    ]
    assert result["document"]["tabs"][0]["tabProperties"]["tabId"] == "tab-1"
    assert (
        result["document"]["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
            "content"
        ]
        == "Hello"
    )


def test_google_docs_insert_text_supports_index_and_tab(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_docs_insert_text("document-id", "new text", index=6, tab_id="tab-2"))

    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "document-id",
            "body": {
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 6, "tabId": "tab-2"},
                            "text": "new text",
                        },
                    },
                ],
            },
        },
    ]
    assert result["update"]["documentId"] == "document-id"


def test_google_docs_insert_text_appends_when_index_is_omitted(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_docs_insert_text("document-id", "append", tab_id="tab-2")

    assert service.documents_resource.batch_update_calls[0]["body"] == {
        "requests": [
            {
                "insertText": {
                    "endOfSegmentLocation": {"tabId": "tab-2"},
                    "text": "append",
                },
            },
        ],
    }


def test_google_docs_replace_text_can_target_selected_tabs(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_replace_text(
            "document-id",
            "DRAFT",
            "Final",
            match_case=True,
            tab_ids=["tab-1", "tab-2"],
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "document-id",
            "body": {
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": "DRAFT", "matchCase": True},
                            "replaceText": "Final",
                            "tabsCriteria": {"tabIds": ["tab-1", "tab-2"]},
                        },
                    },
                ],
            },
        },
    ]
    assert result["update"]["replies"][0]["replaceAllText"]["occurrencesChanged"] == 2


def test_google_docs_config_flags_control_registered_functions(tmp_path: Path) -> None:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_ValidCredentials(),
        create_document=False,
        read_document=True,
        edit_document=False,
    )

    assert set(tool.functions) == {"google_docs_get_document"}


def test_google_docs_agent_config_accepts_inline_operation_flags() -> None:
    config = Config(
        agents={
            "writer": {
                "display_name": "Writer",
                "tools": [
                    {
                        "google_docs": {
                            "create_document": True,
                            "read_document": True,
                            "edit_document": False,
                        },
                    },
                ],
            },
        },
    )

    docs_config = next(entry for entry in config.resolve_entity("writer").tool_configs if entry.name == "google_docs")

    assert docs_config.tool_config_overrides == {
        "create_document": True,
        "read_document": True,
        "edit_document": False,
    }


def test_google_docs_provider_uses_docs_scope_without_drive_scope() -> None:
    provider = google_docs_oauth_provider()

    assert "https://www.googleapis.com/auth/documents" in provider.scopes
    assert "https://www.googleapis.com/auth/drive" not in provider.scopes
    assert "https://www.googleapis.com/auth/drive.file" not in provider.scopes
    assert "https://www.googleapis.com/auth/drive.readonly" not in provider.scopes
    assert "include_granted_scopes" not in provider.extra_auth_params


def test_google_docs_service_account_env_uses_primary_runtime_auth(tmp_path: Path) -> None:
    service_account_path = tmp_path / "service-account.json"
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(
            tmp_path,
            {"GOOGLE_SERVICE_ACCOUNT_FILE": str(service_account_path)},
        ),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )

    assert tool.service_account_path == str(service_account_path)
    assert tool._should_fallback_to_original_auth() is True
