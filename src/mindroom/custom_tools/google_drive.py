"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from functools import wraps
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, cast

from agno.tools.google.drive import GoogleDriveTools as AgnoGoogleDriveTools
from agno.tools.google.drive import MediaIoBaseDownload, WorkspaceType, authenticate
from agno.utils.log import log_error
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin, google_service_account_configured
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_drive import (
    GOOGLE_DRIVE_READ_OAUTH_SCOPES,
    GOOGLE_DRIVE_WRITE_SCOPE,
    google_drive_oauth_provider,
)
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.oauth.service import oauth_connect_url, oauth_credentials_have_scopes
from mindroom.tool_system.metadata import coerce_optional_finite_number
from mindroom.tool_system.toolkit_aliases import apply_toolkit_function_aliases
from mindroom.workspaces import resolve_workspace_relative_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_MODEL_FUNCTION_NAME_ALIASES = {
    "list_files": "google_drive_list_files",
    "search_files": "google_drive_search_files",
    "read_file": "google_drive_read_file",
    "download_file": "google_drive_download_file",
    "upload_file": "google_drive_upload_file",
    "create_folder": "google_drive_create_folder",
    "move_file": "google_drive_move_file",
    "trash_file": "google_drive_trash_file",
}
_WRITE_FUNCTION_NAMES = ("upload_file", "create_folder", "move_file", "trash_file")
_WRITE_RESULT_FIELDS = "id,name,mimeType,modifiedTime,size,parents,trashed,webViewLink"
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _max_read_size_finite_error(value: object) -> TypeError | ValueError:
    msg = "Google Drive max_read_size must be a finite number"
    if isinstance(value, str):
        return ValueError(msg)
    return TypeError(msg)


def _unsafe_drive_filename_error(filename: object) -> str | None:
    if filename is None:
        return "Google Drive file metadata is missing a filename"
    if not isinstance(filename, str):
        return f"Google Drive file metadata filename must be a string: {filename!r}"
    if filename.strip() == "" or filename in {".", ".."}:
        return f"Unsafe Google Drive filename: {filename}"
    if "\x00" in filename or "/" in filename or "\\" in filename:
        return f"Unsafe Google Drive filename: {filename}"

    windows_filename = PureWindowsPath(filename)
    if windows_filename.drive or windows_filename.root:
        return f"Unsafe Google Drive filename: {filename}"
    return None


def _download_target_path(download_dir: str | Path, filename: str, extension: str) -> Path | None:
    download_root = Path(download_dir)
    target_path = download_root / filename
    if extension and not target_path.suffix:
        target_path = target_path.with_suffix(extension)

    resolved_root = download_root.resolve()
    resolved_target = target_path.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        return None
    return target_path


class GoogleDriveTools(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, AgnoGoogleDriveTools):
    """Google Drive toolkit that uses MindRoom-scoped OAuth credentials."""

    _oauth_provider = google_drive_oauth_provider()
    _oauth_tool_name = "google_drive"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        tool_output_workspace_root: Path | None = None,
        write: bool = True,
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
        if kwargs.get("download_file"):
            if tool_output_workspace_root is None:
                logger.warning("Google Drive downloads are disabled because this agent has no workspace")
                kwargs["download_file"] = False
            else:
                kwargs["download_dir"] = tool_output_workspace_root / "google-drive-downloads"
        kwargs["upload_file"] = False
        kwargs.setdefault("scopes", [GOOGLE_DRIVE_WRITE_SCOPE])
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        self._workspace_root = tool_output_workspace_root
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )
        super().__init__(creds=creds, **kwargs)
        if write:
            self._register_write_tools()
        self._set_original_auth(AgnoGoogleDriveTools._auth)
        self._wrap_oauth_function_entrypoints()
        self._wrap_write_scope_entrypoints()
        apply_toolkit_function_aliases(self, _MODEL_FUNCTION_NAME_ALIASES)

    def _register_write_tools(self) -> None:
        sync_tools = (
            (self._upload_file, "upload_file"),
            (self.create_folder, "create_folder"),
            (self.move_file, "move_file"),
            (self.trash_file, "trash_file"),
        )
        async_tools = (
            (self._aupload_file, "upload_file"),
            (self.acreate_folder, "create_folder"),
            (self.amove_file, "move_file"),
            (self.atrash_file, "trash_file"),
        )
        self.tools = (*self.tools, *(tool for tool, _name in sync_tools))
        self._async_tools = (*self._async_tools, *async_tools)
        for tool, name in sync_tools:
            self.register(tool, name=name)
        for tool, name in async_tools:
            self.register(tool, name=name)

    def _stored_credentials_have_required_scopes(self, token_data: dict[str, Any]) -> bool:
        """Let old read-only grants continue authenticating read operations."""
        return oauth_credentials_have_scopes(token_data, GOOGLE_DRIVE_READ_OAUTH_SCOPES)

    def _write_scope_upgrade_result(self) -> str | None:
        if self._provided_creds or self._should_fallback_to_original_auth():
            return None
        token_data = self._load_token_data()
        if not token_data or oauth_credentials_have_scopes(token_data, self._oauth_provider.scopes):
            return None
        if not oauth_credentials_have_scopes(token_data, GOOGLE_DRIVE_READ_OAUTH_SCOPES):
            return None

        connect_url = oauth_connect_url(
            self._oauth_provider,
            self._runtime_paths,
            worker_target=self._worker_target,
        )
        error = OAuthConnectionRequired(
            "Google Drive reconnect required to grant write access. "
            f"Reconnect with this MindRoom link, then retry the write: {connect_url}",
            provider_id=self._oauth_provider.id,
            connect_url=connect_url,
            reason="missing_write_scope",
        )
        return self._structured_auth_failure(error)

    def _wrap_write_scope_entrypoints(self) -> None:
        """Require full Drive scope only for registered write functions."""
        for function_name in _WRITE_FUNCTION_NAMES:
            function = self.functions.get(function_name)
            if function is None or function.entrypoint is None:
                continue
            entrypoint = function.entrypoint

            @wraps(entrypoint)
            def write_scope_entrypoint(
                *args: object,
                _entrypoint: Callable[..., object] = entrypoint,
                **kwargs: object,
            ) -> object:
                if result := self._write_scope_upgrade_result():
                    return result
                return _entrypoint(*args, **kwargs)

            function.entrypoint = write_scope_entrypoint
            setattr(self, function_name, write_scope_entrypoint)

        for function_name in _WRITE_FUNCTION_NAMES:
            function = self.async_functions.get(function_name)
            if function is None or function.entrypoint is None:
                continue
            entrypoint = function.entrypoint

            @wraps(entrypoint)
            async def write_scope_async_entrypoint(
                *args: object,
                _entrypoint: Callable[..., Any] = entrypoint,
                **kwargs: object,
            ) -> object:
                if result := self._write_scope_upgrade_result():
                    return result
                return await _entrypoint(*args, **kwargs)

            function.entrypoint = write_scope_async_entrypoint

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

    def _resolve_upload_path(self, local_path: str) -> Path:
        requested_path = Path(local_path).expanduser()
        if requested_path.is_absolute():
            resolved_path = requested_path.resolve()
            if self._workspace_root is not None and not resolved_path.is_relative_to(self._workspace_root.resolve()):
                msg = f"Google Drive local_path must stay within the workspace root: {self._workspace_root.resolve()}"
                raise ValueError(msg)
            return resolved_path
        if self._workspace_root is None:
            msg = "Google Drive relative local_path requires an agent workspace"
            raise ValueError(msg)
        return resolve_workspace_relative_path(
            self._workspace_root,
            requested_path,
            field_name="Google Drive local_path",
        )

    def _download_guidance(self) -> str:
        if "google_drive_download_file" in self.functions:
            return " Use google_drive_download_file instead."
        return ""

    def _get_file_metadata(self, file_id: str, fields: str) -> dict[str, Any]:
        service = cast("Any", self.service)
        return service.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()

    @authenticate
    def _upload_file(
        self,
        local_path: str,
        folder_id: str | None = None,
        name: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        """Upload one local file, resolving relative paths from the agent workspace."""
        try:
            path = self._resolve_upload_path(local_path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if not path.is_file():
            return json.dumps({"error": f"The file '{path}' does not exist or is not a file."})

        resolved_mime_type = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body: dict[str, object] = {"name": name or path.name}
        if folder_id:
            body["parents"] = [folder_id]
        try:
            service = cast("Any", self.service)
            uploaded_file = (
                service.files()
                .create(
                    body=body,
                    media_body=MediaFileUpload(str(path), mimetype=resolved_mime_type),
                    fields=_WRITE_RESULT_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
            return json.dumps(uploaded_file)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not upload file '{path}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    async def _aupload_file(
        self,
        local_path: str,
        folder_id: str | None = None,
        name: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        """Upload one local file without blocking the async agent loop."""
        if result := self._write_scope_upgrade_result():
            return result
        return await asyncio.to_thread(
            self._upload_file,
            local_path,
            folder_id=folder_id,
            name=name,
            mime_type=mime_type,
        )

    @authenticate
    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Create a Google Drive folder, optionally under one parent folder."""
        if not name.strip():
            return json.dumps({"error": "Google Drive folder name must not be empty"})
        body: dict[str, object] = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
        if parent_id:
            body["parents"] = [parent_id]
        try:
            service = cast("Any", self.service)
            folder = service.files().create(body=body, fields=_WRITE_RESULT_FIELDS, supportsAllDrives=True).execute()
            return json.dumps(folder)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not create Google Drive folder '{name}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    async def acreate_folder(self, name: str, parent_id: str | None = None) -> str:
        """Create a Google Drive folder without blocking the async agent loop."""
        return await asyncio.to_thread(self.create_folder, name, parent_id=parent_id)

    @authenticate
    def move_file(self, file_id: str, new_parent_id: str, name: str | None = None) -> str:
        """Move one Drive file to a new parent and optionally rename it."""
        if not new_parent_id.strip():
            return json.dumps({"error": "Google Drive new_parent_id must not be empty"})
        if name is not None and not name.strip():
            return json.dumps({"error": "Google Drive file name must not be empty"})
        try:
            metadata = self._get_file_metadata(file_id, "parents")
            current_parents = [parent for parent in metadata.get("parents", []) if isinstance(parent, str) and parent]
            update_kwargs: dict[str, object] = {
                "fileId": file_id,
                "body": {"name": name} if name is not None else {},
                "fields": _WRITE_RESULT_FIELDS,
                "supportsAllDrives": True,
            }
            if new_parent_id not in current_parents:
                update_kwargs["addParents"] = new_parent_id
            removed_parents = [parent for parent in current_parents if parent != new_parent_id]
            if removed_parents:
                update_kwargs["removeParents"] = ",".join(removed_parents)
            service = cast("Any", self.service)
            moved_file = service.files().update(**update_kwargs).execute()
            return json.dumps(moved_file)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not move Google Drive file '{file_id}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    async def amove_file(self, file_id: str, new_parent_id: str, name: str | None = None) -> str:
        """Move one Drive file without blocking the async agent loop."""
        return await asyncio.to_thread(self.move_file, file_id, new_parent_id, name=name)

    @authenticate
    def trash_file(self, file_id: str) -> str:
        """Move one Drive file to trash without permanently deleting it."""
        try:
            service = cast("Any", self.service)
            trashed_file = (
                service.files()
                .update(
                    fileId=file_id,
                    body={"trashed": True},
                    fields=_WRITE_RESULT_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
            return json.dumps(trashed_file)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not trash Google Drive file '{file_id}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    async def atrash_file(self, file_id: str) -> str:
        """Trash one Drive file without blocking the async agent loop."""
        return await asyncio.to_thread(self.trash_file, file_id)

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
                    {
                        "error": f"Cannot read {mime_type} as text.{self._download_guidance()}",
                        "file": metadata,
                    },
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
                            "error": f"File is {file_size} bytes, exceeds max_read_size ({self.max_read_size})."
                            f"{self._download_guidance()}",
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

    @authenticate
    def download_file(self, file_id: str, export_format: str | None = None) -> str:
        """Download a Drive file and save it locally, including files in Shared Drives."""
        try:
            service = cast("Any", self.service)
            metadata = self._get_file_metadata(file_id, "id,name,mimeType")
            mime_type = metadata.get("mimeType", "")
            filename = metadata.get("name")
            unsafe_filename_error = _unsafe_drive_filename_error(filename)
            if unsafe_filename_error:
                return json.dumps({"error": unsafe_filename_error, "file": metadata})

            if export_format:
                target_mime = export_format
                ext = mimetypes.guess_extension(export_format) or ""
            elif mime_type in self.DOWNLOAD_EXPORT_TYPES:
                target_mime, ext = self.DOWNLOAD_EXPORT_TYPES[mime_type]
            elif mime_type.startswith(WorkspaceType.WORKSPACE_PREFIX):
                return json.dumps({"error": f"Unsupported Workspace file type for download: {mime_type}"})
            else:
                target_mime = None
                ext = ""

            path = _download_target_path(self.download_dir, cast("str", filename), ext)
            if path is None:
                return json.dumps(
                    {"error": "Google Drive download target escapes the download directory", "file": metadata},
                )
            path.parent.mkdir(parents=True, exist_ok=True)

            if target_mime:
                request = service.files().export_media(fileId=file_id, mimeType=target_mime)
                path.write_bytes(self._download_bytes(request))
                result = {
                    "fileId": file_id,
                    "path": str(path),
                    "status": "exported",
                    "exportMimeType": target_mime,
                    "originalMimeType": mime_type,
                }
            else:
                request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
                with path.open("wb") as file_handle:
                    downloader = MediaIoBaseDownload(file_handle, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                result = {"fileId": file_id, "path": str(path), "status": "downloaded"}
            return json.dumps(result)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            log_error(f"Could not download file '{file_id}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
