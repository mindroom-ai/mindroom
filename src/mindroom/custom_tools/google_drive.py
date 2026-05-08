"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from agno.tools.google.drive import GoogleDriveTools as AgnoGoogleDriveTools
from agno.tools.google.drive import WorkspaceType, authenticate
from agno.utils.log import log_error
from googleapiclient.errors import HttpError

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

    def _get_file_metadata(self, file_id: str, fields: str) -> dict[str, Any]:
        service = cast("Any", self.service)
        return service.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()

    @authenticate
    def search_files(self, query: str | None = None, max_results: int = 10, page_token: str | None = None) -> str:
        """Search Google Drive using a query expression, including files in Shared Drives."""
        if max_results < 1:
            return json.dumps({"error": "max_results must be greater than 0"})

        try:
            service = cast("Any", self.service)
            if self.include_trashed:
                effective_query = query or ""
            elif query:
                effective_query = f"({query}) and trashed=false"
            else:
                effective_query = "trashed=false"
            list_kwargs: dict[str, Any] = {
                "q": effective_query,
                "pageSize": max_results,
                "orderBy": "modifiedTime desc",
                "fields": f"incompleteSearch, {self.SEARCH_FIELDS}",
                "includeItemsFromAllDrives": True,
                "supportsAllDrives": True,
                "corpora": "allDrives",
            }
            if page_token:
                list_kwargs["pageToken"] = page_token
            results = service.files().list(**list_kwargs).execute()
            files = results.get("files", [])
            return json.dumps(
                {
                    "query": effective_query,
                    "files": files,
                    "count": len(files),
                    "nextPageToken": results.get("nextPageToken"),
                    "incompleteSearch": results.get("incompleteSearch", False),
                },
            )
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not search Google Drive files: {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    @authenticate
    def read_file(self, file_id: str) -> str:
        """Read a Drive file and return its text content, including files in Shared Drives."""
        try:
            service = cast("Any", self.service)
            metadata = self._get_file_metadata(file_id, self.READ_METADATA_FIELDS)
            mime_type = metadata.get("mimeType", "")

            if mime_type in self.TEXT_EXPORT_TYPES:
                export_mime = self.TEXT_EXPORT_TYPES[mime_type]
            elif mime_type.startswith(WorkspaceType.WORKSPACE_PREFIX):
                return json.dumps(
                    {"error": f"Cannot read {mime_type} as text. Use download_file instead.", "file": metadata},
                )
            else:
                export_mime = None

            if export_mime:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
                content_bytes = self._download_bytes(request)
            else:
                file_size = int(metadata.get("size", 0))
                if file_size > self.max_read_size:
                    return json.dumps(
                        {
                            "error": f"File is {file_size} bytes, exceeds max_read_size ({self.max_read_size}). Use download_file instead.",
                            "file": metadata,
                        },
                    )
                request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
                content_bytes = self._download_bytes(request)

            content = content_bytes.decode("utf-8", errors="replace")
            return json.dumps(
                {
                    "file": metadata,
                    "content": content,
                    "contentLength": len(content),
                    "exportMimeType": export_mime,
                },
            )
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not read Google Drive file {file_id}: {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
