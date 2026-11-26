"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import io
import json
import mimetypes
import re
import textwrap
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from google.auth.credentials import CredentialsWithQuotaProject
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from mindroom.atomic_file import atomic_write_file_at
from mindroom.bounded_bytes import ByteLimitExceededError
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit
from mindroom.file_access import AuthorizedFile, resolve_agent_file
from mindroom.llm.media import named_mime_type
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import oauth_credentials_have_scopes
from mindroom.oauth.google import GOOGLE_DRIVE_READ_OAUTH_SCOPES, google_oauth_provider
from mindroom.oauth.service import OAUTH_MISSING_WRITE_SCOPE_REASON, oauth_connection_required
from mindroom.path_confinement import (
    open_directory_within_root,
    resolve_path_within_root,
)
from mindroom.tool_system.agno_toolkits import rename_toolkit_functions, toolkit_function_names
from mindroom.tool_system.metadata import coerce_optional_finite_number

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import BinaryIO

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_WORKSPACE_PREFIX = "application/vnd.google-apps."
_DOCUMENT_MIME_TYPE = f"{_WORKSPACE_PREFIX}document"
_SPREADSHEET_MIME_TYPE = f"{_WORKSPACE_PREFIX}spreadsheet"
_PRESENTATION_MIME_TYPE = f"{_WORKSPACE_PREFIX}presentation"
_DRAWING_MIME_TYPE = f"{_WORKSPACE_PREFIX}drawing"
_SCRIPT_MIME_TYPE = f"{_WORKSPACE_PREFIX}script"
_FOLDER_MIME_TYPE = f"{_WORKSPACE_PREFIX}folder"

_MODEL_FUNCTION_NAME_ALIASES = {
    "list_files": "google_drive_list_files",
    "search_files": "google_drive_search_files",
    "read_file": "google_drive_read_file",
    "download_file": "google_drive_download_file",
    "upload_file": "google_drive_upload_file",
    "update_file": "google_drive_update_file",
    "create_folder": "google_drive_create_folder",
    "move_file": "google_drive_move_file",
    "trash_file": "google_drive_trash_file",
}
_WRITE_FUNCTION_NAMES = ("upload_file", "update_file", "create_folder", "move_file", "trash_file")
_WRITE_RESULT_FIELDS = "id,name,mimeType,modifiedTime,size,parents,trashed,webViewLink"
_DEFAULT_MAX_READ_SIZE = 10 * 1024 * 1024
# Downloads land on the primary's storage volume, so one file must not fill it.
_DEFAULT_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024
_DOWNLOAD_LIMIT_ADVICE = "Raise the google_drive max_download_size setting to download it."
_DOWNLOAD_DIRECTORY = "google-drive-downloads"
# The client holds each chunk in memory; its default chunk is 100 MiB.
_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_UNSAFE_PATH_CHARACTERS = re.compile(r"[\x00-\x1f\x7f/\\]")
# File systems allow 255 bytes per name; this leaves room for an export extension.
_MAX_NAME_BYTES = 200
_MAX_EXTENSION_CHARACTERS = 16
# A file of these types over max_read_size is refused as binary rather than as too large; content decides smaller files,
# since Drive types some text by its extension, such as TypeScript as `video/mp2t` or playlists as audio.
_BINARY_MIME_PREFIXES = ("audio/", "video/", "image/")
_BINARY_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/gzip",
        "application/vnd.rar",
        "application/x-7z-compressed",
        "application/x-bzip2",
        "application/x-gzip",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/x-xz",
        "application/x-zip-compressed",
        "application/zip",
    },
)

_DRIVE_QUERY_INSTRUCTIONS = textwrap.dedent(f"""\
    You have access to Google Drive tools for searching, reading, uploading, and downloading files.

    ## Drive Query Syntax
    Use these operators in search and list query parameters:
    - `name contains 'report'` — files with "report" in the name
    - `name = 'Budget 2025.xlsx'` — exact name match
    - `mimeType = '{_DOCUMENT_MIME_TYPE}'` — Google Docs only
    - `mimeType = '{_SPREADSHEET_MIME_TYPE}'` — Google Sheets only
    - `mimeType = 'application/pdf'` — PDF files only
    - `mimeType = '{_FOLDER_MIME_TYPE}'` — folders only
    - `modifiedTime > '2025-01-01T00:00:00'` — modified after date
    - `'<folder_id>' in parents` — files inside a specific folder
    - `sharedWithMe` — files shared with the user
    - `starred` — starred files
    - Combine with `and` / `or`: `name contains 'report' and mimeType = 'application/pdf'`
    - Trashed files are filtered automatically. Do not add trashed clauses.

    ## Handling Incomplete Results
    If search returns `incompleteSearch: true`, some shared drives could not be searched.
    This is a server-side limitation of the `allDrives` corpus, not a problem with your query.
    - Inform the user that results may be incomplete due to shared drive limitations
    - Do NOT retry the same query — the limitation is server-side
    - For complete results, the user must reconfigure with `corpora="user"` or `corpora="drive"`""")


def _size_limit_finite_error(field_name: str, value: object) -> TypeError | ValueError:
    msg = f"Google Drive {field_name} must be a finite number"
    if isinstance(value, str):
        return ValueError(msg)
    return TypeError(msg)


class _BoundedDownloadWriter:
    """Count download bytes, refusing a chunk before writing it past the download size limit."""

    def __init__(self, output: BinaryIO, max_bytes: float) -> None:
        self._output = output
        self._max_bytes = max_bytes
        self.byte_count = 0

    def write(self, chunk: bytes) -> int:
        if self.byte_count + len(chunk) > self._max_bytes:
            msg = f"Google Drive download exceeds max_download_size ({self._max_bytes} bytes). {_DOWNLOAD_LIMIT_ADVICE}"
            raise ByteLimitExceededError(msg)
        self.byte_count += len(chunk)
        return self._output.write(chunk)


def _download_media(output: _BoundedDownloadWriter, request: object) -> None:
    downloader = MediaIoBaseDownload(output, request, chunksize=_DOWNLOAD_CHUNK_BYTES)
    done = False
    while not done:
        _, done = downloader.next_chunk()


def _is_binary_mime_type(mime_type: str) -> bool:
    return mime_type in _BINARY_MIME_TYPES or (
        mime_type.startswith(_BINARY_MIME_PREFIXES) and mime_type != "image/svg+xml"
    )


def _path_component(name: str) -> str:
    """Return a Drive name or ID as one file name: path separators and control characters become `_`.

    A name too long for the file system is shortened, keeping its extension.
    """
    component = _UNSAFE_PATH_CHARACTERS.sub("_", name).strip()
    if component in {"", ".", ".."}:
        return "file"
    if len(component.encode()) <= _MAX_NAME_BYTES:
        return component
    suffix = Path(component).suffix
    # A dot inside a long title, as in "Plan v1.2 final ...", starts no extension.
    if len(suffix) > _MAX_EXTENSION_CHARACTERS:
        suffix = ""
    stem_budget = _MAX_NAME_BYTES - len(suffix.encode())
    return component.removesuffix(suffix).encode()[:stem_budget].decode(errors="ignore") + suffix


def _download_path(workspace_root: Path, file_id: str, filename: str, extension: str) -> Path | None:
    """Return a file's workspace-relative `google-drive-downloads/<file id>/<name>`, or None when a link is on the way."""
    name = _path_component(filename)
    if extension and not name.lower().endswith(extension.lower()):
        name += extension
    path = Path(_DOWNLOAD_DIRECTORY, _path_component(file_id), name)
    try:
        resolve_path_within_root(workspace_root, path, symlinks="reject")
    except ValueError:
        return None
    return path


@contextmanager
def _open_upload_media(authorized: AuthorizedFile, mime_type: str) -> Iterator[MediaIoBaseUpload]:
    """Stream the authorized file in resumable chunks, opened without following links swapped in after the check.

    Non-resumable uploads would read the whole file into memory to build the request body.
    """
    with authorized.open() as file:
        yield MediaIoBaseUpload(file, mimetype=mime_type, resumable=True)


class GoogleDriveTools(GoogleApiToolkit):
    """Google Drive toolkit backed by MindRoom-scoped OAuth credentials."""

    _oauth_provider = google_oauth_provider("google_drive")
    _oauth_tool_name = "google_drive"
    _google_api_name = "drive"
    _google_api_version = "v3"

    TEXT_EXPORT_TYPES: ClassVar[dict[str, str]] = {
        _DOCUMENT_MIME_TYPE: "text/plain",
        _SPREADSHEET_MIME_TYPE: "text/csv",
        _PRESENTATION_MIME_TYPE: "text/plain",
        _SCRIPT_MIME_TYPE: "application/json",
    }
    # Google Vids is intentionally absent: files.export refuses it with fileNotExportable.
    DOWNLOAD_EXPORT_TYPES: ClassVar[dict[str, tuple[str, str]]] = {
        _DOCUMENT_MIME_TYPE: (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".docx",
        ),
        _SPREADSHEET_MIME_TYPE: (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
        _PRESENTATION_MIME_TYPE: (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pptx",
        ),
        _DRAWING_MIME_TYPE: ("image/png", ".png"),
        _SCRIPT_MIME_TYPE: ("application/vnd.google-apps.script+json", ".json"),
    }
    SEARCH_FIELDS: ClassVar[str] = (
        "nextPageToken, incompleteSearch, "
        "files(id, name, mimeType, modifiedTime, size, parents, description, "
        "webViewLink, webContentLink, owners(displayName, emailAddress))"
    )
    READ_METADATA_FIELDS: ClassVar[str] = "id,name,mimeType,modifiedTime,size,webViewLink"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        list_files: bool = True,
        search_files: bool = True,
        read_file: bool = True,
        download_file: object = True,
        write: bool = True,
        max_read_size: object = _DEFAULT_MAX_READ_SIZE,
        max_download_size: object = _DEFAULT_MAX_DOWNLOAD_SIZE,
        quota_project_id: object = None,
        include_trashed: bool = False,
        corpora: str = "allDrives",
        supports_all_drives: bool = True,
        include_items_from_all_drives: bool = True,
        drive_id: str | None = None,
        max_results: int = 20,
        instructions: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        coerced_read_size = self._coerce_size_limit("max_read_size", max_read_size)
        self.max_read_size: int | float = _DEFAULT_MAX_READ_SIZE if coerced_read_size is None else coerced_read_size
        coerced_download_size = self._coerce_size_limit("max_download_size", max_download_size)
        self.max_download_size: int | float = (
            _DEFAULT_MAX_DOWNLOAD_SIZE if coerced_download_size is None else coerced_download_size
        )

        if tool_output_workspace_root is None:
            if download_file:
                logger.warning(
                    "Google Drive downloads are disabled because this toolkit was built without a workspace root",
                )
            download_enabled = False
        else:
            download_enabled = bool(download_file)

        resolved_quota_project_id = quota_project_id or runtime_paths.env_value("GOOGLE_CLOUD_QUOTA_PROJECT_ID")
        if resolved_quota_project_id is not None and not isinstance(resolved_quota_project_id, str):
            msg = "Google Drive quota_project_id must be a string"
            raise TypeError(msg)
        self.quota_project_id: str | None = resolved_quota_project_id

        self.include_trashed = include_trashed
        self.corpora = corpora
        self.supports_all_drives = supports_all_drives
        self.include_items_from_all_drives = include_items_from_all_drives
        self.drive_id = drive_id
        self.max_results = max_results
        self._file_access = file_access
        self._workspace_root = tool_output_workspace_root

        tools: list[Callable[..., str]] = []
        if list_files:
            tools.append(self.list_files)
        if search_files:
            tools.append(self.search_files)
        if read_file:
            tools.append(self.read_file)
        if download_enabled:
            tools.append(self.download_file)
        if write:
            tools.extend([self.upload_file, self.update_file, self.create_folder, self.move_file, self.trash_file])

        super().__init__(
            name="google_drive",
            tools=tools,
            instructions=_DRIVE_QUERY_INSTRUCTIONS if instructions is None else instructions,
            quota_project_id=self.quota_project_id,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )
        self._wrap_write_scope_entrypoints()
        rename_toolkit_functions(self, _MODEL_FUNCTION_NAME_ALIASES)
        self._download_entrypoint = self.functions.get(_MODEL_FUNCTION_NAME_ALIASES["download_file"])

    def _build_service(self, creds: Any) -> Any:  # noqa: ANN401
        """Build Drive without cloning MindRoom's tracked OAuth credential."""
        credentials = creds
        if credentials is None:
            msg = "Google Drive credentials are missing"
            raise RuntimeError(msg)
        if (
            self.quota_project_id
            and type(credentials) is not GoogleOAuthCredentials
            and isinstance(credentials, CredentialsWithQuotaProject)
            and credentials.quota_project_id != self.quota_project_id
        ):
            credentials = credentials.with_quota_project(self.quota_project_id)
            self.creds = credentials
        return build(
            self._google_api_name,
            self._google_api_version,
            http=self._google_authorized_http(credentials),
            cache_discovery=False,
        )

    def _stored_credentials_have_required_scopes(self, token_data: dict[str, Any]) -> bool:
        """Let old read-only grants continue authenticating read operations."""
        return oauth_credentials_have_scopes(token_data, GOOGLE_DRIVE_READ_OAUTH_SCOPES)

    def _write_scope_upgrade_result(self) -> str | None:
        if self._provided_credentials is not None or self._should_fallback_to_original_auth():
            return None
        token_data = self._load_token_data()
        if not token_data or oauth_credentials_have_scopes(token_data, self._oauth_provider.scopes):
            return None
        if not oauth_credentials_have_scopes(token_data, GOOGLE_DRIVE_READ_OAUTH_SCOPES):
            return None

        error = oauth_connection_required(
            self._oauth_credential_context(),
            reason=OAUTH_MISSING_WRITE_SCOPE_REASON,
        )
        return self._structured_auth_failure(error)

    def _wrap_write_scope_entrypoints(self) -> None:
        """Require full Drive scope only for registered write functions."""
        for function_name in _WRITE_FUNCTION_NAMES:
            entrypoint = self.functions.get(function_name)
            if entrypoint is None:
                continue

            @wraps(entrypoint)
            def write_scope_entrypoint(
                *args: object,
                _entrypoint: Callable[..., object] = entrypoint,
                **kwargs: object,
            ) -> object:
                if result := self._write_scope_upgrade_result():
                    return result
                return _entrypoint(*args, **kwargs)

            self.functions[function_name] = write_scope_entrypoint
            setattr(self, function_name, write_scope_entrypoint)

    def _coerce_size_limit(self, field_name: str, value: object) -> int | float | None:
        try:
            return coerce_optional_finite_number(value)
        except OverflowError as exc:
            raise _size_limit_finite_error(field_name, value) from exc
        except TypeError as exc:
            msg = f"Google Drive {field_name} must be a number"
            raise TypeError(msg) from exc
        except ValueError as exc:
            msg = f"Google Drive {field_name} must be a number"
            raise ValueError(msg) from exc

    def _get_file_metadata(self, service: Any, file_id: str, fields: str) -> dict[str, Any]:  # noqa: ANN401
        return cast(
            "dict[str, Any]",
            service.files().get(fileId=file_id, fields=fields, supportsAllDrives=self.supports_all_drives).execute(),
        )

    def _download_bytes(self, request: Any) -> bytes:  # noqa: ANN401
        """Download a Drive API media request into memory via MediaIoBaseDownload."""
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def _resolve_upload_file(self, local_path: str) -> AuthorizedFile:
        return resolve_agent_file(
            local_path,
            workspace_root=self._workspace_root,
            file_access=self._file_access,
            field_name="Google Drive local_path",
        )

    def _download_refusal(self, metadata: dict[str, Any], export_format: str | None = None) -> str | None:
        """Return the error `download_file` gives from a file's metadata, before requesting its content, or None."""
        if not isinstance(metadata.get("name"), str):
            return "Google Drive file metadata is missing a filename"
        file_size = int(metadata.get("size", 0))
        if file_size > self.max_download_size:
            return (
                f"File is {file_size} bytes, exceeds max_download_size ({self.max_download_size}). "
                f"{_DOWNLOAD_LIMIT_ADVICE}"
            )
        mime_type = metadata.get("mimeType", "")
        if (
            not export_format
            and mime_type.startswith(_WORKSPACE_PREFIX)
            and mime_type not in self.DOWNLOAD_EXPORT_TYPES
        ):
            return f"Unsupported Workspace file type for download: {mime_type}"
        return None

    def _offered_download_name(self) -> str | None:
        """Return the active model-facing download function name when offered, even after workspace renaming."""
        if self._download_entrypoint is None:
            return None
        offered = set(toolkit_function_names(self))
        for name, function in self.functions.items():
            if function is self._download_entrypoint and name in offered:
                return name
        return None

    def _read_refusal(self, error: str, metadata: dict[str, Any]) -> str:
        """Refuse a read; name the download function while offered, or quote why it would refuse the file too."""
        if download_name := self._offered_download_name():
            if download_refusal := self._download_refusal(metadata):
                error += f" {download_name} would refuse it too: {download_refusal}"
            else:
                error += f" Use {download_name} instead."
        return json.dumps({"error": error, "file": metadata})

    def _search_files(
        self,
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> str:
        if max_results < 1:
            return json.dumps({"error": "max_results must be greater than 0"})
        try:
            service = self._google_api_service()
            effective_max = min(max_results, self.max_results)
            if self.include_trashed:
                effective_query = query or ""
            elif query:
                effective_query = f"({query}) and trashed=false"
            else:
                effective_query = "trashed=false"
            list_kwargs: dict[str, Any] = {
                "q": effective_query,
                "pageSize": effective_max,
                "orderBy": "modifiedTime desc",
                "fields": self.SEARCH_FIELDS,
                "corpora": self.corpora,
                "supportsAllDrives": self.supports_all_drives,
                "includeItemsFromAllDrives": self.include_items_from_all_drives,
            }
            if self.drive_id:
                list_kwargs["driveId"] = self.drive_id
            if page_token:
                list_kwargs["pageToken"] = page_token
            results = service.files().list(**list_kwargs).execute()
            files = results.get("files", [])
            incomplete = results.get("incompleteSearch", False)
            if incomplete:
                logger.debug("google_drive_incomplete_search", corpora=self.corpora)
            return json.dumps(
                {
                    "query": effective_query,
                    "files": files,
                    "count": len(files),
                    "nextPageToken": results.get("nextPageToken"),
                    "incompleteSearch": incomplete,
                },
            )
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            logger.warning("google_drive_search_failed", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def list_files(
        self,
        query: str | None = None,
        page_size: int = 10,
        page_token: str | None = None,
    ) -> str:
        """List recent files and folders from Google Drive.

        Args:
            query: Optional Drive query to filter results.
            page_size: Number of files to return (default 10).
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string containing file metadata or error message.

        """
        return self._search_files(query=query, max_results=page_size, page_token=page_token)

    def search_files(
        self,
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> str:
        """Search Google Drive using a query expression.

        Searches in file name, type, folder, owner, and modification date.

        Args:
            query: Drive query expression (see instructions for syntax).
            max_results: Number of files to return (default 10).
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string containing matching files and metadata or error message.

        """
        return self._search_files(query=query, max_results=max_results, page_token=page_token)

    def upload_file(
        self,
        local_path: str,
        folder_id: str | None = None,
        name: str | None = None,
        mime_type: str | None = None,
        convert_to: Literal["document", "spreadsheet", "presentation"] | None = None,
    ) -> str:
        """Upload one local file, resolving relative paths from the agent workspace.

        With convert_to, Drive converts the file into a Google Workspace file instead of storing it as it is:
        "document" makes a Google Doc from HTML, Markdown, Word, or plain text, importing the formatting Docs
        supports, such as headings, tables, lists, links, and the images a Word file embeds; "spreadsheet" a
        Google Sheet from Excel or CSV; "presentation" Google Slides from PowerPoint.
        The converted file takes the local file's name without its extension unless name is given.
        """
        try:
            authorized = self._resolve_upload_file(local_path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        resolved_mime_type = mime_type or named_mime_type(authorized.name) or "application/octet-stream"
        body: dict[str, object] = {"name": name or authorized.name}
        if folder_id:
            body["parents"] = [folder_id]
        if convert_to:
            body["name"] = name or Path(authorized.name).stem
            body["mimeType"] = f"{_WORKSPACE_PREFIX}{convert_to}"
        try:
            service = self._google_api_service()
            with _open_upload_media(authorized, resolved_mime_type) as media_body:
                uploaded_file = (
                    service.files()
                    .create(
                        body=body,
                        media_body=media_body,
                        fields=_WRITE_RESULT_FIELDS,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            return json.dumps(uploaded_file)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            logger.warning("google_drive_upload_failed", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def update_file(self, file_id: str, local_path: str, mime_type: str | None = None) -> str:
        """Replace the contents of one Drive file from the agent workspace, keeping its ID, link, and sharing.

        A Google Doc, Sheet, or Slides file is replaced whole by Drive's conversion of the local file, as an upload's
        convert_to converts it, so formatting that is not in the local file is lost and comments on the replaced
        content lose their place.
        To change part of such a file, use the Google Docs or Google Sheets tool instead.
        """
        try:
            authorized = self._resolve_upload_file(local_path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            resolved_mime_type = mime_type or named_mime_type(authorized.name) or "application/octet-stream"
            service = self._google_api_service()
            with _open_upload_media(authorized, resolved_mime_type) as media_body:
                updated_file = (
                    service.files()
                    .update(
                        fileId=file_id,
                        media_body=media_body,
                        fields=_WRITE_RESULT_FIELDS,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            return json.dumps(updated_file)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            logger.warning("google_drive_update_failed", file_id=file_id, error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Create a Google Drive folder, optionally under one parent folder."""
        if not name.strip():
            return json.dumps({"error": "Google Drive folder name must not be empty"})
        body: dict[str, object] = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
        if parent_id:
            body["parents"] = [parent_id]
        try:
            service = self._google_api_service()
            folder = service.files().create(body=body, fields=_WRITE_RESULT_FIELDS, supportsAllDrives=True).execute()
            return json.dumps(folder)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            logger.warning("google_drive_create_folder_failed", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def move_file(self, file_id: str, new_parent_id: str, name: str | None = None) -> str:
        """Move one Drive file to a new parent and optionally rename it."""
        if not new_parent_id.strip():
            return json.dumps({"error": "Google Drive new_parent_id must not be empty"})
        if name is not None and not name.strip():
            return json.dumps({"error": "Google Drive file name must not be empty"})
        try:
            service = self._google_api_service()
            metadata = self._get_file_metadata(service, file_id, "parents")
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
            moved_file = service.files().update(**update_kwargs).execute()
            return json.dumps(moved_file)
        except HttpError as exc:
            return json.dumps({"error": f"Google Drive API error: {exc}"})
        except Exception as exc:
            logger.warning("google_drive_move_failed", file_id=file_id, error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def trash_file(self, file_id: str) -> str:
        """Move one Drive file to trash without permanently deleting it."""
        try:
            service = self._google_api_service()
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
            logger.warning("google_drive_trash_failed", file_id=file_id, error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def read_file(self, file_id: str) -> str:
        """Read a Drive file and return its text content, including files in Shared Drives."""
        try:
            service = self._google_api_service()
            metadata = self._get_file_metadata(service, file_id, self.READ_METADATA_FIELDS)
            mime_type = metadata.get("mimeType", "")

            if mime_type in self.TEXT_EXPORT_TYPES:
                export_mime = self.TEXT_EXPORT_TYPES[mime_type]
            elif mime_type.startswith(_WORKSPACE_PREFIX):
                return self._read_refusal(f"Cannot read {mime_type} as text.", metadata)
            else:
                export_mime = None

            if export_mime:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
                content_bytes = self._download_bytes(request)
            else:
                file_size = int(metadata.get("size", 0))
                if file_size > self.max_read_size:
                    error = (
                        f"Cannot read {mime_type} as text."
                        if _is_binary_mime_type(mime_type)
                        else f"File is {file_size} bytes, exceeds max_read_size ({self.max_read_size})."
                    )
                    return self._read_refusal(error, metadata)
                request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
                content_bytes = self._download_bytes(request)

            # Content with a NUL byte is binary, as git also judges files: images and zip-based Office files
            # always contain NULs, and would reach the model as garbage, while 8-bit text in any encoding has
            # none. A PDF can be NUL-free when its streams are uncompressed, so its signature counts too.
            # UTF-16 text is refused as well.
            if content_bytes.startswith(b"%PDF-") or b"\x00" in content_bytes:
                return self._read_refusal(f"Cannot read {mime_type} as text.", metadata)
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
            logger.warning("google_drive_read_failed", file_id=file_id, error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def download_file(self, file_id: str, export_format: str | None = None) -> str:
        """Save a Drive file, such as audio, video, an image, a PDF, or an archive, into the agent workspace.

        Returns the saved path, its size in bytes, and its MIME type. Each Drive file gets its own
        google-drive-downloads/<file_id>/ directory, so files with the same name never overwrite each other,
        while downloading the same file again under the same name replaces its earlier copy. Google Workspace
        files are exported to a native format such as .docx or .xlsx.

        Args:
            file_id: The Drive file ID.
            export_format: MIME type to export a Google Workspace file to instead, such as application/pdf.

        """
        try:
            if self._workspace_root is None:
                return json.dumps({"error": "Google Drive downloads require an agent workspace"})
            service = self._google_api_service()
            metadata = self._get_file_metadata(service, file_id, "id,name,mimeType,size")
            mime_type = metadata.get("mimeType", "")
            refusal = self._download_refusal(metadata, export_format)
            if refusal:
                return json.dumps({"error": refusal, "file": metadata})

            target_mime, ext = self.DOWNLOAD_EXPORT_TYPES.get(mime_type, (None, ""))
            if export_format:
                target_mime = export_format
                ext = mimetypes.guess_extension(export_format) or ""

            path = _download_path(self._workspace_root, file_id, metadata["name"], ext)
            if path is None:
                return json.dumps(
                    {"error": f"A link in the {_DOWNLOAD_DIRECTORY} path blocks this download", "file": metadata},
                )
            with (
                open_directory_within_root(self._workspace_root, path.parent, create=True) as directory_fd,
                atomic_write_file_at(directory_fd, path.name) as file_handle,
            ):
                # Exports carry no size in their metadata, so the limit also holds while bytes arrive.
                output = _BoundedDownloadWriter(file_handle, self.max_download_size)
                if target_mime:
                    request = service.files().export_media(fileId=file_id, mimeType=target_mime)
                else:
                    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
                _download_media(output, request)
            result: dict[str, object] = {
                "fileId": file_id,
                "name": metadata["name"],
                "path": str(self._workspace_root.absolute() / path),
                "size": output.byte_count,
                "mimeType": target_mime or mime_type,
                "status": "exported" if target_mime else "downloaded",
            }
            if target_mime:
                result["originalMimeType"] = mime_type
            return json.dumps(result)
        except HttpError as exc:
            error = f"Google Drive API error: {exc}"
        except ByteLimitExceededError as exc:
            error = str(exc)
        except Exception as exc:
            logger.warning("google_drive_download_failed", file_id=file_id, error_type=type(exc).__name__)
            error = f"Unexpected error: {type(exc).__name__}: {exc}"
        return json.dumps({"error": error})
