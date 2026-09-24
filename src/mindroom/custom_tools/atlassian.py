"""Jira and Confluence Cloud tools that act as the requester through Atlassian OAuth."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import unicodedata
from email.message import Message
from email.utils import collapse_rfc2231_value
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from agno.tools import Toolkit

from mindroom.attachments import register_bytes_attachment
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.atlassian_client import (
    AtlassianAccessRejectedError,
    AtlassianError,
    AtlassianSite,
    AtlassianSitePin,
    accessible_sites,
    download,
    normalize_cloud_id,
    normalize_site_url,
    request_json,
    select_site,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.logging_config import get_logger
from mindroom.oauth.atlassian import (
    ATLASSIAN_PRODUCTS,
    AtlassianProduct,
    atlassian_function_names,
    atlassian_oauth_provider,
    atlassian_product_scopes,
)
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    oauth_credentials_usable,
    refresh_oauth_credentials_with_result,
)
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
from mindroom.tool_system.runtime_context import append_tool_runtime_attachment_id, get_tool_runtime_context
from mindroom.tool_system.sandbox_proxy import inline_attachment_byte_limit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_MAX_PAGE_SIZE = 50
_PAGE_ID_PATTERN = re.compile(r"[0-9]{1,20}")
_ATTACHMENT_ID_PATTERN = re.compile(r"(?:att)?([0-9]{1,20})")
_ISSUE_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}-[0-9]{1,20}|[0-9]{1,20}")
_PROJECT_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}")
_SPACE_KEY_PATTERN = re.compile(r"~?[A-Za-z0-9_-]{1,255}")
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9._~+/=%-]{1,2048}")
_MIME_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}")
_MAX_FILENAME_CHARS = 200
_DEFAULT_ISSUE_FIELDS = ("summary", "status", "assignee", "reporter", "priority", "issuetype", "updated")
_ATTACHMENT_USAGE = (
    "Usable in this turn only. Open it with get_attachment(attachment_id), or save it with "
    "get_attachment(attachment_id, mindroom_output_path=...); download it again in a later turn."
)


class _InvalidArgumentError(AtlassianError):
    """A model-supplied argument was rejected before any authentication or network access."""

    def __init__(self, message: str) -> None:
        super().__init__(code="invalid_argument", message=message)


def _page_id(value: object, *, field_name: str = "page_id") -> str:
    if isinstance(value, str) and value.isascii() and _PAGE_ID_PATTERN.fullmatch(value):
        return value
    msg = f"{field_name} must be a numeric Confluence page ID, not a URL, title, or path."
    raise _InvalidArgumentError(msg)


def _confluence_attachment_id(value: object) -> str:
    match = _ATTACHMENT_ID_PATTERN.fullmatch(value) if isinstance(value, str) and value.isascii() else None
    if match is None:
        msg = "attachment_id must be a Confluence attachment ID from confluence_list_attachments, such as att123456."
        raise _InvalidArgumentError(msg)
    return f"att{match.group(1)}"


def _issue_key(value: object) -> str:
    if isinstance(value, str) and value.isascii() and _ISSUE_KEY_PATTERN.fullmatch(value):
        return value.upper()
    msg = "issue_key must be a Jira issue key such as PROJ-123, or a numeric issue ID."
    raise _InvalidArgumentError(msg)


def _required_text(value: object, field_name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    msg = f"{field_name} must be a non-empty string."
    raise _InvalidArgumentError(msg)


def _page_size(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{field_name} must be an integer."
        raise _InvalidArgumentError(msg)
    return max(1, min(value, _MAX_PAGE_SIZE))


def _cursor(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _CURSOR_PATTERN.fullmatch(value):
        return value
    msg = f"{field_name} must be the unchanged value returned by a previous call."
    raise _InvalidArgumentError(msg)


def _string_list(value: object, field_name: str) -> list[str] | None:
    if value is None:
        return None
    raw_items = cast("list[object]", value) if isinstance(value, list) else []
    items = [item.strip() for item in raw_items if isinstance(item, str) and item.strip()]
    if not raw_items or len(items) != len(raw_items):
        msg = f"{field_name} must be a non-empty list of strings."
        raise _InvalidArgumentError(msg)
    return items


def _extra_fields(value: object) -> dict[str, object]:
    if value is None:
        return {}
    fields = cast("dict[object, object]", value) if isinstance(value, dict) else None
    if fields is None or not all(isinstance(key, str) for key in fields):
        msg = "fields must be an object mapping Jira field IDs to values."
        raise _InvalidArgumentError(msg)
    return {str(key): item for key, item in fields.items()}


def _adf_document(text: str) -> dict[str, object]:
    """Convert plain text into an Atlassian Document Format document, one paragraph per blank-line block."""
    paragraphs: list[dict[str, object]] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        if not lines:
            continue
        content: list[dict[str, object]] = []
        for index, line in enumerate(lines):
            if index:
                content.append({"type": "hardBreak"})
            content.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _mapping(value: object) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else {}


def _absolute_url(base: object, relative: object) -> str | None:
    """Join an Atlassian `_links.base` and relative web link, accepting only HTTPS bases."""
    if not isinstance(base, str) or not isinstance(relative, str) or not relative.startswith("/"):
        return None
    return f"{base.rstrip('/')}{relative}" if base.startswith("https://") else None


def _next_cursor(links: object) -> tuple[bool, str | None]:
    """Return whether Atlassian links a next page, and its cursor when it can be passed back unchanged."""
    next_link = _mapping(links).get("next")
    if not isinstance(next_link, str) or not next_link:
        return False, None
    # The cursor is a percent-encoded URL component, not form data, so a raw "+" is literal.
    for field in urlsplit(next_link).query.split("&"):
        name, separator, value = field.partition("=")
        if separator and unquote(name) == "cursor":
            cursor = unquote(value)
            return True, cursor if _CURSOR_PATTERN.fullmatch(cursor) else None
    return True, None


def _page_fields(has_more: bool, next_cursor: str | None) -> dict[str, object]:
    fields: dict[str, object] = {"has_more": has_more, "next_cursor": next_cursor}
    if has_more and next_cursor is None:
        fields["warning"] = (
            "More results exist, but Atlassian returned no usable cursor, so this listing is incomplete."
        )
    return fields


def _content_disposition_filenames(content_disposition: str | None) -> list[str]:
    """Return raw Content-Disposition filenames, RFC 5987 ``filename*`` first whatever the header order."""
    if not content_disposition:
        return []
    message = Message()
    message["content-disposition"] = content_disposition
    params = message.get_params(header="content-disposition") or []
    names = [value for key, value in params[1:] if key.strip().lower() == "filename"]
    # The parser decodes filename* into a (charset, language, text) tuple.
    names.sort(key=lambda value: not isinstance(value, tuple))
    return [collapse_rfc2231_value(value) for value in names]


def _sanitized_filename(raw_name: object) -> str | None:
    """Strip directories and control characters from an untrusted display filename."""
    if not isinstance(raw_name, str):
        return None
    name = re.split(r"[/\\]", raw_name)[-1]
    name = "".join(char for char in name if unicodedata.category(char)[0] != "C").strip().lstrip(".").strip()
    return name[:_MAX_FILENAME_CHARS] or None


def _display_filename(content_disposition: str | None, fallback: str | None) -> str | None:
    for raw_name in (*_content_disposition_filenames(content_disposition), fallback):
        if name := _sanitized_filename(raw_name):
            return name
    return None


def _mime_type(content_type: str | None, filename: str) -> str:
    mime_type = (content_type or "").split(";", 1)[0].strip().lower()
    if _MIME_TYPE_PATTERN.fullmatch(mime_type) and mime_type != "application/octet-stream":
        return mime_type
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _attachment_kind(mime_type: str) -> Literal["audio", "file", "image", "video"]:
    top_level = mime_type.split("/", 1)[0]
    if top_level == "image":
        return "image"
    if top_level == "audio":
        return "audio"
    return "video" if top_level == "video" else "file"


def _issue_reference(issue: object, site: AtlassianSite) -> dict[str, object]:
    issue_data = _mapping(issue)
    key = issue_data.get("key")
    return {
        "key": key,
        "id": issue_data.get("id"),
        "url": f"{site.url}/browse/{key}" if site.url and isinstance(key, str) else None,
    }


def _issue_summary(issue: object, site: AtlassianSite) -> dict[str, object]:
    return {**_issue_reference(issue, site), "fields": _mapping(issue).get("fields")}


def _transition_summary(transition: object) -> dict[str, object]:
    transition_data = _mapping(transition)
    return {
        "id": transition_data.get("id"),
        "name": transition_data.get("name"),
        "to_status": _mapping(transition_data.get("to")).get("name"),
    }


def _attachment_summary(item: object) -> dict[str, object]:
    item_data = _mapping(item)
    extensions = _mapping(item_data.get("extensions"))
    version = _mapping(item_data.get("version"))
    return {
        "id": item_data.get("id"),
        "title": item_data.get("title"),
        "media_type": extensions.get("mediaType"),
        "file_size": extensions.get("fileSize"),
        "comment": extensions.get("comment") or None,
        "version": version.get("number"),
        "updated": version.get("when"),
    }


def _search_result_summary(item: object, base: object) -> dict[str, object]:
    item_data = _mapping(item)
    content = _mapping(item_data.get("content"))
    return {
        "id": content.get("id"),
        "type": content.get("type") or item_data.get("entityType"),
        "title": item_data.get("title") or content.get("title"),
        "excerpt": item_data.get("excerpt"),
        "space": _mapping(item_data.get("resultGlobalContainer")).get("title"),
        "last_modified": item_data.get("lastModified"),
        "url": _absolute_url(base, item_data.get("url")),
    }


def _storage_body(value: str) -> dict[str, str]:
    return {"representation": "storage", "value": value}


type _Operation = Callable[[str, AtlassianSite], Awaitable[dict[str, object]]]


class AtlassianToolkit(Toolkit):
    """Jira and Confluence functions for one Atlassian connection."""

    def __init__(
        self,
        *,
        provider: OAuthProvider,
        function_prefix: str,
        products: Sequence[AtlassianProduct],
        write: bool,
        site_url: str | None,
        cloud_id: str | None,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        runtime_config: Config | None,
    ) -> None:
        if credentials_manager is None:
            msg = "Atlassian tools require an explicit credentials_manager"
            raise RuntimeError(msg)
        self._provider = provider
        self._pin = AtlassianSitePin(
            site_url=normalize_site_url(site_url) if site_url and site_url.strip() else None,
            cloud_id=normalize_cloud_id(cloud_id) if cloud_id and cloud_id.strip() else None,
        )
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        super().__init__(name=provider.id)
        functions = {
            "jira_search_issues": self.jira_search_issues,
            "jira_get_issue": self.jira_get_issue,
            "jira_create_issue": self.jira_create_issue,
            "jira_update_issue": self.jira_update_issue,
            "jira_add_comment": self.jira_add_comment,
            "jira_transition_issue": self.jira_transition_issue,
            "confluence_search": self.confluence_search,
            "confluence_get_page": self.confluence_get_page,
            "confluence_list_attachments": self.confluence_list_attachments,
            "confluence_download_attachment": self.confluence_download_attachment,
            "confluence_create_page": self.confluence_create_page,
            "confluence_update_page": self.confluence_update_page,
            "confluence_add_comment": self.confluence_add_comment,
        }
        for name in atlassian_function_names(products, write=write):
            self.register(functions[name], name=f"{function_prefix}{name}")

    def _payload(self, status: str, **fields: object) -> str:
        return custom_tool_payload(self.name, status, **fields)

    def _credential_context(self) -> OAuthCredentialContext:
        return active_oauth_credential_context(
            self._provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            config=self._config,
        )

    async def _connection_required(
        self,
        context: OAuthCredentialContext,
        *,
        reason: str | None = None,
    ) -> OAuthConnectionRequired:
        # Building the link reads credential state synchronously, so keep it off the event loop.
        return await asyncio.to_thread(oauth_connection_required, context, reason=reason)

    async def _access_token(self) -> str:
        """Return a current access token for the requester, refreshing it when it is about to expire."""
        context = self._credential_context()
        if context.worker_target is None:
            # Requester-only credentials never fall back to a shared or global store.
            raise await self._connection_required(context)
        try:
            refreshed = await refresh_oauth_credentials_with_result(context)
        except OAuthCredentialUnreadableError:
            raise await self._connection_required(context, reason=OAUTH_RESET_REQUIRED_REASON) from None
        except OAuthRefreshRejectedError:
            raise await self._connection_required(context, reason=OAUTH_REFRESH_REJECTED_REASON) from None
        except OAuthProviderError as exc:
            logger.warning(
                "atlassian_oauth_refresh_failed",
                provider_id=self._provider.id,
                error_type=type(exc).__name__,
            )
            raise AtlassianError(
                code="oauth_refresh_failed",
                message="Atlassian authorization could not be refreshed. Retry this request shortly.",
            ) from None
        credentials = refreshed.credentials
        usable = await asyncio.to_thread(oauth_credentials_usable, self._provider, self._runtime_paths, credentials)
        token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
        if not usable or not isinstance(token, str) or not token:
            raise await self._connection_required(context)
        return token

    async def _call(self, product: AtlassianProduct, operation: _Operation) -> str:
        """Authenticate, resolve the pinned site, and run one operation, reducing every failure to a safe payload."""
        try:
            token = await self._access_token()
            site = select_site(
                await accessible_sites(token),
                product=product,
                product_scopes=atlassian_product_scopes(product),
                pin=self._pin,
            )
            fields = await operation(token, site)
        except OAuthConnectionRequired as exc:
            return self._payload("error", **oauth_connection_required_payload(exc))
        except AtlassianAccessRejectedError:
            exc = await self._connection_required(self._credential_context(), reason=OAUTH_ACCESS_REJECTED_REASON)
            return self._payload("error", **oauth_connection_required_payload(exc))
        except AtlassianError as exc:
            return self._error(exc)
        return self._payload("ok", product=product, site=site.summary(), **fields)

    def _error(self, exc: AtlassianError) -> str:
        return self._payload("error", code=exc.code, message=exc.message, **exc.details)

    async def jira_search_issues(
        self,
        jql: str,
        max_results: int = 20,
        fields: list[str] | None = None,
        next_page_token: str | None = None,
    ) -> str:
        """Search Jira issues with JQL.

        Args:
            jql: Jira Query Language expression, for example "project = PROJ AND statusCategory != Done ORDER BY updated DESC".
            max_results: Maximum issues to return; values above 50 are capped.
            fields: Jira field IDs to include, for example ["summary", "status", "assignee"].
            next_page_token: The next_page_token from a previous call, to fetch the next page.

        """
        try:
            body: dict[str, object] = {
                "jql": _required_text(jql, "jql"),
                "maxResults": _page_size(max_results, "max_results"),
                "fields": _string_list(fields, "fields") or list(_DEFAULT_ISSUE_FIELDS),
            }
            if token := _cursor(next_page_token, "next_page_token"):
                body["nextPageToken"] = token
        except AtlassianError as exc:
            return self._error(exc)

        async def search(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(access_token, site, "jira", "POST", "/rest/api/3/search/jql", json_body=body),
            )
            next_token = data.get("nextPageToken")
            next_token = next_token if isinstance(next_token, str) and _CURSOR_PATTERN.fullmatch(next_token) else None
            is_last = data.get("isLast")
            has_more = not is_last if isinstance(is_last, bool) else next_token is not None
            issues = data.get("issues")
            return {
                "issues": [_issue_summary(issue, site) for issue in issues] if isinstance(issues, list) else [],
                "has_more": has_more,
                "next_page_token": next_token,
            }

        return await self._call("jira", search)

    async def jira_get_issue(self, issue_key: str, fields: list[str] | None = None) -> str:
        """Get one Jira issue, including the transitions available from its current status.

        Args:
            issue_key: Issue key such as PROJ-123.
            fields: Optional Jira field IDs to include; all navigable fields are returned by default.

        """
        try:
            key = _issue_key(issue_key)
            params: dict[str, str | int] = {"expand": "transitions"}
            if requested_fields := _string_list(fields, "fields"):
                params["fields"] = ",".join(requested_fields)
        except AtlassianError as exc:
            return self._error(exc)

        async def get_issue(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(access_token, site, "jira", "GET", f"/rest/api/3/issue/{key}", params=params),
            )
            transitions = data.get("transitions")
            issue = _issue_summary(data, site)
            issue["transitions"] = (
                [_transition_summary(item) for item in transitions] if isinstance(transitions, list) else []
            )
            return {"issue": issue}

        return await self._call("jira", get_issue)

    async def jira_create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str | None = None,
        fields: dict[str, object] | None = None,
    ) -> str:
        """Create a Jira issue.

        Args:
            project_key: Project key such as PROJ.
            summary: Issue summary line.
            issue_type: Issue type name, such as Task, Bug, or Story.
            description: Optional plain-text description.
            fields: Optional additional Jira fields keyed by field ID, for example {"labels": ["docs"]}.

        """
        try:
            if not isinstance(project_key, str) or not _PROJECT_KEY_PATTERN.fullmatch(project_key):
                msg = "project_key must be a Jira project key such as PROJ."
                raise _InvalidArgumentError(msg)
            issue_fields: dict[str, object] = {
                **_extra_fields(fields),
                "project": {"key": project_key.upper()},
                "summary": _required_text(summary, "summary"),
                "issuetype": {"name": _required_text(issue_type, "issue_type")},
            }
            if description is not None:
                issue_fields["description"] = _adf_document(_required_text(description, "description"))
        except AtlassianError as exc:
            return self._error(exc)

        async def create(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = await request_json(
                access_token,
                site,
                "jira",
                "POST",
                "/rest/api/3/issue",
                json_body={"fields": issue_fields},
            )
            return {"issue": _issue_reference(data, site)}

        return await self._call("jira", create)

    async def jira_update_issue(
        self,
        issue_key: str,
        summary: str | None = None,
        description: str | None = None,
        fields: dict[str, object] | None = None,
    ) -> str:
        """Update fields of a Jira issue.

        Args:
            issue_key: Issue key such as PROJ-123.
            summary: Optional new summary line.
            description: Optional new plain-text description, replacing the current one.
            fields: Optional additional Jira fields keyed by field ID, for example {"labels": ["docs"]}.

        """
        try:
            key = _issue_key(issue_key)
            issue_fields = _extra_fields(fields)
            if summary is not None:
                issue_fields["summary"] = _required_text(summary, "summary")
            if description is not None:
                issue_fields["description"] = _adf_document(_required_text(description, "description"))
            if not issue_fields:
                msg = "Provide summary, description, or fields to update."
                raise _InvalidArgumentError(msg)
        except AtlassianError as exc:
            return self._error(exc)

        async def update(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await request_json(
                access_token,
                site,
                "jira",
                "PUT",
                f"/rest/api/3/issue/{key}",
                json_body={"fields": issue_fields},
            )
            return {"issue": _issue_reference({"key": key}, site), "updated_fields": sorted(issue_fields)}

        return await self._call("jira", update)

    async def jira_add_comment(self, issue_key: str, comment: str) -> str:
        """Add a plain-text comment to a Jira issue.

        Args:
            issue_key: Issue key such as PROJ-123.
            comment: Comment text; blank lines separate paragraphs.

        """
        try:
            key = _issue_key(issue_key)
            body = {"body": _adf_document(_required_text(comment, "comment"))}
        except AtlassianError as exc:
            return self._error(exc)

        async def add_comment(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(
                    access_token,
                    site,
                    "jira",
                    "POST",
                    f"/rest/api/3/issue/{key}/comment",
                    json_body=body,
                ),
            )
            return {"comment": {"id": data.get("id"), "issue_key": key}}

        return await self._call("jira", add_comment)

    async def jira_transition_issue(self, issue_key: str, transition: str) -> str:
        """Move a Jira issue through its workflow.

        Args:
            issue_key: Issue key such as PROJ-123.
            transition: Transition ID or name from jira_get_issue, such as "31" or "In Progress".

        """
        try:
            key = _issue_key(issue_key)
            requested = _required_text(transition, "transition").strip()
        except AtlassianError as exc:
            return self._error(exc)

        async def transition_issue(access_token: str, site: AtlassianSite) -> dict[str, object]:
            path = f"/rest/api/3/issue/{key}/transitions"
            data = _mapping(await request_json(access_token, site, "jira", "GET", path))
            available = [_transition_summary(item) for item in data.get("transitions") or [] if isinstance(item, dict)]
            wanted = requested.casefold()
            match = next(
                (
                    item
                    for item in available
                    if item["id"] == requested
                    or (isinstance(item["name"], str) and item["name"].casefold() == wanted)
                    or (isinstance(item["to_status"], str) and item["to_status"].casefold() == wanted)
                ),
                None,
            )
            if match is None:
                raise AtlassianError(
                    code="transition_not_available",
                    message="That transition is not available for this issue from its current status.",
                    available_transitions=available,
                )
            await request_json(access_token, site, "jira", "POST", path, json_body={"transition": {"id": match["id"]}})
            return {"issue": _issue_reference({"key": key}, site), "transition": match}

        return await self._call("jira", transition_issue)

    async def confluence_search(self, cql: str, limit: int = 10, cursor: str | None = None) -> str:
        """Search Confluence with CQL.

        Args:
            cql: Confluence Query Language expression, for example 'type = page AND text ~ "release plan"'.
            limit: Maximum results to return; values above 50 are capped.
            cursor: The next_cursor from a previous call, to fetch the next page.

        """
        try:
            params: dict[str, str | int] = {
                "cql": _required_text(cql, "cql"),
                "limit": _page_size(limit, "limit"),
                "excerpt": "indexed",
            }
            if next_cursor := _cursor(cursor, "cursor"):
                params["cursor"] = next_cursor
        except AtlassianError as exc:
            return self._error(exc)

        async def search(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(access_token, site, "confluence", "GET", "/wiki/rest/api/search", params=params),
            )
            links = _mapping(data.get("_links"))
            results = data.get("results")
            return {
                "results": [_search_result_summary(item, links.get("base")) for item in results]
                if isinstance(results, list)
                else [],
                **_page_fields(*_next_cursor(links)),
            }

        return await self._call("confluence", search)

    async def confluence_get_page(self, page_id: str) -> str:
        """Get a Confluence page with its storage-format body and current version number.

        Args:
            page_id: Numeric Confluence page ID.

        """
        try:
            page = _page_id(page_id)
        except AtlassianError as exc:
            return self._error(exc)
        params = {"cql": f"id = {page} AND type = page", "limit": 1, "expand": "body.storage,version,space"}

        async def get_page(access_token: str, site: AtlassianSite) -> dict[str, object]:
            path = "/wiki/rest/api/content/search"
            data = _mapping(await request_json(access_token, site, "confluence", "GET", path, params=params))
            results = data.get("results")
            if not isinstance(results, list) or not results:
                raise AtlassianError(
                    code="page_not_found",
                    message="The page does not exist or the connected account cannot view it.",
                )
            item = _mapping(results[0])
            space = _mapping(item.get("space"))
            links = _mapping(item.get("_links"))
            return {
                "page": {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "space_key": space.get("key"),
                    "space_name": space.get("name"),
                    "version": _mapping(item.get("version")).get("number"),
                    "body_storage": _mapping(_mapping(item.get("body")).get("storage")).get("value"),
                    "url": _absolute_url(_mapping(data.get("_links")).get("base"), links.get("webui")),
                },
            }

        return await self._call("confluence", get_page)

    async def confluence_list_attachments(self, page_id: str, limit: int = 25, cursor: str | None = None) -> str:
        """List files attached to a Confluence page.

        Args:
            page_id: Numeric Confluence page ID.
            limit: Maximum attachments to return; values above 50 are capped.
            cursor: The next_cursor from a previous call, to fetch the next page.

        """
        try:
            page = _page_id(page_id)
            params: dict[str, str | int] = {
                "cql": f"type = attachment AND container = {page}",
                "limit": _page_size(limit, "limit"),
                "expand": "version",
            }
            if next_cursor := _cursor(cursor, "cursor"):
                params["cursor"] = next_cursor
        except AtlassianError as exc:
            return self._error(exc)

        async def list_attachments(access_token: str, site: AtlassianSite) -> dict[str, object]:
            path = "/wiki/rest/api/content/search"
            data = _mapping(await request_json(access_token, site, "confluence", "GET", path, params=params))
            results = data.get("results")
            return {
                "page_id": page,
                "attachments": [_attachment_summary(item) for item in results] if isinstance(results, list) else [],
                **_page_fields(*_next_cursor(data.get("_links"))),
            }

        return await self._call("confluence", list_attachments)

    async def confluence_download_attachment(
        self,
        page_id: str,
        attachment_id: str,
        filename: str | None = None,
    ) -> str:
        """Download one Confluence page attachment into this conversation.

        Returns a context attachment ID (att_...) that is usable in this turn only.
        Open it with get_attachment(attachment_id), or save it to the workspace with
        get_attachment(attachment_id, mindroom_output_path=...); in a later turn, download it again.

        Args:
            page_id: Numeric Confluence page ID that owns the attachment.
            attachment_id: Confluence attachment ID from confluence_list_attachments, such as att123456.
            filename: Optional title from confluence_list_attachments, used only when the download names no file.

        """
        try:
            page = _page_id(page_id)
            confluence_attachment_id = _confluence_attachment_id(attachment_id)
        except AtlassianError as exc:
            return self._error(exc)
        context = get_tool_runtime_context()
        if context is None or context.storage_path is None:
            return self._payload(
                "error",
                code="attachment_context_unavailable",
                message="Attachment downloads require a conversation with MindRoom attachment storage.",
            )
        storage_path = context.storage_path
        max_bytes = inline_attachment_byte_limit(self._runtime_paths)

        async def download_attachment(access_token: str, site: AtlassianSite) -> dict[str, object]:
            path = f"/wiki/rest/api/content/{page}/child/attachment/{confluence_attachment_id}/download"
            downloaded = await download(access_token, site, "confluence", path, max_bytes=max_bytes)
            display_name = _display_filename(downloaded.content_disposition, filename) or confluence_attachment_id
            mime_type = _mime_type(downloaded.content_type, display_name)
            # Like MindRoom's own media registration, a cancelled call still finishes storing,
            # so no write or registration is left running behind it.
            record = await run_blocking_until_complete(
                partial(
                    register_bytes_attachment,
                    storage_path,
                    downloaded.content,
                    kind=_attachment_kind(mime_type),
                    mime_type=mime_type,
                    attachment_id=f"att_{uuid4().hex[:16]}",
                    filename=display_name,
                    room_id=context.room_id,
                    thread_id=context.resolved_thread_id,
                    sender=context.requester_id,
                ),
            )
            if record is None:
                raise AtlassianError(code="attachment_store_failed", message="The attachment could not be stored.")
            append_tool_runtime_attachment_id(record.attachment_id)
            return {
                "page_id": page,
                "confluence_attachment_id": confluence_attachment_id,
                "attachment_id": record.attachment_id,
                "attachment": {
                    "filename": record.filename,
                    "mime_type": record.mime_type,
                    "size_bytes": record.size_bytes,
                    "sha256": record.content_sha256,
                },
                "usage": _ATTACHMENT_USAGE,
            }

        return await self._call("confluence", download_attachment)

    async def confluence_create_page(
        self,
        space_key: str,
        title: str,
        body_storage: str,
        parent_page_id: str | None = None,
    ) -> str:
        """Create and publish a Confluence page.

        Args:
            space_key: Key of the space to create the page in, such as DOCS.
            title: Page title.
            body_storage: Page body in Confluence storage format (XHTML), for example "<p>Hello</p>".
            parent_page_id: Optional numeric ID of the parent page.

        """
        try:
            if not isinstance(space_key, str) or not _SPACE_KEY_PATTERN.fullmatch(space_key):
                msg = "space_key must be a Confluence space key such as DOCS."
                raise _InvalidArgumentError(msg)
            body: dict[str, object] = {
                "status": "current",
                "title": _required_text(title, "title"),
                "body": _storage_body(_required_text(body_storage, "body_storage")),
            }
            if parent_page_id is not None:
                body["parentId"] = _page_id(parent_page_id, field_name="parent_page_id")
        except AtlassianError as exc:
            return self._error(exc)

        async def create(access_token: str, site: AtlassianSite) -> dict[str, object]:
            spaces = _mapping(
                await request_json(
                    access_token,
                    site,
                    "confluence",
                    "GET",
                    "/wiki/api/v2/spaces",
                    params={"keys": space_key, "limit": 1},
                ),
            ).get("results")
            space_id = next(
                (
                    space.get("id")
                    for space in spaces or []
                    if isinstance(space, dict)
                    and str(space.get("key")).casefold() == space_key.casefold()
                    and space.get("id")
                ),
                None,
            )
            if space_id is None:
                raise AtlassianError(
                    code="space_not_found",
                    message="The space does not exist or the connected account cannot view it.",
                )
            data = await request_json(
                access_token,
                site,
                "confluence",
                "POST",
                "/wiki/api/v2/pages",
                json_body={**body, "spaceId": str(space_id)},
            )
            return {"page": self._written_page(data)}

        return await self._call("confluence", create)

    async def confluence_update_page(
        self,
        page_id: str,
        title: str,
        body_storage: str,
        version_number: int,
        version_message: str | None = None,
    ) -> str:
        """Replace the title and body of a published Confluence page.

        Args:
            page_id: Numeric Confluence page ID.
            title: Page title, which may be unchanged.
            body_storage: Complete new page body in Confluence storage format (XHTML).
            version_number: The page's current version number from confluence_get_page plus one.
            version_message: Optional change note stored with the new version.

        """
        try:
            page = _page_id(page_id)
            if isinstance(version_number, bool) or not isinstance(version_number, int) or version_number < 2:
                msg = "version_number must be the page's current version number plus one."
                raise _InvalidArgumentError(msg)
            version: dict[str, object] = {"number": version_number}
            if version_message is not None:
                version["message"] = _required_text(version_message, "version_message")
            body = {
                "id": page,
                "status": "current",
                "title": _required_text(title, "title"),
                "body": _storage_body(_required_text(body_storage, "body_storage")),
                "version": version,
            }
        except AtlassianError as exc:
            return self._error(exc)

        async def update(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = await request_json(
                access_token,
                site,
                "confluence",
                "PUT",
                f"/wiki/api/v2/pages/{page}",
                json_body=body,
            )
            return {"page": self._written_page(data)}

        return await self._call("confluence", update)

    async def confluence_add_comment(self, page_id: str, body_storage: str) -> str:
        """Add a footer comment to a Confluence page.

        Args:
            page_id: Numeric Confluence page ID.
            body_storage: Comment body in Confluence storage format (XHTML), for example "<p>Looks good.</p>".

        """
        try:
            page = _page_id(page_id)
            body = {"pageId": page, "body": _storage_body(_required_text(body_storage, "body_storage"))}
        except AtlassianError as exc:
            return self._error(exc)

        async def add_comment(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(
                    access_token,
                    site,
                    "confluence",
                    "POST",
                    "/wiki/api/v2/footer-comments",
                    json_body=body,
                ),
            )
            return {"comment": {"id": data.get("id"), "page_id": page}}

        return await self._call("confluence", add_comment)

    @staticmethod
    def _written_page(data: object) -> dict[str, object]:
        page = _mapping(data)
        links = _mapping(page.get("_links"))
        return {
            "id": page.get("id"),
            "title": page.get("title"),
            "version": _mapping(page.get("version")).get("number"),
            "url": _absolute_url(links.get("base"), links.get("webui")),
        }


class AtlassianTools(AtlassianToolkit):
    """The default Atlassian connection, optionally pinned to one site per agent."""

    def __init__(
        self,
        site_url: str | None = None,
        cloud_id: str | None = None,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
    ) -> None:
        super().__init__(
            provider=atlassian_oauth_provider(),
            function_prefix="",
            products=ATLASSIAN_PRODUCTS,
            write=True,
            site_url=site_url,
            cloud_id=cloud_id,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
        )
