"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.google.drive import GoogleDriveTools as AgnoGoogleDriveTools

from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin, google_service_account_configured
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.tool_system.metadata import coerce_optional_finite_number
from mindroom.tool_system.toolkit_aliases import apply_toolkit_function_aliases

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_MODEL_FUNCTION_NAME_ALIASES = {
    "list_files": "google_drive_list_files",
    "search_files": "google_drive_search_files",
    "read_file": "google_drive_read_file",
}


def _max_read_size_finite_error(value: object) -> TypeError | ValueError:
    msg = "Google Drive max_read_size must be a finite number"
    if isinstance(value, str):
        return ValueError(msg)
    return TypeError(msg)


class GoogleDriveTools(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, AgnoGoogleDriveTools):
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
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )
        super().__init__(creds=creds, **kwargs)
        self._set_original_auth(AgnoGoogleDriveTools._auth)
        self._wrap_oauth_function_entrypoints()
        apply_toolkit_function_aliases(self, _MODEL_FUNCTION_NAME_ALIASES)

    def _coerce_max_read_size(self, value: object) -> int | float | None:
        try:
            return coerce_optional_finite_number(value)
        except OverflowError as exc:
            raise _max_read_size_finite_error(value) from exc
        except TypeError as exc:
            msg = "Google Drive max_read_size must be a number"
            raise TypeError(msg) from exc
        except ValueError as exc:
            msg = "Google Drive max_read_size must be a number"
            raise ValueError(msg) from exc

    def _should_fallback_to_original_auth(self) -> bool:
        return google_service_account_configured(self.service_account_path, self._runtime_paths)
