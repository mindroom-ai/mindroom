"""Microsoft Graph client shared by the Microsoft 365 tools.

Every request authenticates with the requester's own delegated token and goes only to Microsoft Graph.
Graph answers a content download with a redirect to a short-lived SharePoint URL; httpx drops the
Authorization header when a redirect leaves Graph, so the token never reaches another host.
"""

from __future__ import annotations

import base64
import os
from contextlib import ExitStack, contextmanager
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from mindroom.atomic_file import atomic_write_file_at
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.file_access import AuthorizedFile, resolve_agent_file
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    oauth_credentials_usable,
    refresh_oauth_credentials_blocking,
)
from mindroom.oauth.microsoft import microsoft_token_url
from mindroom.oauth.providers import (
    OAuthConnectionRequired,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    oauth_connection_required_payload,
)
from mindroom.oauth.service import (
    OAUTH_ACCESS_REJECTED_REASON,
    OAUTH_REFRESH_REJECTED_REASON,
    OAUTH_RESET_REQUIRED_REASON,
    oauth_connection_required,
)
from mindroom.path_confinement import open_directory_within_root, resolve_path_within_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path
    from typing import BinaryIO

    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

GRAPH_URL = "https://graph.microsoft.com/v1.0"
_GRAPH_HOST = "graph.microsoft.com"
_DEFAULT_TIMEOUT_SECONDS = 60.0
_CHUNK_BYTES = 1024 * 1024
_STATUS_ERRORS = {
    403: ("forbidden", "The connected Microsoft account has no permission for this."),
    404: ("not_found", "Microsoft Graph found nothing there, or the connected account cannot see it."),
    409: ("conflict", "The request conflicts with the current state, for example a name that already exists."),
    412: ("etag_mismatch", "The item changed since that eTag; read it again before writing."),
    413: ("too_large", "Microsoft Graph refused the request as too large."),
    423: ("locked", "The item is locked, for example open for editing or checked out; try again later."),
}


class MicrosoftGraphError(Exception):
    """A refused or failed Graph operation, described only by a stable code, a fixed message, and safe details."""

    def __init__(self, *, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def path_segment(value: object, field_name: str) -> str:
    """Return a model-supplied ID as one escaped Graph path segment, refusing anything that changes the path."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or len(value) > 1024
        or any(character in "/\\?#" or ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        msg = f"{field_name} must be a Microsoft Graph ID."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return quote(value, safe="!:@=")


def share_id(url: str) -> str:
    """Encode a sharing or web link as a Graph share ID."""
    return "u!" + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _unsafe_filename(name: str) -> bool:
    if not name.strip() or name in {".", ".."} or "\x00" in name or "/" in name or "\\" in name:
        return True
    windows_name = PureWindowsPath(name)
    return bool(windows_name.drive or windows_name.root)


def checked_upload_source(
    local_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
    max_bytes: int,
    field_name: str = "local_path",
) -> AuthorizedFile:
    """Return the authorized workspace file to upload, refusing it before any request when it is too large."""
    try:
        source = resolve_agent_file(
            local_path,
            workspace_root=workspace_root,
            file_access=file_access,
            field_name=field_name,
        )
    except ValueError as exc:
        raise MicrosoftGraphError(code="invalid_local_path", message=str(exc)) from None
    try:
        with source.open() as handle:
            size = os.fstat(handle.fileno()).st_size
    except OSError as exc:
        raise _unreadable(source, exc) from None
    _check_upload_size(source, size, max_bytes)
    return source


def _unreadable(source: AuthorizedFile, exc: OSError) -> MicrosoftGraphError:
    # A file or workspace replaced by a link after the check is refused here, without following it.
    reason = exc.strerror or type(exc).__name__
    return MicrosoftGraphError(code="invalid_local_path", message=f"{source.display_path} cannot be read: {reason}.")


def _check_upload_size(source: AuthorizedFile, size: int, max_bytes: int) -> None:
    if size > max_bytes:
        msg = f"{source.display_path} is {size} bytes; the limit is {max_bytes} bytes."
        raise MicrosoftGraphError(code="too_large", message=msg)


def _unwritable(directory: str, exc: OSError) -> MicrosoftGraphError:
    # A workspace replaced by a link, an inaccessible directory, a name too long for the disk, or a full disk.
    reason = exc.strerror or type(exc).__name__
    return MicrosoftGraphError(
        code="workspace_unavailable",
        message=f"The download could not be written to {directory}: {reason}.",
    )


def _chunks(handle: BinaryIO) -> Iterator[bytes]:
    while chunk := handle.read(_CHUNK_BYTES):
        yield chunk


def _graph_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "")
    return int(value) if value.isdigit() else None


class GraphSession:
    """One operation's authenticated Graph client and the credential scope it acts for."""

    def __init__(self, client: httpx.Client, context: OAuthCredentialContext) -> None:
        self.client = client
        self.context = context

    def check(self, response: httpx.Response) -> httpx.Response:
        """Return a successful response, or raise the error its status maps to."""
        if response.is_success:
            return response
        response.read()
        if response.url.host != _GRAPH_HOST:
            # The host Graph redirected to refused the download; reconnecting cannot fix that.
            msg = "SharePoint refused the download link Microsoft Graph returned."
            raise MicrosoftGraphError(code="download_rejected", message=msg, status_code=response.status_code)
        if response.status_code == 401:
            raise oauth_connection_required(self.context, reason=OAUTH_ACCESS_REJECTED_REASON)
        if response.status_code == 429:
            msg = "Microsoft Graph is throttling these requests; retry later."
            raise MicrosoftGraphError(code="throttled", message=msg, retry_after_seconds=_retry_after_seconds(response))
        if response.status_code in _STATUS_ERRORS:
            code, message = _STATUS_ERRORS[response.status_code]
            raise MicrosoftGraphError(code=code, message=message, graph_code=_graph_error_code(response))
        raise MicrosoftGraphError(
            code="graph_error",
            message="Microsoft Graph rejected the request.",
            status_code=response.status_code,
            graph_code=_graph_error_code(response),
        )

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        """Send one request and return its successful response."""
        return self.check(self.client.request(method, path, **kwargs))

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Send one request and return its JSON object, or an empty object for a response without a body."""
        response = self.request(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise MicrosoftGraphError(
                code="graph_invalid_response",
                message="Microsoft Graph returned a response that is not JSON.",
            )
        return payload

    def save_download(
        self,
        path: str,
        *,
        workspace_root: Path,
        directory: str,
        filename: str,
        max_bytes: int,
    ) -> Path:
        """Stream one Graph content response into the workspace directory, atomically and within `max_bytes`."""
        target = workspace_root.absolute() / directory / filename
        if _unsafe_filename(filename):
            raise MicrosoftGraphError(
                code="unsafe_name",
                message="Microsoft Graph reported an unsafe file name.",
                name=filename,
            )
        try:
            resolve_path_within_root(workspace_root, target, symlinks="reject")
        except ValueError:
            raise MicrosoftGraphError(
                code="unsafe_name",
                message="The download target escapes its directory.",
            ) from None
        except OSError as exc:
            raise _unwritable(directory, exc) from None
        with self.client.stream("GET", path, follow_redirects=True) as response:
            self.check(response)
            try:
                with (
                    open_directory_within_root(workspace_root, directory, create=True) as directory_fd,
                    atomic_write_file_at(directory_fd, filename) as handle,
                ):
                    remaining = max_bytes
                    for chunk in response.iter_bytes(_CHUNK_BYTES):
                        if len(chunk) > remaining:
                            msg = f"The download exceeds {max_bytes} bytes."
                            raise MicrosoftGraphError(code="too_large", message=msg)
                        remaining -= len(chunk)
                        handle.write(chunk)
            except OSError as exc:
                raise _unwritable(directory, exc) from None
        return target

    def upload(
        self,
        method: str,
        path: str,
        source: AuthorizedFile,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Stream one workspace file as the request body and return Graph's JSON answer."""
        with ExitStack() as stack:
            try:
                handle = stack.enter_context(source.open())
            except OSError as exc:
                raise _unreadable(source, exc) from None
            # Measure the open file itself, which the body streams; an explicit length avoids chunked encoding.
            size = os.fstat(handle.fileno()).st_size
            _check_upload_size(source, size, max_bytes)
            return self.json(
                method,
                path,
                content=_chunks(handle),
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size), **(headers or {})},
            )


def _new_http_client(
    *,
    headers: Mapping[str, str],
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    return httpx.Client(
        base_url=GRAPH_URL,
        headers=dict(headers),
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
    )


def _access_token(provider: OAuthProvider, runtime_paths: RuntimePaths, context: OAuthCredentialContext) -> str:
    """Return a current access token for the requester, refreshing it when it is about to expire."""
    try:
        token_url = microsoft_token_url(runtime_paths)
    except OAuthProviderError as exc:
        raise MicrosoftGraphError(code="invalid_tenant", message=str(exc)) from None
    try:
        credentials = refresh_oauth_credentials_blocking(context)
    except OAuthCredentialUnreadableError:
        raise oauth_connection_required(context, reason=OAUTH_RESET_REQUIRED_REASON) from None
    except OAuthRefreshRejectedError:
        raise oauth_connection_required(context, reason=OAUTH_REFRESH_REJECTED_REASON) from None
    except OAuthProviderError as exc:
        logger.warning("microsoft_oauth_refresh_failed", provider_id=provider.id, error_type=type(exc).__name__)
        msg = "Microsoft authorization could not be refreshed. Retry this request shortly."
        raise MicrosoftGraphError(code="oauth_refresh_failed", message=msg) from None
    token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
    if not oauth_credentials_usable(provider, runtime_paths, credentials) or not isinstance(token, str) or not token:
        raise oauth_connection_required(context)
    if (credentials or {}).get("token_uri") != token_url:
        # The connection was made for another tenant; a tenant change must not leave it usable until refresh.
        raise oauth_connection_required(context, reason=OAUTH_REFRESH_REJECTED_REASON)
    return token


@contextmanager
def graph_session(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    config: Config | None,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[GraphSession]:
    """Open one Graph session as the active requester, mapping transport failures to safe errors."""
    context = active_oauth_credential_context(
        provider,
        runtime_paths,
        credentials_manager,
        worker_target,
        config=config,
    )
    if context.worker_target is None:
        # Requester-scoped credentials never fall back to a shared or installation-level token.
        raise oauth_connection_required(context)
    token = _access_token(provider, runtime_paths, context)
    try:
        with _new_http_client(
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
        ) as client:
            yield GraphSession(client, context)
    except httpx.TimeoutException:
        msg = f"Microsoft Graph did not answer within {timeout_seconds:g} seconds."
        raise MicrosoftGraphError(code="graph_timeout", message=msg) from None
    except httpx.HTTPError as exc:
        # The exception text can name URLs; report only its type.
        msg = "Microsoft Graph could not be reached."
        raise MicrosoftGraphError(code="graph_unreachable", message=msg, error_type=type(exc).__name__) from None


def graph_error_payload(tool_name: str, exc: MicrosoftGraphError) -> str:
    """Render one Graph error as the tool's JSON payload."""
    return custom_tool_payload(tool_name, "error", code=exc.code, message=exc.message, **exc.details)


def graph_tool_result(tool_name: str, operation: Callable[[], Mapping[str, object]]) -> str:
    """Run one tool operation and render it, its connect prompt, or its error as the tool's JSON payload."""
    try:
        fields = operation()
    except OAuthConnectionRequired as exc:
        return custom_tool_payload(tool_name, "error", **oauth_connection_required_payload(exc))
    except MicrosoftGraphError as exc:
        return graph_error_payload(tool_name, exc)
    return custom_tool_payload(tool_name, "ok", **fields)
