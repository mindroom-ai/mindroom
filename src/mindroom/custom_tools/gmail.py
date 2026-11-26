"""Gmail tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import tempfile
import textwrap
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from googleapiclient.errors import HttpError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import (
    GoogleApiToolkit,
    filter_google_tools,
    matches_google_function,
    restrict_google_actions,
    visible_google_function_map,
)
from mindroom.file_access import resolve_agent_file
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_GMAIL_PROFILE_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
    },
)
_GMAIL_SEND_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    },
)
_GMAIL_READ_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    },
)
_GMAIL_DRAFT_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
    },
)
_GMAIL_DRAFT_READ_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
    },
)
_GMAIL_MODIFY_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
    },
)

_ReadAction = Literal[
    "latest",
    "from_user",
    "unread",
    "starred",
    "by_context",
    "by_date",
    "by_thread",
    "search_emails",
    "get_message",
    "get_thread",
    "search_threads",
    "get_draft",
    "list_drafts",
    "list_custom_labels",
]
_DraftAction = Literal["create", "update"]
_SendAction = Literal["send", "reply"]
_ManageAction = Literal[
    "mark_read",
    "mark_unread",
    "star",
    "unstar",
    "apply_label",
    "remove_label",
    "delete_custom_label",
]

_ALL_READ_ACTIONS: tuple[str, ...] = (
    "latest",
    "from_user",
    "unread",
    "starred",
    "by_context",
    "by_date",
    "by_thread",
    "search_emails",
    "get_message",
    "get_thread",
    "search_threads",
    "get_draft",
    "list_drafts",
    "list_custom_labels",
)
_ALL_DRAFT_ACTIONS: tuple[str, ...] = ("create", "update")
_ALL_SEND_ACTIONS: tuple[str, ...] = ("send", "reply")
_ALL_MANAGE_ACTIONS: tuple[str, ...] = (
    "mark_read",
    "mark_unread",
    "star",
    "unstar",
    "apply_label",
    "remove_label",
    "delete_custom_label",
)

_GMAIL_DRAFT_READ_ACTIONS = frozenset({"get_draft", "list_drafts"})
_GMAIL_MAILBOX_READ_ACTIONS = frozenset(_ALL_READ_ACTIONS) - _GMAIL_DRAFT_READ_ACTIONS

_READ_ACTION_FIELDS: dict[str, str] = {
    "latest": "count page_token",
    "from_user": "user count page_token",
    "unread": "count page_token",
    "starred": "count page_token",
    "by_context": "query count page_token",
    "by_date": "start_date range_in_days count page_token",
    "by_thread": "thread_id",
    "search_emails": "query count page_token",
    "get_message": "message_id download_attachments",
    "get_thread": "thread_id",
    "search_threads": "query count page_token",
    "get_draft": "draft_id",
    "list_drafts": "count page_token",
    "list_custom_labels": "",
}
_DRAFT_ACTION_FIELDS: dict[str, str] = {
    "create": "to subject body cc bcc attachments thread_id message_id",
    "update": "draft_id to subject body cc bcc attachments thread_id message_id",
}
_SEND_ACTION_FIELDS: dict[str, str] = {
    "send": "to subject body cc bcc attachments thread_id message_id",
    "reply": "to subject body cc attachments thread_id message_id",
}
_MANAGE_ACTION_FIELDS: dict[str, str] = {
    "mark_read": "message_id",
    "mark_unread": "message_id",
    "star": "message_id",
    "unstar": "message_id",
    "apply_label": "query label_name count",
    "remove_label": "query label_name count",
    "delete_custom_label": "label_name confirm",
}
_SIMPLE_LABEL_MUTATIONS: dict[str, tuple[dict[str, list[str]], str, str]] = {
    "mark_read": ({"removeLabelIds": ["UNREAD"]}, "Email {id} marked as read.", "marking as read"),
    "mark_unread": ({"addLabelIds": ["UNREAD"]}, "Email {id} marked as unread.", "marking as unread"),
    "star": ({"addLabelIds": ["STARRED"]}, "Email {id} starred.", "starring"),
    "unstar": ({"removeLabelIds": ["STARRED"]}, "Email {id} unstarred.", "unstarring"),
}

# Bounds raw attachment bytes held in memory across a single message.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_GMAIL_QUERY_INSTRUCTIONS = textwrap.dedent("""\
    ## Gmail Query & Pagination
    Pass `nextPageToken` as `page_token` for subsequent pages.
    Query operators: `from:`, `to:`, `subject:"..."`, `is:unread`, `is:starred`, `is:important`, `has:attachment`, `newer_than:7d`, `older_than:1m`, `after:YYYY/MM/DD`, `before:YYYY/MM/DD`, `label:`, `from:me` (space = AND).""")

_COMPOSE_HINT_NAMES = ("gmail_send", "gmail_draft")


def _validate_email(email: str) -> bool:
    """Validate email address syntax."""
    return bool(_EMAIL_PATTERN.match(email.strip()))


def _compose_instructions_for(
    visible: dict[str, str],
    *,
    send_actions: Collection[str],
    draft_actions: Collection[str],
) -> str:
    lines: list[str] = []
    send_name = visible.get("gmail_send")
    draft_name = visible.get("gmail_draft")
    if send_name and "reply" in send_actions:
        lines.append(
            f"- Reply in-thread: `{send_name}(action='reply', thread_id=..., message_id=..., to=..., subject=..., body=...)`",
        )
    if draft_name and "create" in draft_actions:
        lines.append(
            f"- Draft reply in-thread: `{draft_name}(action='create', to=..., subject=..., body=..., thread_id=..., message_id=...)`",
        )
    if not lines:
        return ""
    return "\n## Threading\n" + "\n".join(lines)


def _read_attachments(
    workspace_root: Path | None,
    attachments: object,
    *,
    file_access: FileAccess,
) -> list[tuple[str, bytes]]:
    """Read authorized attachments directly via no-follow file descriptors within the 25 MiB limit."""
    workspace_mode = file_access == "workspace"
    if workspace_mode:
        if workspace_root is None:
            msg = "Gmail attachments require an agent workspace"
            raise ValueError(msg)
        if not workspace_root.is_dir():
            msg = "Gmail attachments require an existing agent workspace"
            raise ValueError(msg)
    requested = [attachments] if isinstance(attachments, str) else attachments
    if not isinstance(requested, list | tuple) or not all(isinstance(path, str) for path in requested):
        msg = "Gmail attachments must be file paths"
        raise ValueError(msg)

    location = "in the agent workspace" if workspace_mode else "readable by MindRoom"
    remaining = _MAX_ATTACHMENT_BYTES
    loaded: list[tuple[str, bytes]] = []
    for attachment in requested:
        try:
            authorized = resolve_agent_file(
                attachment,
                workspace_root=workspace_root,
                file_access=file_access,
                field_name="Gmail attachment",
            )
            with authorized.open() as source:
                data = source.read(remaining + 1)
        except (OSError, ValueError):
            msg = f"Gmail attachment must be a regular file {location}: {attachment}"
            raise ValueError(msg) from None
        if len(data) > remaining:
            msg = "Gmail attachments exceed the 25 MiB limit"
            raise ValueError(msg)
        remaining -= len(data)
        loaded.append((authorized.name, data))
    return loaded


def _decode_body_data(data: str) -> str:
    """Decode a base64url-encoded Gmail body part to text."""
    try:
        raw_bytes = base64.urlsafe_b64decode(data)
    except Exception:
        return ""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("latin-1")


def _validate_email_params(to: str, subject: str, body: str) -> None:
    if not to:
        msg = "Recipient email cannot be empty"
        raise ValueError(msg)
    for email in to.split(","):
        if not _validate_email(email.strip()):
            msg = f"Invalid recipient email format: {email}"
            raise ValueError(msg)
    if not subject or not subject.strip():
        msg = "Subject cannot be empty"
        raise ValueError(msg)
    if body is None:
        msg = "Email body cannot be None"
        raise ValueError(msg)


def _header_value(headers: list[dict[str, str]], name: str) -> str | None:
    return next((header["value"] for header in headers if header["name"] == name), None)


def _format_emails(emails: list[dict[str, Any]]) -> str:
    if not emails:
        return "No emails found."
    return "\n\n".join(
        (
            f"From: {email['from']}\n"
            f"Subject: {email['subject']}\n"
            f"Date: {email['date']}\n"
            f"Body: {email['body']}\n"
            f"Message ID: {email['id']}\n"
            f"In-Reply-To: {email['in-reply-to']}\n"
            f"References: {email['references']}\n"
            f"Thread ID: {email['thread_id']}\n"
            "----------------------------------------"
        )
        for email in emails
    )


def _build_mime_message(
    to: list[str],
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> dict[str, str]:
    """Build a base64url-encoded MIME payload for Gmail send/draft endpoints."""
    html_body = body.replace("\\n", "\n").replace("\n", "<br>")
    message: MIMEMultipart | MIMEText
    if attachments:
        message = MIMEMultipart()
        message.attach(MIMEText(html_body, "html"))
        for filename, data in attachments:
            content_type, encoding = mimetypes.guess_type(filename)
            if content_type is None or encoding is not None:
                content_type = "application/octet-stream"
            _, sub_type = content_type.split("/", 1)
            attachment = MIMEApplication(data, _subtype=sub_type)
            attachment.add_header("Content-Disposition", "attachment", filename=filename)
            message.attach(attachment)
    else:
        message = MIMEText(html_body, "html")

    message["to"] = ", ".join(to)
    message["from"] = "me"
    message["subject"] = subject
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if thread_id and message_id:
        message["In-Reply-To"] = message_id
        message["References"] = message_id

    email_data: dict[str, str] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if thread_id:
        email_data["threadId"] = thread_id
    return email_data


def _validate_gmail_scopes(
    registered_names: Collection[str],
    *,
    read_actions: Collection[str],
    draft_actions: Collection[str],
    send_actions: Collection[str],
    manage_actions: Collection[str],
    scopes: Collection[str],
) -> None:
    """Ensure explicit scopes cover every enabled Gmail operation."""
    registered = set(registered_names)
    scope_set = set(scopes)
    if "gmail_draft" in registered and draft_actions and not (_GMAIL_DRAFT_SCOPES & scope_set):
        msg = "A Gmail compose or modify scope is required for draft operations"
        raise ValueError(msg)
    if "gmail_send" in registered and send_actions and not (_GMAIL_SEND_SCOPES & scope_set):
        msg = "A Gmail send, compose, or modify scope is required for email sending operations"
        raise ValueError(msg)
    if "gmail_read" in registered:
        enabled_reads = set(read_actions)
        if enabled_reads & _GMAIL_DRAFT_READ_ACTIONS and not (_GMAIL_DRAFT_READ_SCOPES & scope_set):
            msg = "A Gmail read, compose, or modify scope is required for draft reading operations"
            raise ValueError(msg)
        if enabled_reads & _GMAIL_MAILBOX_READ_ACTIONS and not (_GMAIL_READ_SCOPES & scope_set):
            msg = "A Gmail read or modify scope is required for email reading operations"
            raise ValueError(msg)
    if "gmail_manage" in registered and manage_actions and not (_GMAIL_MODIFY_SCOPES & scope_set):
        msg = "The scope https://www.googleapis.com/auth/gmail.modify is required for email modification operations"
        raise ValueError(msg)


class GmailTools(GoogleApiToolkit):
    """Read, search, organize, draft, and send emails with MindRoom-scoped OAuth credentials."""

    _oauth_provider = google_oauth_provider("google_gmail")
    _oauth_tool_name = "gmail"
    _google_api_name = "gmail"
    _google_api_version = "v1"
    _require_delegated_user_for_service_account: ClassVar[bool] = True

    default_scopes: ClassVar[list[str]] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
    ]

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        scopes: Sequence[str] | None = None,
        max_results: int = 100,
        max_body_length: int = 50000,
        attachment_dir: str | None = None,
        include_html: bool = False,
        get_latest_emails: bool | None = True,
        get_emails_from_user: bool | None = True,
        get_unread_emails: bool | None = True,
        get_starred_emails: bool | None = True,
        get_emails_by_context: bool | None = True,
        get_emails_by_date: bool | None = True,
        get_emails_by_thread: bool | None = True,
        search_emails: bool | None = True,
        mark_email_as_read: bool | None = True,
        mark_email_as_unread: bool | None = True,
        star_email: bool | None = True,
        unstar_email: bool | None = True,
        create_draft_email: bool | None = True,
        send_email: bool | None = True,
        send_email_reply: bool | None = True,
        list_custom_labels: bool | None = True,
        apply_label: bool | None = True,
        remove_label: bool | None = True,
        delete_custom_label: bool | None = True,
        get_message: bool | None = True,
        get_thread: bool | None = True,
        search_threads: bool | None = True,
        get_draft: bool | None = True,
        list_drafts: bool | None = True,
        update_draft: bool | None = True,
        include_tools: Sequence[str] | None = None,
        exclude_tools: Sequence[str] | None = None,
        instructions: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        self.scopes = list(self.default_scopes) if scopes is None else list(scopes)
        self.max_results = max(min(max_results, 500), 1)
        self.max_body_length = max_body_length
        self.attachment_dir = attachment_dir
        self.include_html = include_html
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._custom_instructions = instructions

        read_toggles: tuple[tuple[bool | None, str], ...] = (
            (get_latest_emails, "latest"),
            (get_emails_from_user, "from_user"),
            (get_unread_emails, "unread"),
            (get_starred_emails, "starred"),
            (get_emails_by_context, "by_context"),
            (get_emails_by_date, "by_date"),
            (get_emails_by_thread, "by_thread"),
            (search_emails, "search_emails"),
            (get_message, "get_message"),
            (get_thread, "get_thread"),
            (search_threads, "search_threads"),
            (get_draft, "get_draft"),
            (list_drafts, "list_drafts"),
            (list_custom_labels, "list_custom_labels"),
        )
        draft_toggles: tuple[tuple[bool | None, str], ...] = (
            (create_draft_email, "create"),
            (update_draft, "update"),
        )
        send_toggles: tuple[tuple[bool | None, str], ...] = (
            (send_email, "send"),
            (send_email_reply, "reply"),
        )
        manage_toggles: tuple[tuple[bool | None, str], ...] = (
            (mark_email_as_read, "mark_read"),
            (mark_email_as_unread, "mark_unread"),
            (star_email, "star"),
            (unstar_email, "unstar"),
            (apply_label, "apply_label"),
            (remove_label, "remove_label"),
            (delete_custom_label, "delete_custom_label"),
        )
        self._read_actions: tuple[str, ...] = tuple(action for enabled, action in read_toggles if enabled)
        self._draft_actions: tuple[str, ...] = tuple(action for enabled, action in draft_toggles if enabled)
        self._send_actions: tuple[str, ...] = tuple(action for enabled, action in send_toggles if enabled)
        self._manage_actions: tuple[str, ...] = tuple(action for enabled, action in manage_toggles if enabled)

        selected: list[Callable[..., str]] = []
        if self._read_actions:
            selected.append(self.gmail_read)
        if self._draft_actions:
            selected.append(self.gmail_draft)
        if self._send_actions:
            selected.append(self.gmail_send)
        if _GMAIL_PROFILE_SCOPES.intersection(self.scopes) and _GMAIL_SEND_SCOPES.intersection(self.scopes):
            selected.append(self.gmail_send_to_self)
        else:
            logger.warning("gmail_send_email_to_self_disabled_missing_scope")
        if self._manage_actions:
            selected.append(self.gmail_manage)

        tools = filter_google_tools(selected, include_tools, exclude_tools)

        super().__init__(
            name="gmail",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )
        _validate_gmail_scopes(
            self.functions,
            read_actions=self._read_actions,
            draft_actions=self._draft_actions,
            send_actions=self._send_actions,
            manage_actions=self._manage_actions,
            scopes=self.scopes,
        )

    def _service_account_scopes(self) -> tuple[str, ...]:
        """Return Gmail service-account scopes, honoring explicit constructor scopes."""
        return tuple(self.scopes)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict Gmail action enums and parameters to enabled actions."""
        restrictions: tuple[tuple[str, tuple[str, ...], tuple[str, ...], dict[str, str]], ...] = (
            ("gmail_read", self._read_actions, _ALL_READ_ACTIONS, _READ_ACTION_FIELDS),
            ("gmail_draft", self._draft_actions, _ALL_DRAFT_ACTIONS, _DRAFT_ACTION_FIELDS),
            ("gmail_send", self._send_actions, _ALL_SEND_ACTIONS, _SEND_ACTION_FIELDS),
            ("gmail_manage", self._manage_actions, _ALL_MANAGE_ACTIONS, _MANAGE_ACTION_FIELDS),
        )
        for base_name, enabled, all_actions, fields in restrictions:
            if matches_google_function(function.name, base_name) and len(enabled) < len(all_actions):
                return restrict_google_actions(function, enabled, fields)
        return function

    @property
    def instructions(self) -> str:  # ty: ignore[incompatible-property-override]
        """Return instructions scoped to the currently registered Gmail functions."""
        return self.instructions_for(self.functions)

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return Gmail guidance naming only offered functions."""
        if self._custom_instructions is not None:
            return self._custom_instructions
        if not function_names:
            return ""
        compose_section = _compose_instructions_for(
            visible_google_function_map(function_names, _COMPOSE_HINT_NAMES),
            send_actions=self._send_actions,
            draft_actions=self._draft_actions,
        )
        return f"{_GMAIL_QUERY_INSTRUCTIONS}{compose_section}"

    def _load_attachments(self, attachments: object) -> list[tuple[str, bytes]] | None:
        if not attachments:
            return None
        return _read_attachments(
            self._workspace_root,
            attachments,
            file_access=self._file_access,
        )

    def _list_emails(
        self,
        *,
        query: str | None,
        count: int,
        page_token: str | None,
        operation: str,
        error_label: str,
    ) -> str:
        try:
            service = self._google_api_service()
            effective_count = min(count, self.max_results)
            list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": effective_count}
            if query is not None:
                list_kwargs["q"] = query
            if page_token:
                list_kwargs["pageToken"] = page_token
            results = service.users().messages().list(**list_kwargs).execute()
            emails = self._get_message_details(service, results.get("messages", []))
            response: dict[str, Any] = {"emails": emails, "count": len(emails)}
            if count > effective_count:
                response["requested"] = count
            if results.get("resultSizeEstimate"):
                response["totalEstimate"] = results["resultSizeEstimate"]
            if results.get("nextPageToken"):
                response["nextPageToken"] = results["nextPageToken"]
            return json.dumps(response)
        except HttpError as exc:
            logger.warning("gmail_list_emails_failed", operation=operation, status=exc.resp.status)
            return json.dumps({"error": f"Error retrieving {error_label}: {exc}"})
        except Exception as exc:
            logger.warning("gmail_list_emails_unexpected_error", operation=operation, error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

    def _modify_simple_label(
        self,
        *,
        message_id: str,
        body: dict[str, list[str]],
        success_message: str,
        error_label: str,
    ) -> str:
        try:
            service = self._google_api_service()
            service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        except HttpError as exc:
            logger.warning("gmail_modify_label_failed", operation=error_label, status=exc.resp.status)
            return f"HTTP Error {error_label} email {message_id}: {exc}"
        except Exception as exc:
            logger.warning("gmail_modify_label_unexpected_error", operation=error_label, error_type=type(exc).__name__)
            return f"Error {error_label} email {message_id}: {type(exc).__name__}: {exc}"
        return success_message

    def _get_message_details(self, service: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:  # noqa: ANN401
        details: list[dict[str, Any]] = []
        for msg in messages:
            msg_data = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            headers = msg_data["payload"]["headers"]
            details.append(
                {
                    "id": msg_data["id"],
                    "thread_id": msg_data.get("threadId"),
                    "subject": _header_value(headers, "Subject"),
                    "from": _header_value(headers, "From"),
                    "date": _header_value(headers, "Date"),
                    "in-reply-to": _header_value(headers, "In-Reply-To"),
                    "references": _header_value(headers, "References"),
                    "body": self._get_message_body(msg_data),
                },
            )
        return details

    def _get_message_body(self, msg_data: dict[str, Any]) -> str:
        try:
            payload = msg_data["payload"]
            body, parsed_attachments = self._extract_body(payload)
            attachments = [attachment["filename"] for attachment in parsed_attachments]
            attachments.extend(
                part["filename"]
                for part in payload.get("parts", [])
                if part.get("filename") and not part.get("body", {}).get("attachmentId")
            )
        except Exception:
            return "Unable to decode message body"
        if attachments:
            return f"{body}\n\nAttachments: {', '.join(attachments)}"
        return body

    def _extract_body(self, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        attachments: list[dict[str, Any]] = []

        def extract_part(part: dict[str, Any]) -> tuple[str, str]:
            mime_type = part.get("mimeType", "text/plain")
            if "parts" in part:
                bodies = [item for item in (extract_part(child) for child in part["parts"]) if item[0]]
                if not bodies:
                    return "", ""
                if part.get("mimeType", "multipart/alternative") == "multipart/alternative":
                    plain_bodies = [item for item in bodies if item[1] == "text/plain"]
                    bodies = plain_bodies or bodies
                body_mime = "text/plain" if all(mime == "text/plain" for _, mime in bodies) else "text/html"
                return "\n".join(text for text, _ in bodies), body_mime

            part_body = part.get("body", {})
            if part_body.get("attachmentId"):
                attachments.append(
                    {
                        "filename": part.get("filename", "unknown"),
                        "mimeType": part.get("mimeType", ""),
                        "size": part_body.get("size", 0),
                        "attachmentId": part_body["attachmentId"],
                    },
                )
                return "", ""

            data = part_body.get("data")
            if not data or mime_type not in ("text/plain", "text/html"):
                return "", ""

            text = _decode_body_data(data)
            if mime_type == "text/html" and not self.include_html:
                text = re.sub(r"<[^>]+>", "", text)
                text = "\n".join(stripped for line in text.splitlines() if (stripped := line.strip()))
            return text, mime_type

        body, _ = extract_part(payload)
        if self.max_body_length and len(body) > self.max_body_length:
            body = body[: self.max_body_length] + "... [truncated]"
        return body, attachments

    def _format_message(self, msg_data: dict[str, Any], include_body: bool = True) -> dict[str, Any]:
        raw_headers = msg_data.get("payload", {}).get("headers", [])
        headers = {header["name"].lower(): header["value"] for header in raw_headers}
        result: dict[str, Any] = {
            "id": msg_data["id"],
            "threadId": msg_data.get("threadId"),
            "labelIds": msg_data.get("labelIds", []),
            "snippet": msg_data.get("snippet", ""),
            "subject": headers.get("subject"),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "date": headers.get("date"),
            "cc": headers.get("cc"),
            "inReplyTo": headers.get("in-reply-to"),
            "references": headers.get("references"),
        }
        if include_body and "payload" in msg_data:
            body, attachments = self._extract_body(msg_data["payload"])
            result["body"] = body
            if attachments:
                result["attachments"] = attachments
        return result

    def _resolve_label_ids(self, service: Any, label_names: list[str]) -> list[str]:  # noqa: ANN401
        label_cache = self._label_cache
        if label_cache is None:
            labels = service.users().labels().list(userId="me").execute().get("labels", [])
            label_cache = {label["name"].lower(): label["id"] for label in labels}
            self._label_cache = label_cache
        return [label_cache.get(name.lower(), name) for name in label_names]

    def _download_attachment_file(
        self,
        service: Any,  # noqa: ANN401
        message_id: str,
        attachment_id: str,
        filename: str,
    ) -> str:
        att = (
            service.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id).execute()
        )
        data = base64.urlsafe_b64decode(att["data"])
        if self.attachment_dir:
            dest_dir = Path(self.attachment_dir)
        else:
            if self._temp_dir is None:
                self._temp_dir = tempfile.TemporaryDirectory()
            dest_dir = Path(self._temp_dir.name)
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "attachment"
        file_path = dest_dir / safe_name
        file_path.write_bytes(data)
        return str(file_path)

    def _prepare_outgoing_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        is_reply: bool = False,
    ) -> dict[str, str] | str:
        """Load attachments, validate fields, and build a Gmail MIME payload."""
        try:
            loaded_attachments = self._load_attachments(attachments)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        _validate_email_params(to, subject, body)
        if (is_reply or thread_id) and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return _build_mime_message(
            [addr.strip() for addr in to.split(",")],
            subject,
            body,
            cc=[addr.strip() for addr in cc.split(",")] if cc else None,
            bcc=[addr.strip() for addr in bcc.split(",")] if bcc else None,
            thread_id=thread_id,
            message_id=message_id,
            attachments=loaded_attachments,
        )

    def _send_email_impl(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        is_reply: bool = False,
        error_verb: str = "sending email",
    ) -> str:
        try:
            mime = self._prepare_outgoing_message(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc,
                attachments=attachments,
                thread_id=thread_id,
                message_id=message_id,
                is_reply=is_reply,
            )
            if isinstance(mime, str):
                return mime
            service = self._google_api_service()
            sent = service.users().messages().send(userId="me", body=mime).execute()
        except HttpError as exc:
            logger.warning("gmail_send_failed", status=exc.resp.status)
            return f"HTTP Error {error_verb}: {exc}"
        except Exception as exc:
            logger.warning("gmail_send_unexpected_error", error_type=type(exc).__name__)
            return f"Error {error_verb}: {type(exc).__name__}: {exc}"
        return json.dumps(sent)

    def _get_emails_by_date(
        self,
        start_date: str,
        range_in_days: int | None,
        count: int,
        page_token: str | None,
    ) -> str:
        try:
            start_date_dt = datetime.strptime(start_date, "%Y/%m/%d")  # noqa: DTZ007
        except Exception as exc:
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        if range_in_days:
            end_date = start_date_dt + timedelta(days=range_in_days)
            query = f"after:{start_date} before:{end_date.strftime('%Y/%m/%d')}"
        else:
            query = f"after:{start_date}"
        return self._list_emails(
            query=query,
            count=count,
            page_token=page_token,
            operation="get_emails_by_date",
            error_label="emails by date",
        )

    def _get_emails_by_thread(self, thread_id: str) -> str:
        try:
            service = self._google_api_service()
            thread = service.users().threads().get(userId="me", id=thread_id).execute()
            emails = self._get_message_details(service, thread.get("messages", []))
        except HttpError as exc:
            logger.warning("gmail_get_thread_failed", status=exc.resp.status)
            return f"Error retrieving emails from thread {thread_id}: {exc}"
        except Exception as exc:
            logger.warning("gmail_get_thread_unexpected_error", error_type=type(exc).__name__)
            return f"Unexpected error retrieving emails from thread {thread_id}: {type(exc).__name__}: {exc}"
        return _format_emails(emails)

    def _get_message(self, message_id: str, download_attachments: bool) -> str:
        try:
            service = self._google_api_service()
            raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            result = self._format_message(raw)
            if download_attachments and result.get("attachments"):
                for att in result["attachments"]:
                    if att.get("attachmentId"):
                        att["localPath"] = self._download_attachment_file(
                            service,
                            message_id,
                            att["attachmentId"],
                            att["filename"],
                        )
        except HttpError as exc:
            logger.warning("gmail_get_message_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_get_message_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps(result)

    def _get_thread(self, thread_id: str) -> str:
        try:
            service = self._google_api_service()
            thread = service.users().threads().get(userId="me", id=thread_id).execute()
            messages = [self._format_message(msg) for msg in thread.get("messages", [])]
        except HttpError as exc:
            logger.warning("gmail_get_thread_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_get_thread_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"threadId": thread_id, "messages": messages, "messageCount": len(messages)})

    def _search_threads(self, query: str, count: int, page_token: str | None) -> str:
        try:
            service = self._google_api_service()
            effective_count = min(count, self.max_results, 500)
            list_kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": effective_count}
            if page_token:
                list_kwargs["pageToken"] = page_token
            results = service.users().threads().list(**list_kwargs).execute()
        except HttpError as exc:
            logger.warning("gmail_search_threads_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_search_threads_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

        threads = results.get("threads", [])
        response: dict[str, Any] = {
            "threads": threads,
            "totalEstimate": results.get("resultSizeEstimate", len(threads)),
        }
        if count > effective_count:
            response["requested"] = count
        if results.get("nextPageToken"):
            response["nextPageToken"] = results["nextPageToken"]
        return json.dumps(response)

    def _get_draft(self, draft_id: str) -> str:
        try:
            service = self._google_api_service()
            draft = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
            msg_data = draft.get("message", {})
        except HttpError as exc:
            logger.warning("gmail_get_draft_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_get_draft_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"draftId": draft["id"], "message": self._format_message(msg_data) if msg_data else {}})

    def _list_drafts(self, count: int, page_token: str | None) -> str:
        try:
            service = self._google_api_service()
            effective_count = min(count, self.max_results, 500)
            list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": effective_count}
            if page_token:
                list_kwargs["pageToken"] = page_token
            results = service.users().drafts().list(**list_kwargs).execute()
        except HttpError as exc:
            logger.warning("gmail_list_drafts_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_list_drafts_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

        drafts = results.get("drafts", [])
        response: dict[str, Any] = {
            "drafts": drafts,
            "totalEstimate": results.get("resultSizeEstimate", len(drafts)),
        }
        if count > effective_count:
            response["requested"] = count
        if results.get("nextPageToken"):
            response["nextPageToken"] = results["nextPageToken"]
        return json.dumps(response)

    def _list_custom_labels(self) -> str:
        try:
            service = self._google_api_service()
            labels = service.users().labels().list(userId="me").execute().get("labels", [])
        except HttpError as exc:
            logger.warning("gmail_list_labels_failed", status=exc.resp.status)
            return f"Error fetching labels: {exc}"
        except Exception as exc:
            logger.warning("gmail_list_labels_unexpected_error", error_type=type(exc).__name__)
            return f"Unexpected error: {type(exc).__name__}: {exc}"

        custom_labels = [label["name"] for label in labels if label.get("type") == "user"]
        if not custom_labels:
            return "No custom labels found."
        numbered = [f"{idx}. {name}" for idx, name in enumerate(custom_labels, 1)]
        return f"Your Custom Labels ({len(custom_labels)} total):\n\n" + "\n".join(numbered)

    def _apply_label(self, query: str, label_name: str, count: int) -> str:
        try:
            service = self._google_api_service()
            messages = (
                service.users().messages().list(userId="me", q=query, maxResults=count).execute().get("messages", [])
            )
            if not messages:
                return f"No emails found matching: '{query}'"

            self._resolve_label_ids(service, [label_name])
            assert self._label_cache is not None
            label_id = self._label_cache.get(label_name.lower())
            if not label_id:
                label = (
                    service.users()
                    .labels()
                    .create(
                        userId="me",
                        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                    )
                    .execute()
                )
                label_id = label["id"]
                self._label_cache = None

            for msg in messages:
                service.users().messages().modify(userId="me", id=msg["id"], body={"addLabelIds": [label_id]}).execute()
        except HttpError as exc:
            logger.warning("gmail_apply_label_failed", status=exc.resp.status)
            return f"Error applying label '{label_name}': {exc}"
        except Exception as exc:
            logger.warning("gmail_apply_label_unexpected_error", error_type=type(exc).__name__)
            return f"Unexpected error: {type(exc).__name__}: {exc}"
        return f"Applied label '{label_name}' to {len(messages)} emails matching '{query}'."

    def _remove_label(self, query: str, label_name: str, count: int) -> str:
        try:
            service = self._google_api_service()
            self._resolve_label_ids(service, [label_name])
            assert self._label_cache is not None
            label_id = self._label_cache.get(label_name.lower())
            if not label_id:
                return f"Label '{label_name}' not found."

            messages = (
                service.users()
                .messages()
                .list(userId="me", q=f"{query} label:{label_name}", maxResults=count)
                .execute()
                .get("messages", [])
            )
            if not messages:
                return f"No emails found matching: '{query}' with label '{label_name}'"

            for msg in messages:
                service.users().messages().modify(
                    userId="me",
                    id=msg["id"],
                    body={"removeLabelIds": [label_id]},
                ).execute()
        except HttpError as exc:
            logger.warning("gmail_remove_label_failed", status=exc.resp.status)
            return f"Error removing label '{label_name}': {exc}"
        except Exception as exc:
            logger.warning("gmail_remove_label_unexpected_error", error_type=type(exc).__name__)
            return f"Unexpected error: {type(exc).__name__}: {exc}"
        return f"Removed label '{label_name}' from {len(messages)} emails matching '{query}'."

    def _delete_custom_label(self, label_name: str, confirm: bool) -> str:
        if not confirm:
            return (
                f"LABEL DELETION REQUIRES CONFIRMATION. This will permanently delete the label '{label_name}' "
                "from all emails. Set confirm=True to proceed."
            )
        try:
            service = self._google_api_service()
            labels = service.users().labels().list(userId="me").execute().get("labels", [])
            target = next((label for label in labels if label["name"].lower() == label_name.lower()), None)
            if not target:
                return f"Label '{label_name}' not found."
            if target.get("type") != "user":
                return f"Cannot delete system label '{label_name}'. Only user-created labels can be deleted."
            service.users().labels().delete(userId="me", id=target["id"]).execute()
            self._label_cache = None
        except HttpError as exc:
            logger.warning("gmail_delete_label_failed", status=exc.resp.status)
            return f"Error deleting label '{label_name}': {exc}"
        except Exception as exc:
            logger.warning("gmail_delete_label_unexpected_error", error_type=type(exc).__name__)
            return f"Unexpected error: {type(exc).__name__}: {exc}"
        return f"Successfully deleted label '{label_name}'. This label has been removed from all emails."

    def _dispatch_list_read(
        self,
        action: _ReadAction,
        *,
        query: str | None,
        user: str | None,
        start_date: str | None,
        range_in_days: int | None,
        count: int,
        page_token: str | None,
    ) -> str | None:
        list_specs: dict[str, tuple[str | None, str, str]] = {
            "latest": (None, "get_latest_emails", "latest emails"),
            "from_user": (f"from:{user or ''} OR to:{user or ''}", "get_emails_from_user", f"emails from {user or ''}"),
            "unread": ("is:unread", "get_unread_emails", "unread emails"),
            "starred": ("is:starred", "get_starred_emails", "starred emails"),
            "by_context": (query or "", "get_emails_by_context", "emails by context"),
            "search_emails": (query or "", "search_emails", "emails"),
        }
        if action in list_specs:
            resolved_query, operation, error_label = list_specs[action]
            return self._list_emails(
                query=resolved_query,
                count=count,
                page_token=page_token,
                operation=operation,
                error_label=error_label,
            )
        if action == "by_date":
            return self._get_emails_by_date(start_date or "", range_in_days, count, page_token)
        return None

    def gmail_read(  # noqa: PLR0911
        self,
        action: _ReadAction,
        query: str | None = None,
        user: str | None = None,
        message_id: str | None = None,
        thread_id: str | None = None,
        draft_id: str | None = None,
        start_date: str | None = None,
        range_in_days: int | None = None,
        count: int = 10,
        page_token: str | None = None,
        download_attachments: bool = False,
    ) -> str:
        """Read and search Gmail messages, threads, drafts, and custom labels.

        - `latest`: latest inbox emails (`count`, `page_token`)
        - `from_user`: emails from or to `user` (`count`, `page_token`)
        - `unread`: unread emails (`count`, `page_token`)
        - `starred`: starred emails (`count`, `page_token`)
        - `by_context`: emails matching context `query` (`count`, `page_token`)
        - `by_date`: emails from `start_date` (`YYYY/MM/DD`, optional `range_in_days`, `count`, `page_token`)
        - `by_thread`: formatted text of all messages in `thread_id`
        - `search_emails`: full search with Gmail `query` (`count`, `page_token`)
        - `get_message`: full message JSON for `message_id` (optional `download_attachments`)
        - `get_thread`: structured JSON of all messages in `thread_id`
        - `search_threads`: thread IDs and snippets matching `query` (`count`, `page_token`)
        - `get_draft`: full draft JSON for `draft_id`
        - `list_drafts`: draft IDs (`count`, `page_token`)
        - `list_custom_labels`: list user-created labels
        """
        if action not in self._read_actions:
            return json.dumps({"error": f"Gmail action {action!r} is disabled."})
        if (
            list_result := self._dispatch_list_read(
                action,
                query=query,
                user=user,
                start_date=start_date,
                range_in_days=range_in_days,
                count=count,
                page_token=page_token,
            )
        ) is not None:
            return list_result
        if action == "by_thread":
            return self._get_emails_by_thread(thread_id or "")
        if action == "get_message":
            return self._get_message(message_id or "", download_attachments)
        if action == "get_thread":
            return self._get_thread(thread_id or "")
        if action == "search_threads":
            return self._search_threads(query or "", count, page_token)
        if action == "get_draft":
            return self._get_draft(draft_id or "")
        if action == "list_drafts":
            return self._list_drafts(count, page_token)
        if action == "list_custom_labels":
            return self._list_custom_labels()
        return json.dumps({"error": f"Unsupported Gmail read action {action!r}."})

    def gmail_draft(
        self,
        action: _DraftAction,
        to: str = "",
        subject: str = "",
        body: str = "",
        draft_id: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Create or update a Gmail draft without sending.

        - `create`: save a new draft (`to`, `subject`, `body`, optional `cc`, `bcc`, `attachments`, `thread_id`, `message_id`)
        - `update`: replace `draft_id` (`to`, `subject`, `body`, optional `cc`, `bcc`, `attachments`, `thread_id`, `message_id`)
        """
        if action not in self._draft_actions:
            return json.dumps({"error": f"Gmail draft action {action!r} is disabled."})
        try:
            mime = self._prepare_outgoing_message(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc,
                attachments=attachments,
                thread_id=thread_id,
                message_id=message_id,
            )
            if isinstance(mime, str):
                return mime
            service = self._google_api_service()
            if action == "create":
                draft = service.users().drafts().create(userId="me", body={"message": mime}).execute()
                return json.dumps(draft)
            updated = service.users().drafts().update(userId="me", id=draft_id or "", body={"message": mime}).execute()
            return json.dumps({"draftId": updated["id"]})
        except HttpError as exc:
            logger.warning(f"gmail_{action}_draft_failed", status=exc.resp.status)
            return (
                json.dumps({"error": f"Gmail API error: {exc}"})
                if action == "update"
                else f"HTTP Error creating draft: {exc}"
            )
        except Exception as exc:
            logger.warning(f"gmail_{action}_draft_unexpected_error", error_type=type(exc).__name__)
            return (
                json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                if action == "update"
                else f"Error creating draft: {type(exc).__name__}: {exc}"
            )

    def gmail_send(
        self,
        action: _SendAction,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Send an email or thread reply to external recipients immediately.

        - `send`: send email (`to`, `subject`, `body`, optional `cc`, `bcc`, `attachments`, `thread_id`, `message_id`)
        - `reply`: reply in thread (`thread_id`, `message_id`, `to`, `subject`, `body`, optional `cc`, `attachments`)
        """
        if action not in self._send_actions:
            return json.dumps({"error": f"Gmail send action {action!r} is disabled."})
        if action == "send":
            return self._send_email_impl(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc,
                attachments=attachments,
                thread_id=thread_id,
                message_id=message_id,
                error_verb="sending email",
            )
        if action == "reply":
            return self._send_email_impl(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                attachments=attachments,
                thread_id=thread_id or "",
                message_id=message_id or "",
                is_reply=True,
                error_verb="sending reply",
            )
        return json.dumps({"error": f"Unsupported Gmail send action {action!r}."})

    def gmail_send_to_self(self, subject: str, body: str) -> str:
        """Send an email strictly to the connected Gmail account's own address."""
        service = self._google_api_service()
        profile = service.users().getProfile(userId="me").execute()
        email_address = profile.get("emailAddress") if isinstance(profile, dict) else None
        if (
            not isinstance(email_address, str)
            or email_address != email_address.strip()
            or not _validate_email(email_address)
        ):
            msg = "Connected Gmail profile did not return one valid email address"
            raise RuntimeError(msg)
        return self._send_email_impl(to=email_address, subject=subject, body=body)

    def gmail_manage(
        self,
        action: _ManageAction,
        message_id: str | None = None,
        label_name: str | None = None,
        query: str | None = None,
        count: int = 10,
        confirm: bool = False,
    ) -> str:
        """Organize Gmail messages and manage custom labels.

        - `mark_read`: mark `message_id` as read
        - `mark_unread`: mark `message_id` as unread
        - `star`: star `message_id`
        - `unstar`: unstar `message_id`
        - `apply_label`: apply (or create) `label_name` to up to `count` emails matching `query`
        - `remove_label`: remove `label_name` from up to `count` emails matching `query`
        - `delete_custom_label`: permanently delete user label `label_name` (requires `confirm=True`)
        """
        if action not in self._manage_actions:
            return json.dumps({"error": f"Gmail manage action {action!r} is disabled."})
        if action in _SIMPLE_LABEL_MUTATIONS:
            body, template, error_label = _SIMPLE_LABEL_MUTATIONS[action]
            msg_id = message_id or ""
            return self._modify_simple_label(
                message_id=msg_id,
                body=body,
                success_message=template.format(id=msg_id),
                error_label=error_label,
            )
        if action == "apply_label":
            return self._apply_label(query or "", label_name or "", count)
        if action == "remove_label":
            return self._remove_label(query or "", label_name or "", count)
        if action == "delete_custom_label":
            return self._delete_custom_label(label_name or "", confirm)
        return json.dumps({"error": f"Unsupported Gmail manage action {action!r}."})
