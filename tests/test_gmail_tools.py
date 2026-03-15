"""Tests for the custom Gmail tools wrapper."""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from agno.tools.gmail import GmailTools as AgnoGmailTools

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.gmail import GmailTools


@pytest.fixture
def mock_credentials_manager(tmp_path: Path) -> CredentialsManager:
    """Create a mock credentials manager with test data."""
    manager = CredentialsManager(base_path=tmp_path / "test_creds")

    # Save test Google credentials
    test_creds = {
        "token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
    }
    manager.save_credentials("google", test_creds)
    return manager


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create an isolated runtime context for Gmail tool tests."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={},
    )


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

        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None
            GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            mock_credentials_class.assert_called_once_with(
                token="test_access_token",  # noqa: S106
                refresh_token="test_refresh_token",  # noqa: S106
                token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
                client_id="test_client_id",
                client_secret="test_client_secret",  # noqa: S106
                scopes=[
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.compose",
                ],
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
        mock_manager = MagicMock()
        mock_manager.load_credentials.return_value = None
        mock_manager.shared_manager.return_value = mock_manager

        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None
            GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_manager)

            mock_logger.warning.assert_not_called()
            mock_parent_init.assert_called_once_with(creds=None)

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
        mock_credentials_manager.save_credentials("google", {"invalid": "data"})
        mock_credentials_class.side_effect = TypeError("Missing required fields")

        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None
            GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            mock_logger.exception.assert_called_once()
            mock_parent_init.assert_called_once_with(creds=None)

    @patch("google.auth.transport.requests.Request")
    def test_auth_with_valid_credentials(
        self,
        mock_request_class: Mock,  # noqa: ARG002
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth method with valid credentials."""
        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            gmail_tools.creds = MagicMock()
            gmail_tools.creds.valid = True

            gmail_tools._auth()

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
        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)

            gmail_tools.creds = None

            mock_creds = MagicMock()
            mock_creds.expired = True
            mock_creds.refresh_token = "refresh_token"  # noqa: S105
            mock_creds.token = "new_access_token"  # noqa: S105
            mock_credentials_class.return_value = mock_creds

            mock_request = MagicMock()
            mock_request_class.return_value = mock_request

            gmail_tools._auth()
            mock_creds.refresh.assert_called_once_with(mock_request)
            saved_creds = mock_credentials_manager.load_credentials("google")
            assert saved_creds is not None
            assert saved_creds["token"] == "new_access_token"  # noqa: S105

    @patch("mindroom.custom_tools.gmail.logger")
    def test_auth_without_stored_credentials(
        self,
        mock_logger: Mock,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth falls back to original auth when no credentials stored."""
        mock_manager = MagicMock()
        mock_manager.load_credentials.return_value = None
        mock_manager.shared_manager.return_value = mock_manager

        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None

            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_manager)
            gmail_tools.creds = None

            mock_parent_auth = Mock()
            gmail_tools._original_auth = mock_parent_auth

            gmail_tools._auth()

            # Verify warning was logged
            mock_logger.warning.assert_called_with(
                "No stored credentials found, initiating OAuth flow",
            )

            # Verify original auth was called
            mock_parent_auth.assert_called_once()

    def test_auth_error_handling(
        self,
        mock_credentials_manager: CredentialsManager,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test _auth handles errors properly."""
        with patch("mindroom.custom_tools.gmail.AgnoGmailTools.__init__") as mock_parent_init:
            mock_parent_init.return_value = None
            gmail_tools = GmailTools(runtime_paths=runtime_paths, credentials_manager=mock_credentials_manager)
            gmail_tools.creds = None

            # Mock Credentials to raise an exception
            with patch("google.oauth2.credentials.Credentials") as mock_creds:
                mock_creds.side_effect = Exception("Test error")

                # Should raise the exception
                with pytest.raises(Exception, match="Test error"):
                    gmail_tools._auth()

    def test_inheritance_from_agno_gmail_tools(self) -> None:
        """Test that GmailTools properly inherits from AgnoGmailTools."""
        # Verify inheritance
        assert issubclass(GmailTools, AgnoGmailTools)

        # Verify DEFAULT_SCOPES is accessible
        assert hasattr(GmailTools, "DEFAULT_SCOPES")
        assert isinstance(GmailTools.DEFAULT_SCOPES, list)
        assert len(GmailTools.DEFAULT_SCOPES) > 0
