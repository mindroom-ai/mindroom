"""Google Application Default Credential loading helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from google.auth.credentials import Credentials as GoogleCredentials

_GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class _GoogleApplicationCredentialsError(PermanentStartupError):
    """Raised when Google Application Default Credentials are invalid for startup."""


def _google_adc_file_type(credentials_path: str) -> str | None:
    """Return the ADC JSON credential type without invoking google-auth loaders."""
    try:
        credentials_info = json.loads(Path(credentials_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    credentials_type = credentials_info.get("type") if isinstance(credentials_info, dict) else None
    return credentials_type if isinstance(credentials_type, str) else None


def load_ambient_google_credentials() -> tuple[GoogleCredentials, str | None]:
    """Locate Application Default Credentials and the project they name, without minting a token.

    Raises google-auth's ``DefaultCredentialsError`` when the host has none, as the SDK's own lookup does.
    """
    import google.auth  # noqa: PLC0415 - loaded on the first Vertex request, off the event loop

    credentials, project_id = google.auth.default(scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE])
    return cast("GoogleCredentials", credentials), project_id


def load_google_application_credentials(credentials_path: str) -> GoogleCredentials:
    """Load Google ADC credentials for Vertex-backed model clients."""
    if not Path(credentials_path).is_file():
        msg = (
            "GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist: "
            f"{credentials_path}. Fix the path, recreate the credential file, or unset "
            "GOOGLE_APPLICATION_CREDENTIALS if this MindRoom instance should not use Vertex AI."
        )
        raise _GoogleApplicationCredentialsError(msg)

    credentials_type = _google_adc_file_type(credentials_path)
    scopes = [_GOOGLE_CLOUD_PLATFORM_SCOPE]
    try:
        if credentials_type == "service_account":
            from google.oauth2.service_account import Credentials as ServiceAccount  # noqa: PLC0415 - Vertex only

            return ServiceAccount.from_service_account_file(credentials_path, scopes=scopes)
        if credentials_type == "authorized_user":
            from google.oauth2.credentials import Credentials as AuthorizedUser  # noqa: PLC0415 - Vertex only

            return AuthorizedUser.from_authorized_user_file(credentials_path, scopes=scopes)
        import google.auth  # noqa: PLC0415 - Vertex only

        return google.auth.load_credentials_from_file(credentials_path, scopes=scopes)[0]
    except Exception as exc:
        msg = f"Failed to load GOOGLE_APPLICATION_CREDENTIALS at {credentials_path}: {exc}"
        raise _GoogleApplicationCredentialsError(msg) from exc
