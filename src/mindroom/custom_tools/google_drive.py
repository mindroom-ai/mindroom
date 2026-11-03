"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import mimetypes
import re
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from agno.tools.google.drive import GoogleDriveTools as AgnoGoogleDriveTools
from agno.tools.google.drive import MediaIoBaseDownload, WorkspaceType, authenticate
from agno.utils.log import log_error
from google.auth.credentials import CredentialsWithQuotaProject
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from mindroom.atomic_file import atomic_write_file_at
from mindroom.bounded_bytes import ByteLimitExceededError
from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin
from mindroom.file_access import AuthorizedFile, resolve_agent_file
from mindroom.llm.media import named_mime_type
from mindroom.logging_config import get_logger
from mindroom.oauth.agno_compat_google_auth import AgnoGoogleAuthBindingMixin
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.credential_lifecycle import oauth_credentials_have_scopes
from mindroom.oauth.google import GOOGLE_DRIVE_READ_OAUTH_SCOPES, GOOGLE_DRIVE_WRITE_SCOPE, google_oauth_provider
from mindroom.oauth.service import (
    OAUTH_MISSING_WRITE_SCOPE_REASON,
    oauth_connection_required,
)
from mindroom.path_confinement import (
    open_directory_within_root,
    resolve_path_within_root,
)
from mindroom.tool_system.agno_toolkits import rename_toolkit_functions, toolkit_function_names
from mindroom.tool_system.metadata import coerce_optional_finite_number

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import BinaryIO

    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

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
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_GOOGLE_WORKSPACE_MIME_PREFIX = "application/vnd.google-apps."
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


class GoogleDriveTools(
    ScopedOAuthClientMixin,
    AgnoGoogleAuthBindingMixin,
    ThreadLocalGoogleServiceMixin,
    AgnoGoogleDriveTools,
):
    """Google Drive toolkit that uses MindRoom-scoped OAuth credentials."""

    _oauth_provider = google_oauth_provider("google_drive")
    _oauth_tool_name = "google_drive"
    # AGNO_COMPAT: Drive `download_file` exports Google Vids, which the Drive API never exports.
    # Reason: Agno 3.0.9 maps `application/vnd.google-apps.vid` to a `video/mp4` export, but `files.export` refuses
    # every Vids file with `fileNotExportable`, so a Vids download, and the download advice a refused read gives,
    # end in an API error. Without the mapping, MindRoom refuses Vids as an unsupported Workspace type.
    # Upstream issue: Tracking gap; no matching issue identified.
    # Upstream PR: None identified.
    # Remove when: Agno's `DOWNLOAD_EXPORT_TYPES` no longer maps Google Vids to a `files.export` format.
    # Coverage: tests/test_google_drive_oauth_tool.py::test_google_drive_read_refusal_advises_download_only_for_files_the_download_saves.
    DOWNLOAD_EXPORT_TYPES: ClassVar[dict[str, tuple[str, str]]] = {
        mime_type: export
        for mime_type, export in AgnoGoogleDriveTools.DOWNLOAD_EXPORT_TYPES.items()
        if mime_type != WorkspaceType.VID
    }

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        write: bool = True,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GoogleDriveTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        if "max_read_size" in kwargs:
            max_read_size = self._coerce_size_limit("max_read_size", kwargs["max_read_size"])
            if max_read_size is None:
                kwargs.pop("max_read_size")
            else:
                kwargs["max_read_size"] = max_read_size
        max_download_size = self._coerce_size_limit("max_download_size", kwargs.pop("max_download_size", None))
        self.max_download_size = _DEFAULT_MAX_DOWNLOAD_SIZE if max_download_size is None else max_download_size
        if tool_output_workspace_root is None:
            if kwargs.get("download_file"):
                logger.warning(
                    "Google Drive downloads are disabled because this toolkit was built without a workspace root",
                )
            kwargs["download_file"] = False
        else:
            kwargs.setdefault("download_file", True)
        kwargs["upload_file"] = False
        kwargs.setdefault("scopes", [GOOGLE_DRIVE_WRITE_SCOPE])
        # Search every drive the account can see; Agno reports ``incompleteSearch`` itself.
        kwargs.setdefault("corpora", "allDrives")
        kwargs.setdefault("supports_all_drives", True)
        kwargs.setdefault("include_items_from_all_drives", True)
        quota_project_id = kwargs.get("quota_project_id") or runtime_paths.env_value(
            "GOOGLE_CLOUD_QUOTA_PROJECT_ID",
        )
        if quota_project_id is not None and not isinstance(quota_project_id, str):
            msg = "Google Drive quota_project_id must be a string"
            raise TypeError(msg)
        if quota_project_id is not None:
            kwargs["quota_project_id"] = quota_project_id
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        self._file_access = file_access
        self._workspace_root = tool_output_workspace_root
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            config=runtime_config,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
            quota_project_id=quota_project_id,
        )
        super().__init__(creds=creds, **kwargs)
        # Agno's async variants run Drive calls on the event loop's default executor, which the
        # gateway cannot track; synchronous bodies run on the caller's tool executor instead.
        self.async_functions.clear()
        if write:
            self._register_write_tools()
        self._set_original_auth(AgnoGoogleDriveTools._resolve_creds)
        self._wrap_oauth_function_entrypoints()
        self._wrap_write_scope_entrypoints()
        rename_toolkit_functions(self, _MODEL_FUNCTION_NAME_ALIASES)
        # A rename, such as an additional Google workspace's prefix, renames this function in place.
        self._download_function = self.functions.get(_MODEL_FUNCTION_NAME_ALIASES["download_file"])

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
        return build("drive", "v3", http=self._google_authorized_http(credentials))

    def _register_write_tools(self) -> None:
        write_tools = (
            (self._upload_file, "upload_file"),
            (self.update_file, "update_file"),
            (self.create_folder, "create_folder"),
            (self.move_file, "move_file"),
            (self.trash_file, "trash_file"),
        )
        self.tools = (*self.tools, *(tool for tool, _name in write_tools))
        for tool, name in write_tools:
            self.register(tool, name=name)

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
            and mime_type.startswith(WorkspaceType.WORKSPACE_PREFIX)
            and mime_type not in self.DOWNLOAD_EXPORT_TYPES
        ):
            return f"Unsupported Workspace file type for download: {mime_type}"
        return None

    def _read_refusal(self, error: str, metadata: dict[str, Any]) -> str:
        """Refuse a read; name the download function while offered, or quote why it would refuse the file too."""
        download = self._download_function
        if download is not None and download.name in toolkit_function_names(self):
            if download_refusal := self._download_refusal(metadata):
                error += f" {download.name} would refuse it too: {download_refusal}"
            else:
                error += f" Use {download.name} instead."
        return json.dumps({"error": error, "file": metadata})

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
            authorized = self._resolve_upload_file(local_path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        resolved_mime_type = mime_type or named_mime_type(authorized.name) or "application/octet-stream"
        body: dict[str, object] = {"name": name or authorized.name}
        if folder_id:
            body["parents"] = [folder_id]
        try:
            service = cast("Any", self.service)
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
            log_error(f"Could not upload file '{authorized.display_path}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    @authenticate
    def update_file(self, file_id: str, local_path: str, mime_type: str | None = None) -> str:
        """Replace the contents of one binary Drive file from the agent workspace."""
        try:
            authorized = self._resolve_upload_file(local_path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            metadata = self._get_file_metadata(file_id, "id,name,mimeType")
            existing_mime_type = metadata.get("mimeType")
            if isinstance(existing_mime_type, str) and existing_mime_type.startswith(_GOOGLE_WORKSPACE_MIME_PREFIX):
                return json.dumps(
                    {
                        "error": (
                            "Google Drive content replacement only supports binary files; "
                            f"{existing_mime_type} requires its Google Workspace API"
                        ),
                    },
                )
            resolved_mime_type = mime_type or named_mime_type(authorized.name) or "application/octet-stream"
            service = cast("Any", self.service)
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
            log_error(f"Could not update Google Drive file '{file_id}': {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

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

    # AGNO_COMPAT: Drive `read_file` refuses text by MIME type and names a download function that may not exist.
    # Reason: Agno 3.0.9 refuses every `application/octet-stream` file, including text MindRoom's own uploads
    # store that way, and ends errors with "Use download_file instead." although MindRoom exposes
    # `google_drive_download_file` or disables it, and although its `download_file` refuses folders and other
    # Workspace types it cannot export. This override decides text by content instead, and refuses a binary type
    # over `max_read_size` as binary rather than as too large. It also refuses Office files, which Agno's
    # MIME-based branch would extract without tables, grouped shapes, or line breaks, or reject with an install
    # hint when the extraction packages are missing.
    # Upstream issue: Tracking gap; no matching issue identified.
    # Upstream PR: https://github.com/agno-agi/agno/pull/10473 (in 3.0.11; MindRoom pins 3.0.9) covers only grouped
    # shapes and line breaks in `.pptx` text; https://github.com/agno-agi/agno/pull/10501 (open) covers only `.docx` tables.
    # Remove when: Agno's `read_file` decides text by content rather than MIME type, lets a toolkit word the advice
    # after a refused read, and extracts complete Office text with dependencies MindRoom ships or refuses it;
    # keep MindRoom's function aliases and the advice that quotes `_download_refusal`.
    # Coverage: tests/test_google_drive_oauth_tool.py::test_google_drive_read_refuses_binary_content_and_names_enabled_download_function;
    # tests/test_google_drive_oauth_tool.py::test_google_drive_read_names_no_download_function_its_filters_leave_out;
    # tests/test_google_drive_oauth_tool.py::test_google_drive_read_refusal_advises_download_only_for_files_the_download_saves;
    # tests/test_google_drive_oauth_tool.py::test_google_drive_read_returns_text_without_nul_bytes;
    # tests/test_google_drive_oauth_tool.py::test_google_drive_read_refuses_large_binary_type_as_binary_without_fetching_it.
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
            log_error(f"Could not read Google Drive file {file_id}: {exc}")
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    @authenticate
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
            service = cast("Any", self.service)
            metadata = self._get_file_metadata(file_id, "id,name,mimeType,size")
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
            log_error(f"Could not download file '{file_id}': {exc}")
            error = f"Unexpected error: {type(exc).__name__}: {exc}"
        return json.dumps({"error": error})
