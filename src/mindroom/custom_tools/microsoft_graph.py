"""Microsoft Graph client shared by the Microsoft 365 tools.

Every request authenticates with the requester's own delegated token and goes only to Microsoft Graph.
Graph answers a content download with a redirect to a short-lived SharePoint URL; httpx drops the
Authorization header when a redirect leaves Graph, so the token never reaches another host.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from contextlib import ExitStack, contextmanager
from html import escape
from html.parser import HTMLParser
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx

from mindroom.atomic_file import atomic_write_file_at
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.file_access import UploadSourceError, open_upload_source
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.microsoft import microsoft_token_url
from mindroom.oauth.providers import (
    OAuthConnectionRequired,
    OAuthProviderError,
    oauth_connection_required_payload,
)
from mindroom.oauth.service import (
    OAUTH_ACCESS_REJECTED_REASON,
    OAUTH_REFRESH_REJECTED_REASON,
    current_oauth_access_token,
    oauth_connection_required,
)
from mindroom.path_confinement import open_directory_within_root, resolve_path_within_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from contextlib import AbstractContextManager
    from pathlib import Path
    from typing import BinaryIO

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.file_access import AuthorizedFile
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

GRAPH_URL = "https://graph.microsoft.com/v1.0"
_GRAPH_HOST = "graph.microsoft.com"
# Windows zone names ("W. Europe Standard Time", "Central Standard Time (Mexico)", "UTC-11") and IANA names
# ("America/New_York"); the name only goes into JSON bodies, never a URL or OData string.
_TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9 ._+/()-]{0,63}")
_DEFAULT_TIMEOUT_SECONDS = 60.0
_CHUNK_BYTES = 1024 * 1024
_OFFSET = re.compile(r"[0-9]{1,10}")
_KEY_SEGMENT = re.compile(r"\('([^/]+)'\)(?=/|$)")
_USER_PREFIX = re.compile(r"^/users/[^/]+")
# One email address or sign-in name; a guest's sign-in name holds `#EXT#`.
_MAILBOX = re.compile(r'[^@\s<>,;"/\\?]+@[^@\s<>,;"/\\?]+\.[^@\s<>,;"/\\?]+')
_MAX_MAILBOX_CHARACTERS = 254
# The mailbox a next link starts with, however Graph names it: /me, /users/{address or id}, or users({key}).
_LINK_MAILBOX = re.compile(r"^/(?:me|users/[^/]+|users\([^/]*\))(?=/)", re.IGNORECASE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_GRAPH_MESSAGE_CHARACTERS = 300
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


def graph_int(value: int, field_name: str, *, maximum: int, minimum: int = 1) -> int:
    """Return a model-supplied integer argument within [minimum, maximum]."""
    if not minimum <= value <= maximum:
        msg = f"{field_name} must be an integer from {minimum} to {maximum}."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value


def required_text(value: str, field_name: str) -> str:
    """Return a model-supplied text argument unchanged, refusing one that is empty or only whitespace."""
    if not value.strip():
        raise MicrosoftGraphError(code="invalid_argument", message=f"{field_name} must not be empty.")
    return value


def time_zone_name(value: str) -> str:
    """Return a model-supplied Windows or IANA time zone name for a Graph `dateTimeTimeZone`."""
    if not _TIME_ZONE.fullmatch(value) or value != value.strip():
        msg = 'time_zone must be a Windows or IANA time zone name, such as "Pacific Standard Time" or "Europe/Berlin".'
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value


def graph_object(value: object) -> dict[str, Any]:
    """Return a value Graph sent as a JSON object, or an empty object for anything else."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def graph_records(payload: Mapping[str, Any], field: str = "value") -> list[dict[str, Any]]:
    """Return the JSON objects in one list field of a Graph payload, skipping anything else."""
    return [record for record in payload.get(field) or [] if isinstance(record, dict)]


def graph_time(value: object) -> str | None:
    """Return a Graph dateTimeTimeZone as ISO 8601, ending in Z for UTC and naming any other zone."""
    fields = graph_object(value)
    moment, zone = fields.get("dateTime"), fields.get("timeZone")
    if not isinstance(moment, str):
        return None
    moment = moment.split(".", 1)[0]
    return f"{moment}Z" if zone in {None, "UTC"} else f"{moment} ({zone})"


def next_page_token(payload: Mapping[str, Any]) -> str | None:
    """Return the continuation token for a Graph collection page: its next link relative to Graph, or None."""
    link = payload.get("@odata.nextLink")
    if isinstance(link, str) and link.startswith(f"{GRAPH_URL}/"):
        return link.removeprefix(GRAPH_URL)
    return None


def page_path(page_token: str, collection: str) -> str:
    """Return the Graph path a page token continues, refusing anything outside the caller's own collection.

    `collection` is a regular expression the token's path must match in full, ignoring case as Graph does.
    Graph may phrase a next link differently from the first request: an OData key segment such as
    `mailFolders('{id}')` is matched as `mailFolders/{id}`, a leading `/users/{id}` as the `/me` it stands for,
    and `collection` names any other form, such as a drive path instead of `/me/drive`.
    The token must also carry a query and no `..`; httpx sends a path's percent escapes unchanged and Graph may
    decode `%2e` to a dot, so the path may hold none.
    """
    path, _, query = page_token.partition("?")
    canonical_path = _USER_PREFIX.sub("/me", _KEY_SEGMENT.sub(r"/\1", path), count=1)
    if (
        not query
        or not path.startswith("/")
        or path.startswith("//")
        or not re.fullmatch(collection, canonical_path, re.IGNORECASE)
        or ".." in path
        or "%2e" in path.lower()
        or any(character in "#\\ " or ord(character) < 32 for character in page_token)
    ):
        msg = "page_token must be the next_page_token this function returned."
        raise MicrosoftGraphError(code="invalid_page_token", message=msg)
    return page_token


def page_offset(page_token: str | None, maximum: int) -> int:
    """Return the result offset a numeric page token names, or 0 for the first page."""
    if page_token is None:
        return 0
    if not _OFFSET.fullmatch(page_token) or int(page_token) > maximum:
        msg = "page_token must be the next_page_token this function returned."
        raise MicrosoftGraphError(code="invalid_page_token", message=msg)
    return int(page_token)


def mailbox_path(value: str | None, field_name: str) -> str:
    """Return the Graph path of the requester's mailbox, or of another user's that the call names in `field_name`.

    Another mailbox is named by one printable email address or sign-in name, without padding; a guest's sign-in
    name holds `#EXT#`, which Graph takes with each `#` percent-encoded.
    """
    if value is None:
        return "/me"
    if len(value) > _MAX_MAILBOX_CHARACTERS or not value.isprintable() or not _MAILBOX.fullmatch(value):
        msg = f"{field_name} must be an email address or sign-in name, such as kim@contoso.com."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return f"/users/{quote(value, safe='!:@=')}"


def mailbox_next_page_token(payload: Mapping[str, Any], mailbox: str) -> str | None:
    """Return the next page token of a listing in `mailbox`, a `mailbox_path`, naming that mailbox as the call did."""
    token = next_page_token(payload)
    return None if token is None else _LINK_MAILBOX.sub(lambda _match: mailbox, token, count=1)


def mailbox_page_path(page_token: str, collection: str, mailbox: str, field_name: str) -> str:
    """Return the Graph path a page token continues, as `page_path` does, only in the call's own mailbox.

    `mailbox` is the call's `mailbox_path`, and `field_name` the parameter that names it.
    """
    token = page_path(page_token, collection)
    if not token.lower().startswith(f"{mailbox.lower()}/"):
        msg = f"page_token must be the next_page_token this function returned for the same {field_name}."
        raise MicrosoftGraphError(code="invalid_page_token", message=msg)
    return token


def share_id(url: str) -> str:
    """Encode a sharing or web link as a Graph share ID."""
    return "u!" + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


_HTML_BLOCK_TAGS = frozenset(
    {"blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "table", "tr", "ul"},
)
_HTML_CELL_TAGS = frozenset({"td", "th"})
_HTML_HIDDEN_TAGS = frozenset({"head", "script", "style", "title"})
# Text collapses its whitespace to spaces, so a tab in the collected text can only be a cell boundary.
_HTML_CELL_BREAK = "\t"


class _HtmlText(HTMLParser):
    """Collect an HTML fragment's visible text.

    Lines break at `br`, rows, and block elements, cells on a row are marked apart, images show as `[image]`,
    emoji keep their alt text, and quoted lines start with `> ` for each quote level.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._line_start = True
        self._hidden = 0
        self._quote_depth = 0

    def _break_line(self) -> None:
        self.parts.append("\n")
        self._line_start = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_HIDDEN_TAGS:
            self._hidden += 1
        elif tag == "body":
            # The closing `head` tag is optional, so the body ends anything hidden before it.
            self._hidden = 0
        elif tag == "br" or (tag in _HTML_BLOCK_TAGS and not self._line_start):
            self._break_line()
        elif tag in _HTML_CELL_TAGS and not self._line_start:
            self.parts.append(_HTML_CELL_BREAK)
        elif tag == "img":
            self.handle_data("[image]")
        elif tag == "emoji":
            # Teams sends an emoji as an empty element whose alt attribute is the emoji itself.
            self.handle_data(dict(attrs).get("alt") or "")
        if tag == "blockquote":
            self._quote_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_HIDDEN_TAGS:
            self._hidden = max(self._hidden - 1, 0)
        elif tag in _HTML_BLOCK_TAGS and not self._line_start:
            self._break_line()
        if tag == "blockquote":
            self._quote_depth = max(self._quote_depth - 1, 0)

    def handle_data(self, data: str) -> None:
        # Source whitespace, line breaks included, renders as one space, and none at the start of a line.
        text = re.sub(r"\s+", " ", data)
        if self._line_start:
            text = text.lstrip()
        if not self._hidden and text:
            self.parts.append("> " * self._quote_depth + text if self._line_start else text)
            self._line_start = False


def html_to_text(html: str) -> str:
    """Return the readable text of an HTML body: tags dropped, entities decoded, line breaks and quotes kept.

    Cells on a table row are separated by ` | `, images show as `[image]`, and quoted lines start with `> ` for each
    quote level.
    """
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    lines = "".join(parser.parts).split("\n")
    return "\n".join(" ".join(line.replace(_HTML_CELL_BREAK, " | ").split()) for line in lines).strip("\n")


def text_to_html(text: str) -> str:
    """Return plain text as an HTML body that keeps its text and line breaks and shows any markup in it literally."""
    return escape("\n".join(text.splitlines())).replace("\n", "<br>")


def _unsafe_filename(name: str) -> bool:
    if not name.strip() or name in {".", ".."} or "\x00" in name or "/" in name or "\\" in name:
        return True
    windows_name = PureWindowsPath(name)
    return bool(windows_name.drive or windows_name.root)


@contextmanager
def graph_upload_errors() -> Iterator[None]:
    """Raise a refused upload file as the Graph error the Microsoft tools render, with its code and message."""
    try:
        yield
    except UploadSourceError as exc:
        raise MicrosoftGraphError(code=exc.code, message=str(exc)) from None


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


def _graph_error_details(response: httpx.Response) -> dict[str, object]:
    """Return Graph's error code and, when Graph gives one, its reason as one short line of plain text.

    The reason is untrusted text: control characters become spaces, links become `<url>`, whitespace collapses,
    and anything past a few hundred characters is cut.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    details: dict[str, object] = {"graph_code": code if isinstance(code, str) else None}
    message = error.get("message")
    if isinstance(message, str):
        printable = "".join(" " if unicodedata.category(character)[0] == "C" else character for character in message)
        text = " ".join(_URL.sub("<url>", printable).split())
        if len(text) > _GRAPH_MESSAGE_CHARACTERS:
            text = text[: _GRAPH_MESSAGE_CHARACTERS - 1].rstrip() + "…"
        if text:
            details["graph_message"] = text
    return details


def response_object(response: httpx.Response) -> dict[str, Any]:
    """Return a Graph response's JSON object, refusing a body that is not one."""
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
            raise MicrosoftGraphError(code=code, message=message, **_graph_error_details(response))
        raise MicrosoftGraphError(
            code="graph_error",
            message="Microsoft Graph rejected the request.",
            status_code=response.status_code,
            **_graph_error_details(response),
        )

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        """Send one request and return its successful response."""
        return self.check(self.client.request(method, path, **kwargs))

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Send one request and return its JSON object, or an empty object for a response without a body."""
        response = self.request(method, path, **kwargs)
        return response_object(response) if response.content else {}

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
            with graph_upload_errors():
                handle, size = stack.enter_context(open_upload_source(source, max_bytes))
            # An explicit length avoids chunked encoding.
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

    def refresh_failed(exc: OAuthProviderError) -> MicrosoftGraphError:
        logger.warning("microsoft_oauth_refresh_failed", provider_id=provider.id, error_type=type(exc).__name__)
        msg = "Microsoft authorization could not be refreshed. Retry this request shortly."
        return MicrosoftGraphError(code="oauth_refresh_failed", message=msg)

    token, credentials = current_oauth_access_token(context, refresh_failed)
    if credentials.get("token_uri") != token_url:
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


def _graph_tool_result(tool_name: str, operation: Callable[[], Mapping[str, object]]) -> str:
    """Run one tool operation and render it, its connect prompt, or its error as the tool's JSON payload."""
    try:
        fields = operation()
    except OAuthConnectionRequired as exc:
        return custom_tool_payload(tool_name, "error", **oauth_connection_required_payload(exc))
    except MicrosoftGraphError as exc:
        return graph_error_payload(tool_name, exc)
    return custom_tool_payload(tool_name, "ok", **fields)


def graph_call[T](
    tool_name: str,
    session: Callable[[], AbstractContextManager[GraphSession]],
    build: Callable[[], T],
    run: Callable[[GraphSession, T], Mapping[str, object]],
) -> str:
    """Refuse malformed arguments before any request, then send the requests and render their result.

    `build` checks the arguments and returns what `run` sends in one session; its refusal opens no session.
    """
    try:
        request = build()
    except MicrosoftGraphError as exc:
        return graph_error_payload(tool_name, exc)

    def operation() -> Mapping[str, object]:
        with session() as opened:
            return run(opened, request)

    return _graph_tool_result(tool_name, operation)


def mailbox_tool_result(
    tool_name: str,
    field_name: str,
    mailbox: str | None,
    operation: Callable[[], Mapping[str, object]],
) -> str:
    """Run one operation, as `_graph_tool_result` does, for a call that may name another user's mailbox.

    A malformed mailbox is refused before the operation runs; once the call names a valid one, every result and
    Graph error repeats it as `field_name`, while a connect prompt, which does not depend on the mailbox, does not.
    """
    try:
        mailbox_path(mailbox, field_name)
    except MicrosoftGraphError as exc:
        return graph_error_payload(tool_name, exc)
    named = {} if mailbox is None else {field_name: mailbox}

    def run() -> Mapping[str, object]:
        try:
            return {**operation(), **named}
        except MicrosoftGraphError as exc:
            exc.details.update(named)
            raise

    return _graph_tool_result(tool_name, run)
