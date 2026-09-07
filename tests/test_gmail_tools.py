"""Tests for the custom Gmail tools wrapper."""

import base64
import json
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from functools import partial
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from agno.tools.google.gmail import GmailTools as AgnoGmailTools
from googleapiclient.errors import HttpError

from mindroom.agents import apply_tool_approval_capability
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.custom_tools.gmail import GmailTools
from mindroom.oauth.credential_lifecycle import load_oauth_credentials_snapshot_sync
from mindroom.oauth.providers import OAuthConnectionRequired


@pytest.fixture
def mock_credentials_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Create a mock credentials manager with test data."""
    manager = get_runtime_credentials_manager(runtime_paths)

    # Save test Gmail OAuth credentials
    test_creds = {
        "token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
    }
    manager.save_credentials("google_gmail_oauth", test_creds)
    return manager


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create an isolated runtime context for Gmail tool tests."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
    )
    get_runtime_credentials_manager(paths).save_credentials(
        "google_gmail_oauth_client",
        {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
        },
    )
    return paths


class TestGmailTools:
    """Test suite for custom Gmail tools wrapper."""

    @patch("google.oauth2.credentials.Credentials")
    def test_initialization_with_stored_credentials(
        self,
        mock_credentials_class: Mock,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test that GmailTools loads credentials from storage on init."""
        mock_creds_instance = MagicMock()
        mock_credentials_class.return_value = mock_creds_instance

        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            mock_credentials_class.assert_called_once_with(
                token="test_access_token",  # noqa: S106
                refresh_token="test_refresh_token",  # noqa: S106
                token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
                client_id="test_client_id",
                client_secret="test_client_secret",  # noqa: S106
                scopes=[
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.compose",
                ],
                quota_project_id=None,
                expiry=None,
            )

            # Verify parent class was initialized with credentials
            mock_parent_init.assert_called_once_with(creds=mock_creds_instance)

    @patch("mindroom.custom_tools.gmail.logger")
    def test_initialization_without_credentials(
        self,
        mock_logger: Mock,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test initialization when no credentials are stored."""
        mock_manager = CredentialsManager(runtime_paths.storage_root / "empty_credentials")

        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_manager)

            mock_logger.warning.assert_not_called()
            mock_parent_init.assert_called_once_with(creds=None)

    def test_service_account_env_uses_upstream_auth(self, tmp_path: Path) -> None:
        """Test env-only service account deployments bypass MindRoom OAuth."""
        runtime_paths = resolve_runtime_paths(
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "GOOGLE_GMAIL_CLIENT_ID": "test_client_id",
                "GOOGLE_GMAIL_CLIENT_SECRET": "test_client_secret",
                "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json"),
            },
        )

        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(
                runtime_paths=runtime_paths,
                credentials_manager=CredentialsManager(tmp_path / "credentials"),
            )

        assert gmail_tools._should_fallback_to_original_auth() is True

    def test_public_method_returns_structured_connect_instruction(self, runtime_paths: RuntimePaths) -> None:
        """Test decorated public methods preserve structured OAuth connection details."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=CredentialsManager(runtime_paths.storage_root / "credentials"),
        )

        result = json.loads(gmail_tools.get_latest_emails(1))

        assert result["oauth_connection_required"] is True
        assert result["provider"] == "google_gmail"
        assert "/api/oauth/google_gmail/authorize" in result["connect_url"]

        result = json.loads(gmail_tools.search_emails("invoice", 1))

        assert result["oauth_connection_required"] is True
        assert result["provider"] == "google_gmail"
        assert "/api/oauth/google_gmail/authorize" in result["connect_url"]

        result = json.loads(gmail_tools.send_email_to_self("Status", "Finished"))

        assert result["oauth_connection_required"] is True
        assert result["provider"] == "google_gmail"
        assert "/api/oauth/google_gmail/authorize" in result["connect_url"]

    @patch("mindroom.custom_tools.gmail.logger")
    @patch("google.oauth2.credentials.Credentials")
    def test_initialization_with_invalid_credentials(
        self,
        mock_credentials_class: Mock,
        mock_logger: Mock,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test initialization when credentials are invalid."""
        mock_credentials_manager.save_credentials("google_gmail_oauth", {"invalid": "data"})
        mock_credentials_class.side_effect = TypeError("Missing required fields")

        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            mock_credentials_class.assert_not_called()
            mock_logger.warning.assert_called_once_with(
                "oauth_credentials_missing_required_scopes",
                tool_name="gmail",
                provider_id="google_gmail",
            )
            mock_parent_init.assert_called_once_with(creds=None)

    @patch("google.auth.transport.requests.Request")
    def test_auth_with_valid_credentials(
        self,
        mock_request_class: Mock,  # noqa: ARG002
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth method with valid credentials."""
        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            gmail_tools.creds = MagicMock()
            gmail_tools.creds.valid = True
            gmail_tools._provided_creds = True

            gmail_tools._authenticate()

    @patch("google.auth.transport.requests.Request")
    @patch("google.oauth2.credentials.Credentials")
    def test_auth_with_expired_credentials(
        self,
        mock_credentials_class: Mock,
        mock_request_class: Mock,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth refreshes expired credentials."""
        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            gmail_tools.creds = None

            mock_creds = MagicMock()
            mock_creds.expired = True
            mock_creds.refresh_token = "refresh_token"  # noqa: S105
            mock_creds.token = "new_access_token"  # noqa: S105
            mock_creds.expiry = None
            refresh = mock_creds.refresh
            mock_credentials_class.return_value = mock_creds

            mock_request = MagicMock()
            mock_request_class.return_value = mock_request

            gmail_tools._authenticate()
            refresh.assert_called_once()
            bounded_request = refresh.call_args.args[0]
            assert isinstance(bounded_request, partial)
            assert bounded_request.func is mock_request
            assert bounded_request.keywords == {"timeout": 20.0}
            saved_creds = load_oauth_credentials_snapshot_sync(
                gmail_tools._oauth_credential_context(),
            ).credentials
            assert saved_creds is not None
            assert saved_creds["token"] == "new_access_token"  # noqa: S105

    @patch("mindroom.custom_tools.gmail.logger")
    def test_auth_without_stored_credentials(
        self,
        mock_logger: Mock,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth falls back to original auth when no credentials stored."""
        mock_manager = CredentialsManager(runtime_paths.storage_root / "empty_credentials")

        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None

            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_manager)
            gmail_tools.creds = None

            mock_parent_auth = Mock()
            gmail_tools._original_auth = mock_parent_auth

            with pytest.raises(OAuthConnectionRequired):
                gmail_tools._authenticate()

            # Verify warning was logged
            mock_logger.warning.assert_not_called()

            # Verify original auth was called
            mock_parent_auth.assert_not_called()

    def test_auth_error_handling(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth handles errors properly."""
        with (
            patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init,
            patch.object(GmailTools, "register"),
        ):
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)
            gmail_tools.creds = None

            # Mock Credentials to raise an exception
            with patch("google.oauth2.credentials.Credentials") as mock_creds:
                mock_creds.side_effect = Exception("Test error")

                with pytest.raises(OAuthConnectionRequired):
                    gmail_tools._authenticate()

    def test_inheritance_from_agno_gmail_tools(self) -> None:
        """Test that GmailTools properly inherits from AgnoGmailTools."""
        # Verify inheritance
        assert issubclass(GmailTools, AgnoGmailTools)

        # Verify the upstream default scopes are accessible
        assert isinstance(GmailTools.default_scopes, list)
        assert len(GmailTools.default_scopes) > 0

    def test_send_email_to_self_uses_only_connected_account(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """The self-send tool must derive its sole recipient from Gmail."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            send_email=False,
        )
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "owner@example.com",
        }
        service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "message-1"}
        gmail_tools.service = service

        function = gmail_tools.functions["send_email_to_self"]
        result = json.loads(function.entrypoint(subject="Status", body="Finished"))

        assert "send_email" not in gmail_tools.functions
        assert list(signature(function.entrypoint).parameters) == ["subject", "body"]
        assert result == {"id": "message-1"}
        service.users.return_value.getProfile.assert_called_once_with(userId="me")
        send = service.users.return_value.messages.return_value.send
        send.assert_called_once()
        assert send.call_args.kwargs["userId"] == "me"
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(send.call_args.kwargs["body"]["raw"]),
        )
        assert message.get_all("To") == ["owner@example.com"]
        assert message.get_all("Cc") is None
        assert message.get_all("Bcc") is None
        assert message["Subject"] == "Status"

    @pytest.mark.parametrize(
        "scopes",
        [
            ["https://mail.google.com/"],
            ["https://www.googleapis.com/auth/gmail.modify"],
            ["https://www.googleapis.com/auth/gmail.compose"],
            [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            ],
        ],
    )
    def test_send_email_to_self_accepts_sufficient_scope(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
        scopes: list[str],
    ) -> None:
        """Self-send accepts every scope that authorizes both API operations."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            include_tools=["send_email_to_self"],
            scopes=scopes,
        )

        assert set(gmail_tools.functions) == {"send_email_to_self"}

    @pytest.mark.parametrize(
        "scope",
        [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    )
    def test_send_email_to_self_is_omitted_with_insufficient_scope(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
        scope: str,
    ) -> None:
        """Self-send is unavailable when scopes authorize only one required operation."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            include_tools=["send_email_to_self"],
            scopes=[scope],
        )

        assert gmail_tools.functions == {}

    def test_insufficient_self_send_scope_preserves_compatible_gmail_functions(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """A missing self-send scope must not disable compatible Gmail functions."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            include_tools=["get_latest_emails", "send_email_to_self"],
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )

        assert set(gmail_tools.functions) == {"get_latest_emails"}

    @pytest.mark.parametrize(
        "tool_filter",
        [
            {"include_tools": []},
            {"include_tools": ["get_latest_emails"]},
            {"exclude_tools": ["send_email_to_self"]},
        ],
        ids=["empty-include", "omitted-from-include", "explicitly-excluded"],
    )
    def test_send_email_to_self_respects_tool_filters(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
        tool_filter: dict[str, list[str]],
    ) -> None:
        """Agno include and exclude filters apply to the local function."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            **tool_filter,
        )

        assert "send_email_to_self" not in gmail_tools.functions

    def test_send_email_to_self_rejects_recipient_header_injection(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Subject text must not provide another recipient header."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            send_email=False,
        )
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "owner@example.com",
        }
        gmail_tools.service = service

        result = gmail_tools.functions["send_email_to_self"].entrypoint(
            subject="Status\nBcc: other@example.com",
            body="Finished",
        )

        assert result.startswith("Error sending email:")
        service.users.return_value.messages.return_value.send.assert_not_called()

    @pytest.mark.parametrize(
        "profile",
        [
            None,
            {},
            {"emailAddress": None},
            {"emailAddress": ""},
            {"emailAddress": "first@example.com,second@example.com"},
            {"emailAddress": " owner@example.com"},
            {"emailAddress": "owner@example.com\n"},
        ],
    )
    def test_send_email_to_self_rejects_ambiguous_profile_address(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
        profile: object,
    ) -> None:
        """Missing, multiple, or padded profile addresses must fail closed."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
            send_email=False,
        )
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = profile
        gmail_tools.service = service

        with pytest.raises(RuntimeError, match="valid email address"):
            gmail_tools.functions["send_email_to_self"].entrypoint(subject="Status", body="Finished")

        service.users.return_value.messages.return_value.send.assert_not_called()

    def test_send_email_to_self_follows_configured_approval_policy(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Self-send must not override the operator's approval policy."""
        gmail_tools = GmailTools(
            runtime_paths=runtime_paths,
            credentials_manager=mock_credentials_manager,
        )

        result = apply_tool_approval_capability(
            gmail_tools,
            Config.model_validate({"tool_approval": {"default": "require_approval"}}),
            supports_native_tool_approval=True,
            registered_tool_name="gmail",
        )

        assert result is gmail_tools
        assert gmail_tools.functions["send_email_to_self"].requires_confirmation is True
        assert gmail_tools.functions["send_email"].requires_confirmation is True

    def test_gmail_metadata_advertises_send_email_to_self(self) -> None:
        """The built-in Gmail configuration must expose the self-send function."""
        from mindroom.tool_system.catalog import TOOL_METADATA  # noqa: PLC0415
        from mindroom.tools import gmail as _gmail_registration  # noqa: F401, PLC0415

        assert "send_email_to_self" in TOOL_METADATA["gmail"].function_names


class _GmailBatch:
    def __init__(
        self,
        callback: Callable[[str, object, Exception | None], None],
        outcomes: dict[str, int | None],
    ) -> None:
        self._callback = callback
        self._outcomes = outcomes
        self.request_ids: list[str] = []

    def add(self, _request: object, *, request_id: str) -> None:
        self.request_ids.append(request_id)

    def execute(self) -> None:
        for request_id in self.request_ids:
            status = self._outcomes[request_id]
            if status is None:
                self._callback(request_id, {"id": request_id}, None)
                continue
            response = SimpleNamespace(status=status, reason="provider error")
            self._callback(request_id, None, HttpError(response, b'{"error":"provider-controlled"}'))


class _GmailBatchService:
    def __init__(self, outcomes: dict[str, int | None]) -> None:
        self._outcomes = outcomes
        self.batches: list[_GmailBatch] = []

    def new_batch_http_request(
        self,
        *,
        callback: Callable[[str, object, Exception | None], None],
    ) -> _GmailBatch:
        batch = _GmailBatch(callback, self._outcomes)
        self.batches.append(batch)
        return batch


def test_gmail_batch_latches_401_across_mixed_chunked_results() -> None:
    """A per-item 401 must survive later successes and a chunk boundary."""
    tool = object.__new__(GmailTools)
    service = _GmailBatchService({"first": 401, "second": None, "third": None})
    tool.service = service
    tool.max_batch_size = 2

    results = tool._batch_get(["first", "second", "third"], lambda item_id: item_id)

    assert results == [
        {"id": "first", "error": "Google request failed"},
        {"id": "second"},
        {"id": "third"},
    ]
    assert [batch.request_ids for batch in service.batches] == [["first", "second"], ["third"]]
    assert tool._consume_google_authorization_rejected() is True


def test_gmail_batch_non_401_does_not_mark_authorization_rejected() -> None:
    """A non-auth provider failure must remain a normal tool failure."""
    tool = object.__new__(GmailTools)
    tool.service = _GmailBatchService({"first": 403})
    tool.max_batch_size = 10

    results = tool._batch_get(["first"], lambda item_id: item_id)

    assert results == [{"id": "first", "error": "Google request failed"}]
    assert tool._consume_google_authorization_rejected() is False
