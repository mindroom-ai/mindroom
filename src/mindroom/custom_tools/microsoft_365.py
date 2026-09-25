"""Connected OneDrive and SharePoint Excel workbooks, acting as the requester through Microsoft 365 OAuth."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlsplit

from agno.tools import Toolkit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.conversation_notices import ConversationNoticeError, send_conversation_notice
from mindroom.custom_tools.excel_workbooks import (
    apply_edits,
    parse_edits,
    parse_sheet_range,
    read_range,
    workbook_outline,
)
from mindroom.custom_tools.microsoft_graph_client import (
    DocumentRef,
    GraphAccessRejectedError,
    GraphError,
    InvalidArgumentError,
    graph_json,
    graph_object,
    graph_path,
    graph_upload_new,
    resolve_share,
    share_id,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.file_access import resolve_agent_file
from mindroom.logging_config import get_logger
from mindroom.oauth.microsoft import microsoft_365_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired, oauth_connection_required_payload
from mindroom.oauth.requester_access import OAuthRefreshUnavailableError, RequesterOAuthAccess
from mindroom.oauth.service import OAUTH_ACCESS_REJECTED_REASON
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.custom_tools.excel_workbooks import EditReceipt
    from mindroom.file_access import AuthorizedFile
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_DOCUMENT_CONTENT_KEY = "io.mindroom.document"
_TOOL_NAME = "microsoft_365"
_ITEM_FIELDS = "id,name,webUrl,webDavUrl,eTag,size,file,folder,lastModifiedDateTime,lastModifiedBy,parentReference"
_FOLDER_FIELDS = "id,folder,parentReference"
_DEFAULT_FOLDER = "MindRoom"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_NAME_CANDIDATES = 20
_MAX_SUMMARY_CHARS = 500
_MAX_FILENAME_CHARS = 200
_ONEDRIVE_FORBIDDEN_NAME_CHARS = frozenset('"*:<>?/\\|')
_NUMBERED_NAME_PATTERN = re.compile(r"^(?P<stem>.*?)(?: \((?P<number>\d+)\))?$")

type _Operation = Callable[[str], Awaitable[dict[str, object]]]


def _https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme == "https" and parts.hostname and parts.username is None else None


def _location(parent_reference: dict[str, Any]) -> str | None:
    """Return a readable folder path such as ``Finance Team / FY27`` from a percent-encoded parent reference."""
    path = parent_reference.get("path")
    if not isinstance(path, str) or ":" not in path:
        return None
    folders = [unquote(segment) for segment in path.split(":", 1)[1].split("/") if segment]
    return " / ".join(folders) or None


@dataclass(frozen=True, slots=True)
class _ConnectedDocument:
    """One workbook's identity and the revision Graph last reported for it."""

    ref: DocumentRef
    name: str
    web_url: str | None
    file_url: str | None
    location: str | None
    etag: str | None
    modified_at: str | None
    modified_by: str | None

    @classmethod
    def _from_item(cls, item: object) -> _ConnectedDocument:
        """Return the document for a driveItem, accepting only Excel .xlsx files."""
        data = graph_object(item)
        name = data.get("name")
        if data.get("folder") is not None or data.get("file") is None:
            raise GraphError(code="not_a_file", message="The link points to a folder, not a workbook.")
        if not isinstance(name, str) or not name.lower().endswith(".xlsx"):
            raise GraphError(
                code="unsupported_document",
                message="Only Excel workbooks (.xlsx) can be connected.",
                name=name if isinstance(name, str) else None,
            )
        modified_by = graph_object(graph_object(data.get("lastModifiedBy")).get("user")).get("displayName")
        return cls(
            ref=DocumentRef.from_item(data),
            name=name,
            web_url=_https_url(data.get("webUrl")),
            # Office desktop apps open the file's WebDAV path; webUrl is the browser editor.
            file_url=_https_url(data.get("webDavUrl")),
            location=_location(graph_object(data.get("parentReference"))),
            etag=data.get("eTag") if isinstance(data.get("eTag"), str) else None,
            modified_at=data.get("lastModifiedDateTime") if isinstance(data.get("lastModifiedDateTime"), str) else None,
            modified_by=modified_by if isinstance(modified_by, str) else None,
        )

    def summary(self) -> dict[str, object]:
        """Return the fields agents and cards share."""
        return {
            "document_id": self.ref.document_id,
            "name": self.name,
            "kind": "xlsx",
            "web_url": self.web_url,
            "file_url": self.file_url,
            "location": self.location,
            "revision": {"etag": self.etag, "modified_at": self.modified_at, "modified_by": self.modified_by},
        }


def _card_body(event: str, document: _ConnectedDocument, change: dict[str, object] | None) -> str:
    """Return the plain-text card body for clients that do not render document cards."""
    where = f" ({document.location})" if document.location else ""
    lines = {
        "connected": [f"Connected {document.name}{where} to this conversation."],
        "saved": [f"Saved {document.name}{where} to Microsoft 365 and connected it to this conversation."],
        "edited": [f"Edited {document.name}{where}."],
    }[event]
    if change is not None:
        lines.append(str(change["summary"]))
        verified = "verified" if change["verified"] else "NOT verified"
        lines.append(f"{change['cells_changed']} cell(s) changed, {verified}.")
    lines.append(f"document_id: {document.ref.document_id}")
    if document.web_url:
        lines.append(document.web_url)
    return "\n".join(lines)


def _document_card_content(
    event: str,
    document: _ConnectedDocument,
    *,
    requester_id: str,
    agent_user_id: str,
    room_id: str,
    thread_id: str | None,
    change: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Return a document card's text body and its ``io.mindroom.document`` metadata."""
    metadata: dict[str, object] = {
        "version": 1,
        "event": event,
        **document.summary(),
        "requester_id": requester_id,
        "agent_user_id": agent_user_id,
        "room_id": room_id,
        "thread_id": thread_id,
    }
    if change is not None:
        metadata["change"] = change
    return _card_body(event, document, change), metadata


def _sanitized_upload_name(value: str) -> str:
    """Return a OneDrive-safe .xlsx file name or raise."""
    name = unicodedata.normalize("NFC", value).strip()
    if (
        not name
        or len(name) > _MAX_FILENAME_CHARS
        or any(char in _ONEDRIVE_FORBIDDEN_NAME_CHARS or unicodedata.category(char)[0] == "C" for char in name)
        or name.startswith(("~$", "."))
        or name.endswith(".")
    ):
        msg = 'name must be a plain file name without path separators or the characters " * : < > ? \\ |.'
        raise InvalidArgumentError(msg)
    if not name.lower().endswith(".xlsx"):
        msg = "Only Excel workbooks (.xlsx) can be saved."
        raise InvalidArgumentError(msg)
    return name


def _name_candidates(name: str) -> list[str]:
    """Return the name, then ``Stem (2).xlsx`` through ``Stem (20).xlsx``, without repeating the name."""
    path = PurePosixPath(name)
    match = _NUMBERED_NAME_PATTERN.match(path.stem)
    stem = match.group("stem") if match is not None and match.group("stem") else path.stem
    numbered = (f"{stem} ({number}){path.suffix}" for number in range(2, _MAX_NAME_CANDIDATES + 1))
    return [name, *(candidate for candidate in numbered if candidate != name)]


def _read_upload(authorized: AuthorizedFile) -> bytes:
    with authorized.open() as file:
        content = file.read(_MAX_UPLOAD_BYTES + 1)
    if len(content) > _MAX_UPLOAD_BYTES:
        msg = f"The workbook is larger than the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
        raise InvalidArgumentError(msg)
    if not content.startswith(b"PK\x03\x04"):
        msg = "The file is not an Excel .xlsx workbook."
        raise InvalidArgumentError(msg)
    return content


def _folder_ref(item: object) -> DocumentRef:
    data = graph_object(item)
    if data.get("folder") is None:
        raise GraphError(code="not_a_folder", message="The target is not a folder.")
    return DocumentRef.from_item(data)


def _receipt_fields(receipt: EditReceipt) -> dict[str, object]:
    """Return the agent-facing receipt, as an error payload unless every edit applied."""
    fields: dict[str, object] = {
        "result": receipt.status,
        "verified": receipt.verified,
        "cells_changed": receipt.cells_changed,
        "edits": [outcome.as_dict() for outcome in receipt.edits],
    }
    message: str | None = None
    if receipt.outcome_unknown:
        message = "A write failed and its outcome is unknown; read the ranges again before retrying."
    elif receipt.status == "partial":
        message = "Some edits were applied; check each edit's outcome."
    elif receipt.status == "conflict":
        message = (
            "Nothing was written because some ranges changed since they were read. "
            "Read them again and propose new edits."
        )
    elif receipt.status == "failed":
        message = "The first write failed and later edits were not attempted; check each edit's outcome."
    if message is not None:
        fields.update(status="error", code=receipt.status, message=message)
    if receipt.applied and not receipt.verified:
        fields["warning"] = "Excel stored different content than was sent for at least one edit; see its written value."
    return fields


class Microsoft365Tools(Toolkit):
    """Connect, read, and edit OneDrive and SharePoint Excel workbooks as the requesting user."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        if credentials_manager is None:
            msg = "Microsoft 365 tools require an explicit credentials_manager"
            raise RuntimeError(msg)
        self._oauth = RequesterOAuthAccess(
            provider=microsoft_365_oauth_provider(),
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            config=runtime_config,
        )
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        super().__init__(
            name=_TOOL_NAME,
            tools=[
                self.connect_office_document,
                self.save_office_document,
                self.read_office_document,
                self.edit_office_document,
            ],
            # A human confirms every write, even under tool_approval.default: auto_approve,
            # because the model issuing the call may have read untrusted document content.
            requires_confirmation_tools=["edit_office_document"],
        )

    def _payload(self, status: str, **fields: object) -> str:
        return custom_tool_payload(self.name, status, **fields)

    def _error(self, exc: GraphError) -> str:
        return self._payload("error", code=exc.code, message=exc.message, **exc.details)

    async def _call(self, operation: _Operation) -> str:
        """Authenticate as the requester and run one operation, reducing every failure to a safe payload."""
        try:
            token = await self._oauth.access_token()
            fields = await operation(token)
        except OAuthConnectionRequired as exc:
            return self._payload("error", **oauth_connection_required_payload(exc))
        except OAuthRefreshUnavailableError:
            return self._payload(
                "error",
                code="oauth_refresh_failed",
                message="Microsoft 365 authorization could not be refreshed. Retry this request shortly.",
            )
        except GraphAccessRejectedError:
            exc = await self._oauth.connection_required(reason=OAUTH_ACCESS_REJECTED_REASON)
            return self._payload("error", **oauth_connection_required_payload(exc))
        except GraphError as exc:
            return self._error(exc)
        status = cast("str", fields.pop("status", "ok"))
        return self._payload(status, **fields)

    async def _post_card(
        self,
        event: str,
        document: _ConnectedDocument,
        change: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Post a document card into the current conversation and report whether it was posted."""
        context = get_tool_runtime_context()
        if context is None:
            return {"card_posted": False}
        body, metadata = _document_card_content(
            event,
            document,
            requester_id=context.requester_id,
            agent_user_id=context.client.user_id,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            change=change,
        )
        try:
            event_id = await send_conversation_notice(
                context,
                body,
                {_DOCUMENT_CONTENT_KEY: metadata},
                operation="microsoft_365_document_card",
            )
        except ConversationNoticeError as exc:
            logger.warning("microsoft_365_card_not_posted", reason=exc.reason, card_event=event)
            return {"card_posted": False}
        return {"card_posted": True, "card_event_id": event_id}

    async def _document(self, token: str, ref: DocumentRef) -> _ConnectedDocument:
        item = await graph_json(token, "GET", ref.path(), params={"$select": _ITEM_FIELDS})
        return _ConnectedDocument._from_item(item)

    async def connect_office_document(self, url: str) -> str:
        """Connect a OneDrive or SharePoint Excel workbook so later requests read and edit that same file.

        Posts a document card in the conversation and returns the workbook outline.
        Pass the returned document_id to read_office_document and edit_office_document.

        Args:
            url: The workbook's OneDrive or SharePoint link, as copied from Excel or the browser.

        """
        try:
            encoded_share = share_id(url)
        except GraphError as exc:
            return self._error(exc)

        async def connect(token: str) -> dict[str, object]:
            document = _ConnectedDocument._from_item(await resolve_share(token, encoded_share, _ITEM_FIELDS))
            outline = await workbook_outline(token, document.ref)
            card = await self._post_card("connected", document)
            return {"document": document.summary(), "outline": outline, **card}

        return await self._call(connect)

    async def save_office_document(self, path: str, folder_url: str | None = None, name: str | None = None) -> str:
        """Upload a workspace .xlsx to Microsoft 365 as a new file and connect it; existing files are never replaced.

        Args:
            path: Workspace path of the .xlsx workbook to upload.
            folder_url: Optional OneDrive or SharePoint folder link; defaults to the MindRoom folder in the user's OneDrive.
            name: Optional file name; defaults to the local file name. A numbered name is used if it is taken.

        """
        try:
            authorized = resolve_agent_file(
                path,
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                field_name="path",
            )
            upload_name = _sanitized_upload_name(name if name is not None else authorized.name)
            folder_share = share_id(folder_url) if folder_url is not None else None
            content = await asyncio.to_thread(_read_upload, authorized)
        except ValueError as exc:
            return self._payload("error", code="invalid_path", message=str(exc))
        except OSError as exc:
            # Opening below the authorized root refuses links swapped in after the check.
            return self._payload(
                "error",
                code="invalid_path",
                message=f"path could not be read safely ({type(exc).__name__}).",
            )
        except GraphError as exc:
            return self._error(exc)

        async def save(token: str) -> dict[str, object]:
            folder = await self._upload_folder(token, folder_share)
            item = await self._upload_new(token, folder, upload_name, content)
            document = _ConnectedDocument._from_item(item)
            card = await self._post_card("saved", document)
            return {"document": document.summary(), **card}

        return await self._call(save)

    async def _upload_folder(self, token: str, folder_share: str | None) -> DocumentRef:
        """Return the target folder: a linked folder, or the user's OneDrive MindRoom folder, created if missing."""
        if folder_share is not None:
            return _folder_ref(await resolve_share(token, folder_share, _FOLDER_FIELDS))
        default_path = f"{graph_path('me', 'drive', 'root')}:/{_DEFAULT_FOLDER}"
        try:
            item = await graph_json(token, "GET", default_path, params={"$select": _FOLDER_FIELDS})
        except GraphError as exc:
            if exc.code != "not_found":
                raise
            try:
                item = await graph_json(
                    token,
                    "POST",
                    graph_path("me", "drive", "root", "children"),
                    json_body={"name": _DEFAULT_FOLDER, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
                )
            except GraphError as create_exc:
                if create_exc.code != "conflict":
                    raise
                # Another request created it first.
                item = await graph_json(token, "GET", default_path, params={"$select": _FOLDER_FIELDS})
        try:
            return _folder_ref(item)
        except GraphError as exc:
            if exc.code != "not_a_folder":
                raise
            raise GraphError(
                code="not_a_folder",
                message=f"'{_DEFAULT_FOLDER}' in the user's OneDrive is a file, so pass folder_url instead.",
            ) from None

    async def _upload_new(self, token: str, folder: DocumentRef, name: str, content: bytes) -> object:
        """Upload under the first free candidate name; the upload session fails instead of replacing a file."""
        for candidate in _name_candidates(name):
            try:
                # A cheap existence check avoids uploading the bytes for a name already taken.
                await graph_json(token, "GET", f"{folder.path()}:{graph_path(candidate)}", params={"$select": "id"})
            except GraphError as exc:
                if exc.code != "not_found":
                    raise
            else:
                continue
            try:
                return await graph_upload_new(token, folder, candidate, content)
            except GraphError as exc:
                if exc.code != "conflict":
                    raise
        raise GraphError(
            code="name_unavailable",
            message=f"Every name up to '{_name_candidates(name)[-1]}' is taken; pass a different name.",
        )

    async def read_office_document(self, document_id: str, range: str | None = None) -> str:  # noqa: A002
        """Read a connected workbook: its outline, or one range's formulas, values, and number formats.

        Without range, returns worksheets with used ranges, tables, and names.
        With range, returns at most 2,000 cells. Always read a range before editing it,
        because edit_office_document needs the exact current formulas as its before values.

        Args:
            document_id: The document_id returned by connect_office_document or save_office_document.
            range: Optional sheet-qualified A1 range such as Assumptions!B4:B8 or 'Summary Sheet'!A1:D20.

        """
        try:
            ref = DocumentRef.parse(document_id)
            target = parse_sheet_range(range) if range is not None else None
        except GraphError as exc:
            return self._error(exc)

        async def read(token: str) -> dict[str, object]:
            if target is not None:
                return {"document_id": ref.document_id, **await read_range(token, ref, target)}
            document = await self._document(token, ref)
            return {"document": document.summary(), "outline": await workbook_outline(token, ref)}

        return await self._call(read)

    async def edit_office_document(
        self,
        document_id: str,
        edits: list[dict[str, Any]],
        summary: str,
        skip_conflicts: bool = False,
    ) -> str:
        """Write cell edits to a connected workbook after a human approves them.

        Every edit names a sheet-qualified range, the before formulas you read with
        read_office_document, and the after formulas to write, as grids matching the range.
        Constants are written as themselves and formulas start with "=". Write numbers as
        numbers (0.12, not "12%"), use "" to clear a cell, and set formats with number_format,
        where null keeps a cell's format. Text Excel would convert, such as "0042", "1/2", or "TRUE",
        needs number_format "@" for that cell.
        A range whose current formulas differ from before is a conflict: by default nothing is written.
        To undo, swap before and after of the applied edits and set skip_conflicts=true, which keeps later human edits.

        Args:
            document_id: The document_id returned by connect_office_document or save_office_document.
            edits: Up to 25 non-overlapping edits covering at most 500 cells, each {"range": "Sheet!B4:B5",
                "before": [[0.08], [142]], "after": [[0.12], [142]], "number_format": [["0%"], [null]]};
                number_format is optional.
            summary: One short sentence describing the change for the reviewer, such as "Raise growth to 12%".
            skip_conflicts: Write only edits whose ranges still hold their before formulas.

        """
        try:
            ref = DocumentRef.parse(document_id)
            plans = parse_edits(edits)
            summary_text = summary.strip() if isinstance(summary, str) else ""
            if not summary_text or len(summary_text) > _MAX_SUMMARY_CHARS:
                msg = f"summary must be a non-empty sentence of at most {_MAX_SUMMARY_CHARS} characters."
                raise InvalidArgumentError(msg)
        except GraphError as exc:
            return self._error(exc)

        async def edit(token: str) -> dict[str, object]:
            receipt = await apply_edits(token, ref, plans, skip_conflicts=skip_conflicts)
            result = _receipt_fields(receipt)
            if not receipt.written:
                # A replay whose edits had all landed before changes nothing, so it posts no second card.
                return result
            # The workbook already changed, so a failure from here on must not hide the receipt.
            try:
                document = await self._document(token, ref)
            except GraphError as exc:
                result.update(card_posted=False, card_error={"code": exc.code, "message": exc.message})
                return result
            change: dict[str, object] = {
                "summary": summary_text,
                "status": receipt.status,
                "verified": receipt.verified,
                "cells_changed": receipt.cells_changed,
                # Cards carry no cell contents; the agent receives them in the receipt.
                "edits": [outcome.summary() for outcome in receipt.edits],
            }
            result["document"] = document.summary()
            result.update(await self._post_card("edited", document, change))
            return result

        return await self._call(edit)
