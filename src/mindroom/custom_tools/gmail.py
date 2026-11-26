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
from typing import TYPE_CHECKING, Any, ClassVar

from googleapiclient.errors import HttpError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit, visible_google_function_map
from mindroom.file_access import resolve_agent_file
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
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
_GMAIL_MODIFY_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
    },
)
_GMAIL_SEND_TOOLS = frozenset({"send_email", "send_email_reply"})
_GMAIL_DRAFT_TOOLS = frozenset({"create_draft_email", "send_draft", "update_draft"})
_GMAIL_READ_TOOLS = frozenset(
    {
        "get_latest_emails",
        "get_emails_from_user",
        "get_unread_emails",
        "get_starred_emails",
        "get_emails_by_context",
        "get_emails_by_date",
        "get_emails_by_thread",
        "search_emails",
        "list_custom_labels",
        "get_message",
        "get_thread",
        "search_threads",
        "list_labels",
        "get_draft",
        "list_drafts",
        "download_attachment",
    },
)
_GMAIL_MODIFY_TOOLS = frozenset(
    {
        "mark_email_as_read",
        "mark_email_as_unread",
        "star_email",
        "unstar_email",
        "archive_email",
        "apply_label",
        "remove_label",
        "delete_custom_label",
        "modify_message_labels",
        "modify_thread_labels",
        "trash_message",
        "trash_thread",
    },
)
# Bounds raw attachment bytes held in memory across a single message.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_GMAIL_QUERY_INSTRUCTIONS = textwrap.dedent("""\
    You have access to Gmail tools for reading, composing, and organizing emails.

    ## Pagination
    List methods return `nextPageToken` in the response when more results exist.
    Pass this value as the `page_token` parameter to fetch the next page.

    ## Gmail Query Syntax
    Use these operators in search and context query parameters:
    - `from:user@example.com` / `to:user@example.com` — filter by sender/recipient
    - `subject:"meeting notes"` — filter by subject
    - `is:unread` / `is:starred` / `is:important` — filter by status
    - `has:attachment` — emails with attachments
    - `newer_than:7d` / `older_than:1m` — relative date (d=days, m=months, y=years)
    - `after:2024/01/01` / `before:2024/12/31` — absolute date range
    - `label:work` — filter by label
    - `from:me` — emails sent by the user
    - Combine with spaces (AND): `from:me newer_than:7d has:attachment`""")

_COMPOSE_HINT_NAMES = ("send_email", "create_draft_email", "send_email_reply")


def _validate_email(email: str) -> bool:
    """Validate email address syntax."""
    return bool(_EMAIL_PATTERN.match(email.strip()))


def _compose_instructions_for(visible: dict[str, str]) -> str:
    lines: list[str] = []
    send_name = visible.get("send_email")
    draft_name = visible.get("create_draft_email")
    reply_name = visible.get("send_email_reply")

    if send_name and draft_name:
        lines.append(f"- **New email:** `{send_name}(to, subject, body)` or `{draft_name}(to, subject, body)`")
    elif send_name:
        lines.append(f"- **New email:** `{send_name}(to, subject, body)`")
    elif draft_name:
        lines.append(f"- **New email:** `{draft_name}(to, subject, body)`")
    if reply_name:
        lines.append(
            f"- **Reply (send now):** `{reply_name}(thread_id, message_id, to, subject, body)` "
            "keeps the message in the thread",
        )
    if draft_name:
        lines.append(
            f"- **Reply (draft):** `{draft_name}(to, subject, body, thread_id=..., message_id=...)` "
            "creates a draft reply in the thread. Get thread_id and message_id from the original message first.",
        )
    if not lines:
        return ""
    return "\n## Composing Emails\n" + "\n".join(lines)


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
        return "No emails found"
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


def _filter_tools(
    tools: list[tuple[str, Callable[..., str]]],
    include_tools: list[str] | None,
    exclude_tools: list[str] | None,
) -> list[Callable[..., str]]:
    if include_tools is not None:
        included = set(include_tools)
        tools = [(name, fn) for name, fn in tools if name in included]
    if exclude_tools is not None:
        excluded = set(exclude_tools)
        tools = [(name, fn) for name, fn in tools if name not in excluded]
    return [fn for _name, fn in tools]


def _validate_gmail_scopes(registered_names: Collection[str], scopes: Collection[str]) -> None:
    """Ensure explicit scopes cover every registered Gmail operation."""
    registered = set(registered_names)
    scope_set = set(scopes)
    if registered & _GMAIL_DRAFT_TOOLS and not (_GMAIL_DRAFT_SCOPES & scope_set):
        msg = "A Gmail compose or modify scope is required for draft operations"
        raise ValueError(msg)
    if registered & _GMAIL_SEND_TOOLS and not (_GMAIL_SEND_SCOPES & scope_set):
        msg = "A Gmail send, compose, or modify scope is required for email sending operations"
        raise ValueError(msg)
    if registered & _GMAIL_READ_TOOLS and not (_GMAIL_READ_SCOPES & scope_set):
        msg = "A Gmail read or modify scope is required for email reading operations"
        raise ValueError(msg)
    if registered & _GMAIL_MODIFY_TOOLS and not (_GMAIL_MODIFY_SCOPES & scope_set):
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
        scopes: list[str] | None = None,
        include_html: bool = False,
        max_body_length: int | None = None,
        attachment_dir: str | None = None,
        max_results: int = 20,
        max_batch_size: int = 10,
        include_tools: list[str] | None = None,
        exclude_tools: list[str] | None = None,
        instructions: str | None = None,
        # Reading
        get_latest_emails: bool = True,
        get_emails_from_user: bool = True,
        get_unread_emails: bool = True,
        get_starred_emails: bool = True,
        get_emails_by_context: bool = True,
        get_emails_by_date: bool = True,
        get_emails_by_thread: bool = True,
        search_emails: bool = True,
        # Management
        mark_email_as_read: bool = True,
        mark_email_as_unread: bool = True,
        star_email: bool = True,
        unstar_email: bool = True,
        archive_email: bool = False,
        # Composing
        create_draft_email: bool = True,
        send_email: bool = True,
        send_email_reply: bool = True,
        # Labels
        list_custom_labels: bool = True,
        apply_label: bool = True,
        remove_label: bool = True,
        delete_custom_label: bool = True,
        # Threads, drafts, messages
        get_message: bool = True,
        get_thread: bool = True,
        search_threads: bool = True,
        modify_thread_labels: bool = False,
        trash_thread: bool = False,
        get_draft: bool = True,
        list_drafts: bool = True,
        send_draft: bool = False,
        update_draft: bool = True,
        list_labels: bool = False,
        modify_message_labels: bool = False,
        trash_message: bool = False,
        download_attachment: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        self._custom_instructions = instructions
        self.scopes = list(self.default_scopes) if scopes is None else list(scopes)
        self.include_html = include_html
        self.max_body_length = max_body_length
        self.attachment_dir = attachment_dir
        self.max_results = max_results
        self.max_batch_size = max(min(max_batch_size, 100), 1)
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None

        toggles: tuple[tuple[bool, str, Callable[..., str]], ...] = (
            (get_latest_emails, "get_latest_emails", self.get_latest_emails),
            (get_emails_from_user, "get_emails_from_user", self.get_emails_from_user),
            (get_unread_emails, "get_unread_emails", self.get_unread_emails),
            (get_starred_emails, "get_starred_emails", self.get_starred_emails),
            (get_emails_by_context, "get_emails_by_context", self.get_emails_by_context),
            (get_emails_by_date, "get_emails_by_date", self.get_emails_by_date),
            (get_emails_by_thread, "get_emails_by_thread", self.get_emails_by_thread),
            (search_emails, "search_emails", self.search_emails),
            (mark_email_as_read, "mark_email_as_read", self.mark_email_as_read),
            (mark_email_as_unread, "mark_email_as_unread", self.mark_email_as_unread),
            (star_email, "star_email", self.star_email),
            (unstar_email, "unstar_email", self.unstar_email),
            (archive_email, "archive_email", self.archive_email),
            (create_draft_email, "create_draft_email", self.create_draft_email),
            (send_email, "send_email", self.send_email),
            (send_email_reply, "send_email_reply", self.send_email_reply),
            (list_custom_labels, "list_custom_labels", self.list_custom_labels),
            (apply_label, "apply_label", self.apply_label),
            (remove_label, "remove_label", self.remove_label),
            (delete_custom_label, "delete_custom_label", self.delete_custom_label),
            (get_message, "get_message", self.get_message),
            (get_thread, "get_thread", self.get_thread),
            (search_threads, "search_threads", self.search_threads),
            (modify_thread_labels, "modify_thread_labels", self.modify_thread_labels),
            (trash_thread, "trash_thread", self.trash_thread),
            (get_draft, "get_draft", self.get_draft),
            (list_drafts, "list_drafts", self.list_drafts),
            (send_draft, "send_draft", self.send_draft),
            (update_draft, "update_draft", self.update_draft),
            (list_labels, "list_labels", self.list_labels),
            (modify_message_labels, "modify_message_labels", self.modify_message_labels),
            (trash_message, "trash_message", self.trash_message),
            (download_attachment, "download_attachment", self.download_attachment),
        )
        named_tools: list[tuple[str, Callable[..., str]]] = [
            (name, method) for enabled, name, method in toggles if enabled
        ]
        if _GMAIL_PROFILE_SCOPES.intersection(self.scopes) and _GMAIL_SEND_SCOPES.intersection(self.scopes):
            named_tools.append(("send_email_to_self", self.send_email_to_self))
        else:
            logger.warning("gmail_send_email_to_self_disabled_missing_scope")

        tools = _filter_tools(named_tools, include_tools, exclude_tools)

        super().__init__(
            name="gmail",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )
        _validate_gmail_scopes(self.functions, self.scopes)

    def _service_account_scopes(self) -> tuple[str, ...]:
        """Return Gmail service-account scopes, honoring explicit constructor scopes."""
        return tuple(self.scopes)

    @property
    def instructions(self) -> str:  # ty: ignore[incompatible-property-override]
        """Return instructions scoped to the currently registered Gmail functions."""
        return self.instructions_for(self.functions)

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return Gmail guidance naming only compose functions offered in ``function_names``."""
        if self._custom_instructions is not None:
            return self._custom_instructions
        if not function_names:
            return ""
        compose_section = _compose_instructions_for(visible_google_function_map(function_names, _COMPOSE_HINT_NAMES))
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

    def _batch_get(
        self,
        ids: list[str],
        request_builder: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        """Execute Gmail batches while retaining final per-item authorization rejection."""
        results: list[dict[str, Any]] = []
        service = self.service
        assert service is not None

        def callback(request_id: str, response: Any, exception: Exception | None) -> None:  # noqa: ANN401
            if exception is None:
                results.append(response)
                return
            if isinstance(exception, HttpError) and exception.resp.status == 401:
                self._mark_google_authorization_rejected()
            logger.warning(
                "gmail_batch_request_failed",
                request_id=request_id,
                error_type=type(exception).__name__,
            )
            results.append({"id": request_id, "error": "Google request failed"})

        for offset in range(0, len(ids), self.max_batch_size):
            chunk = ids[offset : offset + self.max_batch_size]
            batch = service.new_batch_http_request(callback=callback)
            for item_id in chunk:
                batch.add(request_builder(item_id), request_id=item_id)
            batch.execute()
        return results

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
        """Load attachments, validate fields, and build a Gmail MIME payload.

        Returns a JSON error string if attachment loading fails, raises ``ValueError``
        for invalid email parameters, or returns the encoded MIME payload dict.
        """
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

    def get_latest_emails(self, count: int = 10, page_token: str | None = None) -> str:
        """Get the latest X emails from the user's inbox.

        Args:
            count: Number of latest emails to retrieve.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
        return self._list_emails(
            query=None,
            count=count,
            page_token=page_token,
            operation="get_latest_emails",
            error_label="latest emails",
        )

    def get_emails_from_user(self, user: str = "", count: int = 10, page_token: str | None = None) -> str:
        """Get X number of emails from a specific user (name or email).

        Args:
            user: Name or email address of the sender.
            count: Maximum number of emails to retrieve.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
        query = f"from:{user}" if "@" in user else f"from:{user}*"
        return self._list_emails(
            query=query,
            count=count,
            page_token=page_token,
            operation="get_emails_from_user",
            error_label=f"emails from {user}",
        )

    def get_unread_emails(self, count: int = 10, page_token: str | None = None) -> str:
        """Get the X number of latest unread emails from the user's inbox.

        Args:
            count: Maximum number of unread emails to retrieve.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
        return self._list_emails(
            query="is:unread",
            count=count,
            page_token=page_token,
            operation="get_unread_emails",
            error_label="unread emails",
        )

    def get_starred_emails(self, count: int = 10, page_token: str | None = None) -> str:
        """Get X number of starred emails from the user's inbox.

        Args:
            count: Maximum number of starred emails to retrieve.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
        return self._list_emails(
            query="is:starred",
            count=count,
            page_token=page_token,
            operation="get_starred_emails",
            error_label="starred emails",
        )

    def get_emails_by_context(self, context: str = "", count: int = 10, page_token: str | None = None) -> str:
        """Get X number of emails matching a specific context or search term.

        Args:
            context: Search term or context to match in emails.
            count: Maximum number of emails to retrieve.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
        return self._list_emails(
            query=context,
            count=count,
            page_token=page_token,
            operation="get_emails_by_context",
            error_label=f"emails by context '{context}'",
        )

    def get_emails_by_date(
        self,
        start_date: str = "",
        range_in_days: int | None = None,
        num_emails: int | None = 10,
        page_token: str | None = None,
    ) -> str:
        """Get emails from a date or date range.

        Args:
            start_date: Start date in YYYY/MM/DD format (e.g. "2026/03/01").
            range_in_days: Number of days to include in the range (default: None, meaning all emails after start_date).
            num_emails: Maximum number of emails to retrieve (default: 10).
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
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
            count=num_emails or 10,
            page_token=page_token,
            operation="get_emails_by_date",
            error_label="emails by date",
        )

    def get_emails_by_thread(self, thread_id: str = "") -> str:
        """Retrieve all emails from a specific thread.

        Args:
            thread_id: The ID of the email thread.

        Returns:
            Formatted string containing email thread details.

        """
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

    def search_emails(self, query: str = "", count: int = 10, page_token: str | None = None) -> str:
        """Get X number of emails based on a given natural text query.

        Searches in to, from, cc, subject and email body contents.

        Args:
            query: Natural language query to search for.
            count: Number of emails to retrieve.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON with emails array and nextPageToken if more results exist.

        """
        return self._list_emails(
            query=query,
            count=count,
            page_token=page_token,
            operation="search_emails",
            error_label="emails",
        )

    def mark_email_as_read(self, message_id: str = "") -> str:
        """Mark a specific email as read by removing the 'UNREAD' label.

        Args:
            message_id: The ID of the message to mark as read.

        Returns:
            Success message or error description.

        """
        return self._modify_simple_label(
            message_id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
            success_message=f"Successfully marked email {message_id} as read. Labels removed: UNREAD",
            error_label="marking as read",
        )

    def mark_email_as_unread(self, message_id: str = "") -> str:
        """Mark a specific email as unread by adding the 'UNREAD' label.

        Args:
            message_id: The ID of the message to mark as unread.

        Returns:
            Success message or error description.

        """
        return self._modify_simple_label(
            message_id=message_id,
            body={"addLabelIds": ["UNREAD"]},
            success_message=f"Successfully marked email {message_id} as unread. Labels added: UNREAD",
            error_label="marking as unread",
        )

    def star_email(self, message_id: str = "") -> str:
        """Add a star to an email message.

        Args:
            message_id: The ID of the message to star.

        Returns:
            Success message or error description.

        """
        return self._modify_simple_label(
            message_id=message_id,
            body={"addLabelIds": ["STARRED"]},
            success_message=f"Successfully starred email {message_id}",
            error_label="starring",
        )

    def unstar_email(self, message_id: str = "") -> str:
        """Remove the star from an email message.

        Args:
            message_id: The ID of the message to unstar.

        Returns:
            Success message or error description.

        """
        return self._modify_simple_label(
            message_id=message_id,
            body={"removeLabelIds": ["STARRED"]},
            success_message=f"Successfully unstarred email {message_id}",
            error_label="unstarring",
        )

    def archive_email(self, message_id: str = "") -> str:
        """Archive an email by removing it from the inbox.

        The email is NOT deleted and can still be found via search.

        Args:
            message_id: The ID of the message to archive.

        Returns:
            Success message or error description.

        """
        return self._modify_simple_label(
            message_id=message_id,
            body={"removeLabelIds": ["INBOX"]},
            success_message=f"Successfully archived email {message_id}",
            error_label="archiving",
        )

    def create_draft_email(
        self,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Create and save a draft email.

        Args:
            to: Comma-separated recipient email addresses.
            subject: Email subject.
            body: Email body content.
            cc: Comma-separated CC email addresses (optional).
            bcc: Comma-separated BCC email addresses (optional).
            attachments: File path(s) for attachments (optional).
            thread_id: Thread ID for reply drafts (optional).
            message_id: Message ID being replied to, used with thread_id (optional).

        Returns:
            JSON string containing the draft email details including id.

        """
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
            draft = service.users().drafts().create(userId="me", body={"message": mime}).execute()
        except HttpError as exc:
            logger.warning("gmail_create_draft_failed", status=exc.resp.status)
            return f"HTTP Error creating draft: {exc}"
        except Exception as exc:
            logger.warning("gmail_create_draft_unexpected_error", error_type=type(exc).__name__)
            return f"Error creating draft: {type(exc).__name__}: {exc}"
        return json.dumps(draft)

    def send_email(
        self,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Send an email immediately.

        Args:
            to: Comma-separated recipient email addresses.
            subject: Email subject.
            body: Email body content.
            cc: Comma-separated CC email addresses (optional).
            bcc: Comma-separated BCC email addresses (optional).
            attachments: File path(s) for attachments (optional).
            thread_id: Thread ID to send as a reply (optional).
            message_id: Message ID being replied to, used with thread_id (optional).

        Returns:
            JSON string containing the sent email details including id.

        """
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

    def send_email_reply(
        self,
        thread_id: str = "",
        message_id: str = "",
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str | None = None,
        attachments: str | list[str] | None = None,
    ) -> str:
        """Respond to an existing email thread.

        Args:
            thread_id: The ID of the email thread to reply to.
            message_id: The ID of the email being replied to.
            to: Comma-separated recipient email addresses.
            subject: Email subject (prefixed with "Re:" if not already).
            body: Email body content.
            cc: Comma-separated CC email addresses (optional).
            attachments: File path(s) for attachments (optional).

        Returns:
            JSON string containing sent email details including id.

        """
        return self._send_email_impl(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            attachments=attachments,
            thread_id=thread_id,
            message_id=message_id,
            is_reply=True,
            error_verb="sending reply",
        )

    def send_email_to_self(self, subject: str, body: str) -> str:
        """Send an email to the connected Gmail account.

        Args:
            subject: Email subject.
            body: Email body content.

        Returns:
            Stringified dictionary containing the sent email details.

        """
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

    def list_custom_labels(self) -> str:
        """List only user-created custom labels (filters out system labels) in a numbered format.

        Returns:
            A numbered list of custom labels only.

        """
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
            return "No custom labels found.\nCreate labels using apply_label function!"
        numbered = [f"{idx}. {name}" for idx, name in enumerate(custom_labels, 1)]
        return f"Your Custom Labels ({len(custom_labels)} total):\n\n" + "\n".join(numbered)

    def apply_label(self, context: str = "", label_name: str = "", count: int = 10) -> str:
        """Find emails matching a context (search query) and apply a label, creating it if necessary.

        Args:
            context: Gmail search query (e.g., 'is:unread category:promotions').
            label_name: Name of the label to apply.
            count: Maximum number of emails to process.

        Returns:
            Summary of labeled emails.

        """
        try:
            service = self._google_api_service()
            messages = (
                service.users().messages().list(userId="me", q=context, maxResults=count).execute().get("messages", [])
            )
            if not messages:
                return f"No emails found matching: '{context}'"

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
        return f"Applied label '{label_name}' to {len(messages)} emails matching '{context}'."

    def remove_label(self, context: str = "", label_name: str = "", count: int = 10) -> str:
        """Remove a label from emails matching a context (search query).

        Args:
            context: Gmail search query.
            label_name: Name of the label to remove.
            count: Maximum number of emails to process.

        Returns:
            Summary of updated emails.

        """
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
                .list(userId="me", q=f"{context} label:{label_name}", maxResults=count)
                .execute()
                .get("messages", [])
            )
            if not messages:
                return f"No emails found matching: '{context}' with label '{label_name}'"

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
        return f"Removed label '{label_name}' from {len(messages)} emails matching '{context}'."

    def delete_custom_label(self, label_name: str = "", confirm: bool = False) -> str:
        """Delete a custom label (with safety confirmation).

        Args:
            label_name: Name of the label to delete.
            confirm: Must be True to actually delete the label.

        Returns:
            Confirmation message or warning.

        """
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

    def get_message(self, message_id: str = "", download_attachments: bool = False) -> str:
        """Get a single email message by its ID with full content including headers, body, and attachment metadata.

        Args:
            message_id: The Gmail message ID.
            download_attachments: If True, download attachments to disk and include file paths in the response.

        Returns:
            JSON string with message content including id, threadId, subject, from, to, date, body, and attachments.

        """
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

    def get_thread(self, thread_id: str = "") -> str:
        """Get all messages in a Gmail thread as structured JSON.

        Args:
            thread_id: The Gmail thread ID.

        Returns:
            JSON string with thread metadata and all messages in chronological order.

        """
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

    def search_threads(self, query: str = "", count: int = 10, page_token: str | None = None) -> str:
        """Search Gmail threads using Gmail query syntax.

        Returns thread IDs and snippets, not full message content.

        Args:
            query: Gmail search query string. Supports all Gmail operators like from:, to:, subject:, is:unread, etc.
            count: Maximum number of threads to return (default 10, max 500).
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string with list of matching threads and nextPageToken if more results exist.

        """
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

    def _build_label_modify_body(
        self,
        service: Any,  # noqa: ANN401
        add_labels: str | None,
        remove_labels: str | None,
    ) -> dict[str, list[str]]:
        body: dict[str, list[str]] = {}
        if add_labels:
            names = [name.strip() for name in add_labels.split(",") if name.strip()]
            body["addLabelIds"] = self._resolve_label_ids(service, names)
        if remove_labels:
            names = [name.strip() for name in remove_labels.split(",") if name.strip()]
            body["removeLabelIds"] = self._resolve_label_ids(service, names)
        return body

    def modify_thread_labels(
        self,
        thread_id: str = "",
        add_labels: str | None = None,
        remove_labels: str | None = None,
    ) -> str:
        """Add or remove labels from an entire thread (all messages in the conversation).

        Args:
            thread_id: The Gmail thread ID.
            add_labels: Comma-separated label names to add (e.g. 'STARRED,Important').
            remove_labels: Comma-separated label names to remove (e.g. 'UNREAD,INBOX').

        Returns:
            JSON string with updated thread label state.

        """
        try:
            service = self._google_api_service()
            body = self._build_label_modify_body(service, add_labels, remove_labels)
            if not body:
                return json.dumps({"error": "Must specify add_labels or remove_labels"})
            result = service.users().threads().modify(userId="me", id=thread_id, body=body).execute()
        except HttpError as exc:
            logger.warning("gmail_modify_thread_labels_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_modify_thread_labels_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"threadId": result["id"], "labelIds": result.get("labelIds", [])})

    def trash_thread(self, thread_id: str = "") -> str:
        """Move an entire thread to the trash. All messages in the conversation will be trashed.

        Args:
            thread_id: The Gmail thread ID to trash.

        Returns:
            JSON string confirming the thread was trashed.

        """
        try:
            service = self._google_api_service()
            service.users().threads().trash(userId="me", id=thread_id).execute()
        except HttpError as exc:
            logger.warning("gmail_trash_thread_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_trash_thread_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"threadId": thread_id, "action": "trashed"})

    def get_draft(self, draft_id: str = "") -> str:
        """Get a draft email by its ID with full message content.

        Args:
            draft_id: The Gmail draft ID.

        Returns:
            JSON string with draft ID and full message details.

        """
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

    def list_drafts(self, count: int = 10, page_token: str | None = None) -> str:
        """List draft emails in the mailbox.

        Args:
            count: Maximum number of drafts to return (default 10, max 500).
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string with list of draft IDs and nextPageToken if more results exist.

        """
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

    def send_draft(self, draft_id: str = "") -> str:
        """Send an existing draft email.

        Args:
            draft_id: The Gmail draft ID to send.

        Returns:
            JSON string with sent message ID, thread ID, and labels.

        """
        try:
            service = self._google_api_service()
            result = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        except HttpError as exc:
            logger.warning("gmail_send_draft_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_send_draft_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps(
            {
                "id": result.get("id"),
                "threadId": result.get("threadId"),
                "labelIds": result.get("labelIds", []),
            },
        )

    def update_draft(
        self,
        draft_id: str = "",
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        attachments: str | list[str] | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Replace the content of an existing draft email.

        Args:
            draft_id: The Gmail draft ID to update.
            to: Comma-separated recipient email addresses.
            subject: Email subject line.
            body: Email body content.
            cc: Comma-separated CC email addresses (optional).
            bcc: Comma-separated BCC email addresses (optional).
            attachments: File path(s) for attachments (optional).
            thread_id: Thread ID for reply drafts (optional).
            message_id: Message ID being replied to, used with thread_id (optional).

        Returns:
            JSON string with updated draft ID.

        """
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
            result = service.users().drafts().update(userId="me", id=draft_id, body={"message": mime}).execute()
        except HttpError as exc:
            logger.warning("gmail_update_draft_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_update_draft_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"draftId": result["id"]})

    def list_labels(self) -> str:
        """List all Gmail labels (system and custom) with message and thread counts.

        Returns:
            JSON string with list of label objects including name, type, and message/thread counts.

        """
        try:
            service = self._google_api_service()
            labels = service.users().labels().list(userId="me").execute().get("labels", [])
            label_ids = [label["id"] for label in labels]
            detailed = self._batch_get(label_ids, lambda lid: service.users().labels().get(userId="me", id=lid))
        except HttpError as exc:
            logger.warning("gmail_list_labels_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_list_labels_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})

        formatted = [
            {
                "id": detail["id"],
                "name": detail["name"],
                "type": detail.get("type", "system"),
                "messagesTotal": detail.get("messagesTotal", 0),
                "messagesUnread": detail.get("messagesUnread", 0),
                "threadsTotal": detail.get("threadsTotal", 0),
                "threadsUnread": detail.get("threadsUnread", 0),
            }
            for detail in detailed
            if "error" not in detail
        ]
        return json.dumps({"labels": formatted, "count": len(formatted)})

    def modify_message_labels(
        self,
        message_id: str = "",
        add_labels: str | None = None,
        remove_labels: str | None = None,
    ) -> str:
        """Add or remove labels from a single message.

        Use for marking read/unread, starring, categorizing, etc.
        For example: add_labels="STARRED" or remove_labels="UNREAD" to mark as read.

        Args:
            message_id: The Gmail message ID.
            add_labels: Comma-separated label names to add (e.g. 'STARRED,Work').
            remove_labels: Comma-separated label names to remove (e.g. 'UNREAD,INBOX').

        Returns:
            JSON string with updated message label state.

        """
        try:
            service = self._google_api_service()
            body = self._build_label_modify_body(service, add_labels, remove_labels)
            if not body:
                return json.dumps({"error": "Must specify add_labels or remove_labels"})
            result = service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        except HttpError as exc:
            logger.warning("gmail_modify_message_labels_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_modify_message_labels_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"id": result["id"], "labelIds": result.get("labelIds", [])})

    def trash_message(self, message_id: str = "", undo: bool = False) -> str:
        """Move a message to trash, or restore it from trash with undo=True.

        Args:
            message_id: The Gmail message ID.
            undo: If True, restore the message from trash instead of trashing it.

        Returns:
            JSON string confirming the action.

        """
        try:
            service = self._google_api_service()
            if undo:
                service.users().messages().untrash(userId="me", id=message_id).execute()
                action = "untrashed"
            else:
                service.users().messages().trash(userId="me", id=message_id).execute()
                action = "trashed"
        except HttpError as exc:
            logger.warning("gmail_trash_message_failed", undo=undo, status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_trash_message_unexpected_error", undo=undo, error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"id": message_id, "action": action})

    def download_attachment(
        self,
        message_id: str = "",
        attachment_id: str = "",
        filename: str = "",
    ) -> str:
        """Download an email attachment to disk. Use get_message first to find attachment IDs.

        Args:
            message_id: The Gmail message ID containing the attachment.
            attachment_id: The attachment ID from the message's attachment metadata.
            filename: The filename to save the attachment as.

        Returns:
            JSON string with the local file path where the attachment was saved.

        """
        try:
            service = self._google_api_service()
            local_path = self._download_attachment_file(service, message_id, attachment_id, filename)
        except HttpError as exc:
            logger.warning("gmail_download_attachment_failed", status=exc.resp.status)
            return json.dumps({"error": f"Gmail API error: {exc}"})
        except Exception as exc:
            logger.warning("gmail_download_attachment_unexpected_error", error_type=type(exc).__name__)
            return json.dumps({"error": f"Unexpected error: {type(exc).__name__}: {exc}"})
        return json.dumps({"localPath": local_path, "filename": filename, "messageId": message_id})
