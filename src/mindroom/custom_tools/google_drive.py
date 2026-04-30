"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from agno.tools.google.drive import GoogleDriveTools as AgnoGoogleDriveTools

from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_drive import google_drive_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class GoogleDriveTools(ScopedOAuthClientMixin, AgnoGoogleDriveTools):
    """Google Drive toolkit that reads OAuth tokens from MindRoom's credential scopes."""

    _oauth_provider = google_drive_oauth_provider()
    _oauth_tool_name = "google_drive"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GoogleDriveTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        if "max_read_size" in kwargs:
            max_read_size = self._coerce_max_read_size(kwargs["max_read_size"])
            if max_read_size is None:
                kwargs.pop("max_read_size")
            else:
                kwargs["max_read_size"] = max_read_size
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            provided_creds=provided_creds,
            logger=logger,
        )
        super().__init__(creds=creds, **kwargs)
        self._set_original_auth(AgnoGoogleDriveTools._auth)
        self._wrap_oauth_function_entrypoints()

    def _coerce_max_read_size(self, value: object) -> int | float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            msg = "Google Drive max_read_size must be a number"
            raise TypeError(msg)
        if isinstance(value, int | float) and math.isfinite(value):
            return value
        if isinstance(value, int | float):
            msg = "Google Drive max_read_size must be a finite number"
            raise TypeError(msg)
        if isinstance(value, str):
            raw_value = value.strip()
            if not raw_value:
                return None
            try:
                parsed = float(raw_value)
            except ValueError as exc:
                msg = "Google Drive max_read_size must be a number"
                raise ValueError(msg) from exc
            if not math.isfinite(parsed):
                msg = "Google Drive max_read_size must be a finite number"
                raise ValueError(msg)
            return int(parsed) if parsed.is_integer() else parsed
        msg = "Google Drive max_read_size must be a number"
        raise TypeError(msg)

    def _should_fallback_to_original_auth(self) -> bool:
        """Prefer the upstream auth path when a service account is configured."""
        return bool(self.service_account_path or self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))
