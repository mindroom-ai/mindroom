"""Google Application Default Credential loading and Vertex AI Claude client settings."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import runtime_env_path
from mindroom.runtime_env_policy import VERTEXAI_CLAUDE_ENV_BY_KEY
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from google.auth.credentials import Credentials as GoogleCredentials

    from mindroom.constants import RuntimePaths

_GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class _GoogleApplicationCredentialsError(PermanentStartupError):
    """Raised when Google Application Default Credentials are invalid for startup."""


def _google_adc_file_type(credentials_path: str) -> str | None:
    """Return the ADC JSON credential type without invoking google-auth loaders."""
    try:
        with Path(credentials_path).open(encoding="utf-8") as credentials_file:
            credentials_info = json.load(credentials_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(credentials_info, dict):
        return None
    credentials_type = credentials_info.get("type")
    return credentials_type if isinstance(credentials_type, str) else None


def _load_google_application_credentials(credentials_path: str) -> GoogleCredentials:
    """Load Google ADC credentials for Vertex-backed model clients."""
    if not Path(credentials_path).is_file():
        msg = (
            "GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist: "
            f"{credentials_path}. Fix the path, recreate the credential file, or unset "
            "GOOGLE_APPLICATION_CREDENTIALS if this MindRoom instance should not use Vertex AI."
        )
        raise _GoogleApplicationCredentialsError(msg)

    credentials_type = _google_adc_file_type(credentials_path)
    try:
        if credentials_type == "service_account":
            service_account = importlib.import_module("google.oauth2.service_account")
            credentials_cls = service_account.Credentials
            credentials = credentials_cls.from_service_account_file(
                credentials_path,
                scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE],
            )
            return cast("GoogleCredentials", credentials)

        if credentials_type == "authorized_user":
            oauth_credentials = importlib.import_module("google.oauth2.credentials")
            credentials_cls = oauth_credentials.Credentials
            credentials = credentials_cls.from_authorized_user_file(
                credentials_path,
                scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE],
            )
            return cast("GoogleCredentials", credentials)

        google_auth = importlib.import_module("google.auth")
        load_credentials_from_file = google_auth.load_credentials_from_file
        credentials, _project_id = load_credentials_from_file(
            credentials_path,
            scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE],
        )
        return cast("GoogleCredentials", credentials)
    except Exception as exc:
        msg = f"Failed to load GOOGLE_APPLICATION_CREDENTIALS at {credentials_path}: {exc}"
        raise _GoogleApplicationCredentialsError(msg) from exc


def populate_vertexai_claude_runtime_kwargs(extra_kwargs: dict[str, Any], runtime_paths: RuntimePaths) -> None:
    """Fill Vertex AI Claude project, region, endpoint, and ADC credentials from the runtime env.

    Model loading and doctor share this, so doctor's probe reaches the endpoint the runtime calls.
    """
    for key, env_name in (
        ("project_id", VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"]),
        ("region", VERTEXAI_CLAUDE_ENV_BY_KEY["region"]),
        ("base_url", "ANTHROPIC_VERTEX_BASE_URL"),
    ):
        if key not in extra_kwargs and (value := runtime_paths.env_value(env_name)):
            extra_kwargs[key] = value
    client_params = dict(cast("dict[str, Any]", extra_kwargs.get("client_params") or {}))
    if "credentials" not in client_params and (
        credentials_path := runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    ):
        client_params["credentials"] = _load_google_application_credentials(str(credentials_path))
    if client_params:
        extra_kwargs["client_params"] = client_params
