"""Google Docs tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal, cast

from googleapiclient.errors import HttpError

from mindroom.config.legacy_fields import RETIRED_CONSOLIDATED_FUNCTIONS
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import (
    GoogleApiToolkit,
    filter_google_tools,
    google_http_error_result,
    matches_google_function,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider
from mindroom.tools.native_helpers import restrict_actions

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_GOOGLE_DOCS_URL_ID_PATTERN = re.compile(
    r"^https?://docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:[/?#]|$)",
)
_REVISION_CONFLICT_GUIDANCE = (
    "An HTTP 400 with required_revision_id may mean the document changed since it was read "
    "(or another request error). Re-read the document/tab, verify the target text and recompute "
    "UTF-16 start-inclusive/end-exclusive ranges, and retry with the current revisionId."
)

_WriteAction = Literal["create", "insert_text", "replace_text", "batch_update"]
_WRITE_ACTION_FIELDS: dict[str, str] = {
    "create": "title initial_text",
    "insert_text": "document_id text index tab_id",
    "replace_text": "document_id find_text replace_text match_case tab_ids",
    "batch_update": "document_id requests required_revision_id",
}
_WRITE_PARAM_HINTS: dict[tuple[str, str], str] = {
    ("create", "text"): "use initial_text instead",
    ("replace_text", "tab_id"): "use tab_ids=[...] instead",
    ("insert_text", "tab_ids"): "use tab_id instead",
    ("batch_update", "tab_id"): "specify tabId inside each request",
    ("batch_update", "tab_ids"): "specify tabsCriteria inside each request",
}


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _document_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _normalize_document_id(value: str | None) -> str:
    document_id = (value or "").strip()
    match = _GOOGLE_DOCS_URL_ID_PATTERN.match(document_id)
    return match.group(1) if match else document_id


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
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        include_tools = kwargs.pop("include_tools", None)
        exclude_tools = kwargs.pop("exclude_tools", None)
        write_actions: list[str] = []
        if create_document:
            write_actions.append("create")
        if edit_document:
            write_actions.extend(("insert_text", "replace_text", "batch_update"))
        self._write_actions: tuple[str, ...] = tuple(write_actions)

        tools: list[Callable[..., str]] = []
        if read_document:
            tools.append(self.google_docs_get_document)
        if self._write_actions:
            tools.append(self.google_docs_write)
        tools = filter_google_tools(
            tools,
            include_tools,
            exclude_tools,
            retired_tools=RETIRED_CONSOLIDATED_FUNCTIONS["google_docs"],
            known_tools=("google_docs_get_document", "google_docs_write"),
        )
        super().__init__(
            name="google_docs",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``google_docs_write``'s action enum and parameters to enabled actions."""
        if matches_google_function(function.name, "google_docs_write"):
            return restrict_actions(function, self._write_actions, action_fields=_WRITE_ACTION_FIELDS)
        return function

    def _batch_update(
        self,
        document_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, object]:
        service = self._google_api_service()
        body: dict[str, Any] = {"requests": requests}
        if required_revision_id is not None:
            body["writeControl"] = {"requiredRevisionId": required_revision_id}
        return cast(
            "dict[str, object]",
            service.documents().batchUpdate(documentId=document_id, body=body).execute(),
        )

    def google_docs_get_document(self, document_id: str) -> str:
        """Read a Google Doc's full tab-aware structure and content.

        Args:
            document_id: Google Docs document ID from its URL or a create result.

        Returns:
            JSON containing the complete Google Docs API document resource and edit URL.

        """
        document_id = _normalize_document_id(document_id)
        if not document_id:
            return _error("Google Docs document_id must not be empty")
        try:
            service = self._google_api_service()
            document = cast(
                "dict[str, object]",
                service.documents().get(documentId=document_id, includeTabsContent=True).execute(),
            )
            return json.dumps({"document": document, "documentUrl": _document_url(document_id)})
        except HttpError as exc:
            return google_http_error_result("Google Docs", "get_document", exc)

    def _create_document(self, title: str | None, initial_text: str) -> str:
        if not title or not title.strip():
            return _error("Google Docs title must not be empty")
        try:
            service = self._google_api_service()
            document = cast("dict[str, object]", service.documents().create(body={"title": title}).execute())
            document_id = cast("str", document["documentId"])
            result: dict[str, object] = {"document": document, "documentUrl": _document_url(document_id)}
            if initial_text:
                try:
                    result["initialTextUpdate"] = self._batch_update(
                        document_id,
                        [{"insertText": {"endOfSegmentLocation": {}, "text": initial_text}}],
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

    def _insert_text(
        self,
        document_id: str | None,
        text: str | None,
        index: int | None,
        tab_id: str | None,
    ) -> str:
        doc_id = _normalize_document_id(document_id)
        if not doc_id:
            return _error("Google Docs document_id must not be empty")
        if not text:
            return _error("Google Docs insertion text must not be empty")
        if index is not None and index < 1:
            return _error("Google Docs insertion index must be at least 1")

        location: dict[str, object] = {} if index is None else {"index": index}
        if tab_id:
            location["tabId"] = tab_id
        location_key = "endOfSegmentLocation" if index is None else "location"
        try:
            response = self._batch_update(doc_id, [{"insertText": {location_key: location, "text": text}}])
            return json.dumps({"update": response, "documentUrl": _document_url(doc_id)})
        except HttpError as exc:
            return google_http_error_result("Google Docs", "insert_text", exc)

    def _replace_text(
        self,
        document_id: str | None,
        find_text: str | None,
        replace_text: str | None,
        match_case: bool,
        tab_ids: list[str] | None,
    ) -> str:
        doc_id = _normalize_document_id(document_id)
        if not doc_id:
            return _error("Google Docs document_id must not be empty")
        if not find_text:
            return _error("Google Docs find_text must not be empty")
        if replace_text is None:
            return _error("Google Docs replace_text must be provided (pass '' to delete matches)")
        if tab_ids is not None and not tab_ids:
            return _error("Google Docs tab_ids must contain at least one tab ID when provided")

        replace_all_text: dict[str, object] = {
            "containsText": {"text": find_text, "matchCase": match_case},
            "replaceText": replace_text,
        }
        if tab_ids is not None:
            replace_all_text["tabsCriteria"] = {"tabIds": tab_ids}
        try:
            response = self._batch_update(doc_id, [{"replaceAllText": replace_all_text}])
            return json.dumps({"update": response, "documentUrl": _document_url(doc_id)})
        except HttpError as exc:
            return google_http_error_result("Google Docs", "replace_text", exc)

    def _apply_batch_update(
        self,
        document_id: str | None,
        requests: list[dict[str, Any]] | None,
        required_revision_id: str | None,
    ) -> str:
        doc_id = _normalize_document_id(document_id)
        if not doc_id:
            return _error("Google Docs document_id must not be empty")
        if not requests:
            return _error("Google Docs requests must not be empty")
        normalized_revision_id: str | None = None
        if required_revision_id is not None:
            normalized_revision_id = required_revision_id.strip()
            if not normalized_revision_id:
                return _error("Google Docs required_revision_id must not be empty when provided")
        try:
            response = self._batch_update(doc_id, requests, required_revision_id=normalized_revision_id)
            return json.dumps({"update": response, "documentUrl": _document_url(doc_id)})
        except HttpError as exc:
            guidance = (
                _REVISION_CONFLICT_GUIDANCE if normalized_revision_id is not None and exc.resp.status == 400 else None
            )
            return google_http_error_result("Google Docs", "batch_update", exc, guidance=guidance)

    def _write_guard_error(self, action: str, **params: object) -> str | None:
        if action not in self._write_actions:
            return f"Google Docs action {action!r} is disabled"
        allowed = set(_WRITE_ACTION_FIELDS.get(action, "").split())
        for name, value in params.items():
            if name in allowed or value is None:
                continue
            if (name == "initial_text" and value == "") or (name == "match_case" and not value):
                continue
            if name == "required_revision_id":
                return "Google Docs required_revision_id is only supported with action='batch_update'"
            hint = _WRITE_PARAM_HINTS.get((action, name))
            return f"Google Docs {action} does not accept {name}{f'; {hint}' if hint else ''}"
        return None

    def google_docs_write(
        self,
        action: _WriteAction,
        document_id: str | None = None,
        title: str | None = None,
        initial_text: str = "",
        text: str | None = None,
        index: int | None = None,
        tab_id: str | None = None,
        find_text: str | None = None,
        replace_text: str | None = None,
        match_case: bool = False,
        tab_ids: list[str] | None = None,
        requests: list[dict[str, Any]] | None = None,
        required_revision_id: str | None = None,
    ) -> str:
        """Create or edit Google Docs.

        - `create`: new doc with `title` and optional `initial_text`
        - `insert_text`: insert `text` into `document_id` (ID or URL) at 1-based body `index` (omit to append) in optional `tab_id`
        - `replace_text`: replace `find_text` with `replace_text` (empty string deletes; optional `match_case`, `tab_ids` or all tabs) in `document_id` (ID or URL)
        - `batch_update`: apply Docs API `requests` atomically in order to `document_id` (ID or URL) for tables, headings, styles, lists, links, images, or deletes (UTF-16 tab indexes, default first tab; edit end-to-start); pass `required_revision_id` (from `google_docs_get_document`) for optimistic concurrency (`writeControl.requiredRevisionId`), re-reading and recomputing ranges on conflict
        """
        if (
            guard_error := self._write_guard_error(
                action,
                document_id=document_id,
                title=title,
                initial_text=initial_text,
                text=text,
                index=index,
                tab_id=tab_id,
                find_text=find_text,
                replace_text=replace_text,
                match_case=match_case,
                tab_ids=tab_ids,
                requests=requests,
                required_revision_id=required_revision_id,
            )
        ) is not None:
            return _error(guard_error)
        if action == "create":
            return self._create_document(title, initial_text)
        if action == "insert_text":
            return self._insert_text(document_id, text, index, tab_id)
        if action == "replace_text":
            return self._replace_text(document_id, find_text, replace_text, match_case, tab_ids)
        if action == "batch_update":
            return self._apply_batch_update(document_id, requests, required_revision_id)
        return _error(f"Unsupported Google Docs write action {action!r}")
