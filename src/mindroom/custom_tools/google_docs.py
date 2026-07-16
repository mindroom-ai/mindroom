"""Google Docs tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from agno.tools import Toolkit
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin, google_service_account_configured
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_docs import google_docs_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


def _document_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


class GoogleDocsTools(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, Toolkit):
    """Create, inspect, and edit Google Docs with scoped Google credentials."""

    _oauth_provider = google_docs_oauth_provider()
    _oauth_tool_name = "google_docs"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        create_document: bool = True,
        read_document: bool = True,
        edit_document: bool = True,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GoogleDocsTools requires an explicit credentials_manager"
            raise RuntimeError(msg)

        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        self.service_account_path = cast("str | None", kwargs.pop("service_account_path", None))
        self.delegated_user = cast("str | None", kwargs.pop("delegated_user", None))
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            msg = f"Google Docs received unsupported constructor arguments: {unexpected}"
            raise TypeError(msg)

        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )
        self.creds = creds

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
        super().__init__(name="google_docs", tools=tools)
        self._set_original_auth(GoogleDocsTools._service_account_auth)
        self._wrap_oauth_function_entrypoints()

    def _should_fallback_to_original_auth(self) -> bool:
        return google_service_account_configured(self.service_account_path, self._runtime_paths)

    def _service_account_auth(self) -> None:
        """Build Google credentials from the configured service-account file."""
        from google.oauth2 import service_account  # noqa: PLC0415

        if not self.service_account_path:
            msg = "Google Docs service-account authentication requires GOOGLE_SERVICE_ACCOUNT_FILE"
            raise RuntimeError(msg)
        creds = service_account.Credentials.from_service_account_file(
            self.service_account_path,
            scopes=self._oauth_provider.scopes,
        )
        if self.delegated_user:
            creds = creds.with_subject(self.delegated_user)
        self.creds = creds

    def _docs_service(self) -> Any:  # noqa: ANN401
        """Return the per-thread authenticated Google Docs service."""
        self._auth()
        if self.service is None:
            self.service = build("docs", "v1", credentials=self.creds, cache_discovery=False)
        return self.service

    def _batch_update(self, document_id: str, requests: list[dict[str, object]]) -> dict[str, object]:
        service = self._docs_service()
        return cast(
            "dict[str, object]",
            service.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute(),
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
            service = self._docs_service()
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
                except HttpError as exc:
                    result["initialTextError"] = f"Google Docs API error: {exc}"
            return json.dumps(result)
        except HttpError as exc:
            return json.dumps({"error": f"Google Docs API error: {exc}"})

    def google_docs_get_document(self, document_id: str) -> str:
        """Read a Google Doc's full tab-aware structure and content.

        Args:
            document_id: Google Docs document ID from its URL or a create result.

        Returns:
            JSON containing the complete Google Docs API document resource and edit URL.

        """
        if not document_id.strip():
            return json.dumps({"error": "Google Docs document_id must not be empty"})
        try:
            service = self._docs_service()
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
            return json.dumps({"error": f"Google Docs API error: {exc}"})

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
        if not document_id.strip():
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
            return json.dumps({"error": f"Google Docs API error: {exc}"})

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
        if not document_id.strip():
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
            return json.dumps({"error": f"Google Docs API error: {exc}"})
