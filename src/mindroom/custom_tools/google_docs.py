"""Google Docs tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast

from googleapiclient.errors import HttpError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit, google_http_error_result
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_GOOGLE_DOCS_URL_ID_PATTERN = re.compile(
    r"^https?://docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:[/?#]|$)",
)
_FORMAT_OPERATION_TYPES = frozenset(
    {
        "paragraph_style",
        "text_style",
        "native_list",
        "remove_native_list",
        "hyperlink",
    },
)


def _document_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _normalize_document_id(value: str) -> str:
    document_id = value.strip()
    match = _GOOGLE_DOCS_URL_ID_PATTERN.match(document_id)
    return match.group(1) if match else document_id


def _formatting_range(operation: dict[str, object], tab_id: str) -> dict[str, object]:
    start_index = operation["start_index"]
    end_index = operation["end_index"]
    if (
        not isinstance(start_index, int)
        or isinstance(start_index, bool)
        or not isinstance(end_index, int)
        or isinstance(end_index, bool)
        or start_index < 1
        or end_index <= start_index
    ):
        msg = "range must use integer start_index and end_index with 1 <= start_index < end_index"
        raise ValueError(msg)
    return {"startIndex": start_index, "endIndex": end_index, "tabId": tab_id}


def _formatting_fields(operation: dict[str, object]) -> str:
    fields = operation["fields"]
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field.strip() or field == "*" for field in fields)
    ):
        msg = "style fields must be a non-empty list of explicit field names"
        raise ValueError(msg)
    return ",".join(fields)


def _formatting_style(operation: dict[str, object]) -> dict[str, object]:
    style = operation["style"]
    if not isinstance(style, dict) or not style or any(not isinstance(key, str) for key in style):
        msg = "style must be a non-empty object with string field names"
        raise ValueError(msg)
    return dict(style)


def _require_operation_keys(operation: dict[str, object], expected: set[str]) -> None:
    if set(operation) != expected:
        msg = "operation contains unsupported or missing fields"
        raise ValueError(msg)


def _formatting_request(operation: object, tab_id: str) -> dict[str, object]:
    if not isinstance(operation, dict):
        msg = "operation must be an object"
        raise TypeError(msg)
    operation_type = operation.get("type")
    if operation_type not in _FORMAT_OPERATION_TYPES:
        msg = "operation type is not supported"
        raise ValueError(msg)

    if operation_type == "paragraph_style":
        _require_operation_keys(operation, {"type", "start_index", "end_index", "style", "fields"})
        return {
            "updateParagraphStyle": {
                "range": _formatting_range(operation, tab_id),
                "paragraphStyle": _formatting_style(operation),
                "fields": _formatting_fields(operation),
            },
        }

    if operation_type == "text_style":
        _require_operation_keys(operation, {"type", "start_index", "end_index", "style", "fields"})
        style = _formatting_style(operation)
        fields = _formatting_fields(operation)
        if "link" in style or "link" in fields.split(","):
            msg = "text_style cannot set links; use hyperlink"
            raise ValueError(msg)
        return {
            "updateTextStyle": {
                "range": _formatting_range(operation, tab_id),
                "textStyle": style,
                "fields": fields,
            },
        }

    if operation_type == "native_list":
        _require_operation_keys(operation, {"type", "start_index", "end_index", "bullet_preset"})
        bullet_preset = operation["bullet_preset"]
        if not isinstance(bullet_preset, str) or not bullet_preset.strip():
            msg = "native_list requires a non-empty bullet_preset"
            raise ValueError(msg)
        return {
            "createParagraphBullets": {
                "range": _formatting_range(operation, tab_id),
                "bulletPreset": bullet_preset,
            },
        }

    if operation_type == "remove_native_list":
        _require_operation_keys(operation, {"type", "start_index", "end_index"})
        return {"deleteParagraphBullets": {"range": _formatting_range(operation, tab_id)}}

    _require_operation_keys(operation, {"type", "start_index", "end_index", "url"})
    url = operation["url"]
    if not isinstance(url, str) or not url.strip():
        msg = "hyperlink requires a non-empty url"
        raise ValueError(msg)
    return {
        "updateTextStyle": {
            "range": _formatting_range(operation, tab_id),
            "textStyle": {"link": {"url": url}},
            "fields": "link",
        },
    }


def _formatting_target(
    document_id: str,
    tab_id: str,
    expected_revision_id: str,
) -> tuple[str, str, str]:
    if not isinstance(document_id, str):
        msg = "document_id must be a string"
        raise TypeError(msg)
    if not isinstance(tab_id, str):
        msg = "tab_id must be a string"
        raise TypeError(msg)
    if not isinstance(expected_revision_id, str):
        msg = "expected_revision_id must be a string"
        raise TypeError(msg)

    document_id = _normalize_document_id(document_id)
    tab_id = tab_id.strip()
    expected_revision_id = expected_revision_id.strip()
    if not document_id:
        msg = "document_id must not be empty"
        raise ValueError(msg)
    if not tab_id:
        msg = "tab_id must not be empty"
        raise ValueError(msg)
    if not expected_revision_id:
        msg = "expected_revision_id must not be empty"
        raise ValueError(msg)
    return document_id, tab_id, expected_revision_id


def _formatting_requests(operations: object, tab_id: str) -> list[dict[str, object]]:
    if not isinstance(operations, list):
        msg = "operations must be a list"
        raise TypeError(msg)
    if not operations:
        msg = "operations must not be empty"
        raise ValueError(msg)
    return [_formatting_request(operation, tab_id) for operation in operations]


class GoogleDocsTools(GoogleApiToolkit):
    """Create, inspect, and edit Google Docs with scoped Google credentials."""

    _oauth_provider = google_oauth_provider("google_docs")
    _oauth_tool_name = "google_docs"
    _google_api_name = "docs"
    _google_api_version = "v1"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        create_document: bool = True,
        read_document: bool = True,
        edit_document: bool = True,
        format_document: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        tools = []
        if create_document:
            tools.append(self.google_docs_create_document)
        if read_document:
            tools.append(self.google_docs_get_document)
        if edit_document:
            tools.extend(
                [
                    self.google_docs_insert_text,
                    self.google_docs_replace_text,
                ],
            )
        if format_document:
            tools.append(self.google_docs_format_document)
        super().__init__(
            name="google_docs",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )

    def _batch_update(
        self,
        document_id: str,
        requests: list[dict[str, object]],
        *,
        write_control: dict[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._google_api_service()
        body: dict[str, object] = {"requests": requests}
        if write_control is not None:
            body["writeControl"] = write_control
        return cast(
            "dict[str, object]",
            service.documents().batchUpdate(documentId=document_id, body=body).execute(),
        )

    def google_docs_create_document(self, title: str, initial_text: str = "") -> str:
        """Create a Google Doc and optionally insert initial text into its body.

        Args:
            title: Title for the new document.
            initial_text: Optional text to insert into the new document.

        Returns:
            JSON containing the document structure, edit URL, and initial edit result.

        """
        if not title.strip():
            return json.dumps({"error": "Google Docs title must not be empty"})
        try:
            service = self._google_api_service()
            document = cast(
                "dict[str, object]",
                service.documents().create(body={"title": title}).execute(),
            )
            document_id = cast("str", document["documentId"])
            result: dict[str, object] = {
                "document": document,
                "documentUrl": _document_url(document_id),
            }
            if initial_text:
                try:
                    result["initialTextUpdate"] = self._batch_update(
                        document_id,
                        [
                            {
                                "insertText": {
                                    "endOfSegmentLocation": {},
                                    "text": initial_text,
                                },
                            },
                        ],
                    )
                except Exception as exc:
                    status = exc.resp.status if isinstance(exc, HttpError) else None
                    logger.warning(
                        "google_docs_initial_text_update_failed",
                        error_type=type(exc).__name__,
                        status=status,
                    )
                    initial_text_error = "Google Docs initial text update failed"
                    if not isinstance(status, bool) and isinstance(status, int):
                        initial_text_error = f"{initial_text_error} (HTTP {status})"
                    result.update(
                        {
                            "initialTextError": initial_text_error,
                            "partial_success": True,
                            "retry_safe": False,
                        },
                    )
            return json.dumps(result)
        except HttpError as exc:
            return google_http_error_result("Google Docs", "create_document", exc)

    def google_docs_get_document(self, document_id: str) -> str:
        """Read a Google Doc's full tab-aware structure and content.

        Args:
            document_id: Google Docs document ID from its URL or a create result.

        Returns:
            JSON containing the complete Google Docs API document resource and edit URL.

        """
        document_id = _normalize_document_id(document_id)
        if not document_id:
            return json.dumps({"error": "Google Docs document_id must not be empty"})
        try:
            service = self._google_api_service()
            document = cast(
                "dict[str, object]",
                service.documents().get(documentId=document_id, includeTabsContent=True).execute(),
            )
            return json.dumps(
                {
                    "document": document,
                    "documentUrl": _document_url(document_id),
                },
            )
        except HttpError as exc:
            return google_http_error_result("Google Docs", "get_document", exc)

    def google_docs_insert_text(
        self,
        document_id: str,
        text: str,
        index: int | None = None,
        tab_id: str | None = None,
    ) -> str:
        """Insert text at a body index or append it to the end of a document tab.

        Args:
            document_id: Google Docs document ID.
            text: Text to insert.
            index: Optional Docs API body index, with 1 as the first body position; omit to append.
            tab_id: Optional tab ID for a multi-tab document.

        Returns:
            JSON containing the atomic Google Docs batch-update response.

        """
        document_id = _normalize_document_id(document_id)
        if not document_id:
            return json.dumps({"error": "Google Docs document_id must not be empty"})
        if not text:
            return json.dumps({"error": "Google Docs insertion text must not be empty"})
        if index is not None and index < 1:
            return json.dumps({"error": "Google Docs insertion index must be at least 1"})

        location: dict[str, object]
        if index is None:
            location = {}
            if tab_id:
                location["tabId"] = tab_id
            insert_text: dict[str, object] = {"endOfSegmentLocation": location, "text": text}
        else:
            location = {"index": index}
            if tab_id:
                location["tabId"] = tab_id
            insert_text = {"location": location, "text": text}

        try:
            response = self._batch_update(document_id, [{"insertText": insert_text}])
            return json.dumps(
                {
                    "update": response,
                    "documentUrl": _document_url(document_id),
                },
            )
        except HttpError as exc:
            return google_http_error_result("Google Docs", "insert_text", exc)

    def google_docs_replace_text(
        self,
        document_id: str,
        find_text: str,
        replace_text: str,
        match_case: bool = False,
        tab_ids: list[str] | None = None,
    ) -> str:
        """Replace every matching text occurrence in all or selected document tabs.

        Args:
            document_id: Google Docs document ID.
            find_text: Text to find.
            replace_text: Replacement text, which may be empty to delete matches.
            match_case: Whether matching is case-sensitive.
            tab_ids: Optional tab IDs; omit to replace across every tab.

        Returns:
            JSON containing replacement counts in the Google Docs batch-update response.

        """
        document_id = _normalize_document_id(document_id)
        if not document_id:
            return json.dumps({"error": "Google Docs document_id must not be empty"})
        if not find_text:
            return json.dumps({"error": "Google Docs find_text must not be empty"})
        if tab_ids is not None and not tab_ids:
            return json.dumps({"error": "Google Docs tab_ids must contain at least one tab ID when provided"})

        replace_all_text: dict[str, object] = {
            "containsText": {
                "text": find_text,
                "matchCase": match_case,
            },
            "replaceText": replace_text,
        }
        if tab_ids is not None:
            replace_all_text["tabsCriteria"] = {"tabIds": tab_ids}
        try:
            response = self._batch_update(document_id, [{"replaceAllText": replace_all_text}])
            return json.dumps(
                {
                    "update": response,
                    "documentUrl": _document_url(document_id),
                },
            )
        except HttpError as exc:
            return google_http_error_result("Google Docs", "replace_text", exc)

    def google_docs_format_document(
        self,
        document_id: str,
        tab_id: str,
        expected_revision_id: str,
        operations: list[dict[str, object]],
    ) -> str:
        """Apply supported formatting operations to one Google Doc tab at one exact revision.

        Supported operations are paragraph_style, text_style, native_list,
        remove_native_list, and hyperlink. Each operation names a body-index
        range with start_index and end_index. Style operations include a style
        object and explicit fields list, native_list includes bullet_preset,
        and hyperlink includes url.

        Args:
            document_id: Google Docs document ID.
            tab_id: Immutable Google Docs tab ID to format.
            expected_revision_id: Revision ID returned by a preceding document read.
            operations: Supported formatting operations for ranges in the named tab.

        Returns:
            JSON containing the atomic Google Docs batch-update response and edit URL.

        """
        try:
            document_id, tab_id, expected_revision_id = _formatting_target(
                document_id,
                tab_id,
                expected_revision_id,
            )
            requests = _formatting_requests(operations, tab_id)
        except (TypeError, ValueError) as exc:
            return json.dumps({"error": f"Google Docs formatting {exc}"})

        try:
            response = self._batch_update(
                document_id,
                requests,
                write_control={"requiredRevisionId": expected_revision_id},
            )
            return json.dumps(
                {
                    "update": response,
                    "documentUrl": _document_url(document_id),
                    "tabId": tab_id,
                },
            )
        except HttpError as exc:
            return google_http_error_result("Google Docs", "format_document", exc)
