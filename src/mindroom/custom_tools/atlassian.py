"""Jira and Confluence Cloud tools that act as the requester through Atlassian OAuth."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from email.message import Message
from email.utils import collapse_rfc2231_value
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from mindroom.attachments.storage import register_bytes_attachment
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.constants import RETAINED_MEDIA_MAX_BYTES
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.atlassian_client import (
    AtlassianAccessRejectedError,
    AtlassianError,
    AtlassianSite,
    AtlassianSitePin,
    accessible_sites,
    download,
    request_json,
    select_site,
)
from mindroom.custom_tools.atlassian_spaces import (
    CONFLUENCE_SPACE_KEY_PATTERN,
    JIRA_PROJECT_KEY_PATTERN,
    AtlassianSpaces,
    UnconfinableQueryError,
    space_limit_sentence,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.llm.media import canonical_mime_type, named_mime_type
from mindroom.logging_config import get_logger
from mindroom.oauth.atlassian import (
    ATLASSIAN_PRODUCTS,
    AtlassianProduct,
    atlassian_function_names,
    atlassian_oauth_provider,
    atlassian_product_scopes,
    normalize_cloud_id,
    normalize_site_url,
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
from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.runtime_context import append_tool_runtime_attachment_id, get_tool_runtime_context
from mindroom.tool_system.sandbox_proxy import INLINE_ATTACHMENT_BYTES_ENV, inline_attachment_byte_limit

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
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9._~+/=%-]{1,2048}")
_MIME_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}")
_MAX_FILENAME_CHARS = 200
_DEFAULT_ISSUE_FIELDS = ("summary", "status", "assignee", "reporter", "priority", "issuetype", "updated")
# Every field of an issue can be very large, so a single read adds only the description by default.
_DEFAULT_ISSUE_READ_FIELDS = (*_DEFAULT_ISSUE_FIELDS, "description")
# Field selectors whose results already include each issue's space.
_PROJECT_FIELD_SELECTORS = frozenset({"project", "*all", "*navigable"})
_ATTACHMENT_USAGE = (
    'Usable in this turn only. Open it with attachment(action="get", attachment_id=...), or save it with '
    'attachment(action="get", attachment_id=..., mindroom_output_path=...); download it again in a later turn.'
)


class _InvalidArgumentError(AtlassianError):
    """A model-supplied argument was rejected before any authentication or network access."""

    def __init__(self, message: str) -> None:
        super().__init__(code="invalid_argument", message=message)


class _SpaceNotAllowedError(AtlassianError):
    """The item is outside the spaces this agent may use, so nothing of it is returned or changed."""

    def __init__(self, product: AtlassianProduct, allowed: Sequence[str]) -> None:
        super().__init__(
            code="space_not_allowed",
            message=space_limit_sentence(product, allowed),
            allowed_spaces=list(allowed),
        )


def _page_not_found() -> AtlassianError:
    return AtlassianError(
        code="page_not_found",
        message="The page was not found. It may not exist, may not be a page, may not be viewable by "
        "the connected account, or may not be indexed by search yet if it was created moments ago.",
    )


def _page_id(value: str | None, *, field_name: str = "page_id") -> str:
    if isinstance(value, str) and _PAGE_ID_PATTERN.fullmatch(value):
        return value
    msg = f"{field_name} must be a numeric Confluence page ID, not a URL, title, or path."
    raise _InvalidArgumentError(msg)


def _confluence_attachment_id(value: str | None) -> str:
    match = _ATTACHMENT_ID_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        msg = (
            "attachment_id must be a Confluence attachment ID from "
            'confluence_read(action="list_attachments"), such as att123456.'
        )
        raise _InvalidArgumentError(msg)
    return f"att{match.group(1)}"


def _issue_key(value: str | None) -> str:
    if isinstance(value, str) and _ISSUE_KEY_PATTERN.fullmatch(value):
        return value.upper()
    msg = "issue_key must be a Jira issue key such as PROJ-123, or a numeric issue ID."
    raise _InvalidArgumentError(msg)


def _required_text(value: str | None, field_name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    msg = f"{field_name} must be a non-empty string."
    raise _InvalidArgumentError(msg)


def _page_size(value: int, field_name: str = "limit") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{field_name} must be an integer."
        raise _InvalidArgumentError(msg)
    return max(1, min(value, _MAX_PAGE_SIZE))


def _cursor(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if _CURSOR_PATTERN.fullmatch(value):
        return value
    msg = f"{field_name} must be the unchanged value returned by a previous call."
    raise _InvalidArgumentError(msg)


def _string_list(value: list[str] | None, field_name: str) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value]
    if not items or not all(items):
        msg = f"{field_name} must be a non-empty list of strings."
        raise _InvalidArgumentError(msg)
    return items


def _extra_fields(value: dict | None) -> dict[str, object]:
    if value is None:
        return {}
    return dict(value)


def _adf_document(text: str) -> dict[str, object]:
    """Convert plain text into an Atlassian Document Format document, one paragraph per blank-line block."""
    paragraphs: list[dict[str, object]] = []
    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
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


def _page_fields(has_more: bool, next_cursor: str | None, *, cursor_field: str = "next_cursor") -> dict[str, object]:
    fields: dict[str, object] = {"has_more": has_more, cursor_field: next_cursor}
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
    mime_type = canonical_mime_type(content_type) or ""
    if _MIME_TYPE_PATTERN.fullmatch(mime_type) and mime_type != "application/octet-stream":
        return mime_type
    return named_mime_type(filename) or "application/octet-stream"


def _attachment_kind(mime_type: str) -> Literal["audio", "file", "image", "video"]:
    top_level = mime_type.split("/", 1)[0]
    if top_level == "image":
        return "image"
    if top_level == "audio":
        return "audio"
    return "video" if top_level == "video" else "file"


def _issue_space_key(issue: object) -> object:
    return _mapping(_mapping(_mapping(issue).get("fields")).get("project")).get("key")


def _search_result_space_key(item: object) -> object:
    """Return the space of a search result, which is the content's space or, for a space result, the space itself."""
    item_data = _mapping(item)
    space = _mapping(_mapping(item_data.get("content")).get("space")) or _mapping(item_data.get("space"))
    return space.get("key")


def _issue_reference(issue: object, site: AtlassianSite) -> dict[str, object]:
    issue_data = _mapping(issue)
    key = issue_data.get("key")
    return {
        "key": key,
        "id": issue_data.get("id"),
        "url": f"{site.url}/browse/{key}" if site.url and isinstance(key, str) and "-" in key else None,
    }


def _issue_summary(issue: object, site: AtlassianSite, *, omit_space: bool = False) -> dict[str, object]:
    """Return an issue's reference and fields, without the space field when only the space check requested it."""
    fields = _mapping(issue).get("fields")
    if omit_space and isinstance(fields, dict):
        fields = {name: value for name, value in fields.items() if name != "project"}
    return {**_issue_reference(issue, site), "fields": fields}


def _transition_summary(transition: object) -> dict[str, object]:
    transition_data = _mapping(transition)
    return {
        "id": transition_data.get("id"),
        "name": transition_data.get("name"),
        "to_status": _mapping(transition_data.get("to")).get("name"),
    }


def _casefolded(value: object) -> str | None:
    return value.casefold() if isinstance(value, str) else None


def _resolve_transition(requested: str, available: list[dict[str, object]]) -> dict[str, object]:
    """Pick a transition by exact ID, else by name, else by target status, refusing any ambiguous match."""
    by_id = next((item for item in available if item["id"] == requested), None)
    if by_id is not None:
        return by_id
    wanted = requested.casefold()
    for field, label in (("name", "name"), ("to_status", "target status")):
        matches = [item for item in available if _casefolded(item[field]) == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise AtlassianError(
                code="transition_ambiguous",
                message=f"Several transitions have that {label}, so nothing was changed. "
                "Retry with the ID of one candidate transition.",
                candidate_transitions=matches,
            )
    raise AtlassianError(
        code="transition_not_available",
        message="That transition is not available for this issue from its current status.",
        available_transitions=available,
    )


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


def _person(user: object) -> dict[str, str | None] | None:
    """Return a Confluence user's display name and account ID, or None for a missing or anonymous user."""
    user_data = _mapping(user)
    # displayName is null when the profile hides it, while publicName always has a value.
    name = user_data.get("displayName") or user_data.get("publicName")
    account_id = user_data.get("accountId")
    person = {
        "display_name": name if isinstance(name, str) and name else None,
        "account_id": account_id if isinstance(account_id, str) and account_id else None,
    }
    return person if user_data.get("type") != "anonymous" and any(person.values()) else None


def _content_people(content: Mapping[str, Any]) -> dict[str, object]:
    """Name who created, owns, and last edited content read with its history, history.ownedBy, and version."""
    history = _mapping(content.get("history"))
    return {
        "created_by": _person(history.get("createdBy")),
        "owned_by": _person(history.get("ownedBy")),
        "last_modified_by": _person(_mapping(content.get("version")).get("by")),
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
        "created": _mapping(content.get("history")).get("createdDate"),
        "last_modified": item_data.get("lastModified"),
        **_content_people(content),
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
        jira_spaces: list[str] | None = None,
        confluence_spaces: list[str] | None = None,
    ) -> None:
        if credentials_manager is None:
            msg = "Atlassian tools require an explicit credentials_manager"
            raise RuntimeError(msg)
        self._provider = provider
        self._pin = AtlassianSitePin(
            site_url=normalize_site_url(site_url) if site_url and site_url.strip() else None,
            cloud_id=normalize_cloud_id(cloud_id) if cloud_id and cloud_id.strip() else None,
        )
        self._spaces = AtlassianSpaces.from_config(jira_spaces=jira_spaces, confluence_spaces=confluence_spaces)
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        # An empty space list leaves the agent no space of that product, so none of its functions are offered.
        enabled_products = tuple(product for product in products if self._spaces.keys(product) != ())
        instructions = self._spaces.instructions(enabled_products, function_prefix)
        # On a user or user_agent agent each requester still connects their own account.
        if (
            not provider.requester_scoped_credentials
            and enabled_products
            and worker_target is not None
            and worker_target.worker_scope not in {"user", "user_agent"}
        ):
            subjects = " and ".join(f"`{function_prefix}{product}_*`" for product in enabled_products)
            shared = (
                f"The {subjects} functions act as one Atlassian account shared by every requester, "
                "so currentUser() in JQL or CQL is that account, not the requester."
            )
            instructions = f"{instructions} {shared}" if instructions else shared
        super().__init__(name=provider.id, instructions=instructions)
        functions = {
            "jira_read": self.jira_read,
            "jira_write": self.jira_write,
            "confluence_read": self.confluence_read,
            "confluence_write": self.confluence_write,
        }
        self.functions = {
            f"{function_prefix}{name}": functions[name]
            for name in atlassian_function_names(enabled_products, write=write)
        }

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
            # Without a worker target there is no agent or requester to bind, so refuse rather than read an
            # installation-level store.
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

    def _require_space(self, product: AtlassianProduct, key: object) -> None:
        """Refuse an item whose space this agent may not use."""
        allowed = self._spaces.keys(product)
        if allowed is not None and not self._spaces.allows(product, key):
            raise _SpaceNotAllowedError(product, allowed)

    def _restricted_query(self, product: AtlassianProduct, query: str, field_name: str) -> str:
        """Confine a search query to the allowed spaces, refusing one whose structure could escape them."""
        try:
            return self._spaces.restrict_query(product, query)
        except UnconfinableQueryError as exc:
            msg = (
                f"{field_name} has {exc}. With this agent's {product.capitalize()} spaces limited, a query needs "
                "matched parentheses and closed quotes, and no backslash outside quotes."
            )
            raise _InvalidArgumentError(msg) from None

    def _adds_space_field(self, fields: list[str]) -> bool:
        """Return whether issue reads must also request each issue's space, so results can be checked."""
        return self._spaces.jira is not None and not _PROJECT_FIELD_SELECTORS & set(fields)

    async def _require_issue_space(self, access_token: str, site: AtlassianSite, key: str) -> None:
        """Read an issue's current space before changing it, when this agent's Jira spaces are limited."""
        if self._spaces.jira is None:
            return
        path = f"/rest/api/3/issue/{key}"
        issue = await request_json(access_token, site, "jira", "GET", path, params={"fields": "project"})
        self._require_space("jira", _issue_space_key(issue))

    async def _require_page_space(self, access_token: str, site: AtlassianSite, page: str) -> None:
        """Read a page's space before using it, when this agent's Confluence spaces are limited."""
        if self._spaces.confluence is None:
            return
        params = {"cql": f"id = {page}", "limit": 1, "expand": "space"}
        data = await request_json(
            access_token,
            site,
            "confluence",
            "GET",
            "/wiki/rest/api/content/search",
            params=params,
        )
        results = _mapping(data).get("results")
        if not isinstance(results, list) or not results:
            raise _page_not_found()
        self._require_space("confluence", _mapping(_mapping(results[0]).get("space")).get("key"))

    async def _dispatch(
        self,
        product: AtlassianProduct,
        action: str,
        builders: Mapping[str, Callable[[], _Operation]],
    ) -> str:
        builder = builders.get(action)
        if builder is None:
            expected = ", ".join(repr(name) for name in builders)
            return self._error(_InvalidArgumentError(f"Unsupported action {action!r}; expected one of: {expected}."))
        try:
            operation = builder()
        except AtlassianError as exc:
            return self._error(exc)
        return await self._call(product, operation)

    def _jira_search_op(
        self,
        jql: str | None,
        fields: list[str] | None,
        max_results: int,
        next_page_token: str | None,
    ) -> _Operation:
        requested = _string_list(fields, "fields") or list(_DEFAULT_ISSUE_FIELDS)
        adds_space_field = self._adds_space_field(requested)
        body: dict[str, object] = {
            "jql": self._restricted_query("jira", _required_text(jql, "jql"), "jql"),
            "maxResults": _page_size(max_results, "max_results"),
            "fields": [*requested, "project"] if adds_space_field else requested,
        }
        if token := _cursor(next_page_token, "next_page_token"):
            body["nextPageToken"] = token

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
                "issues": [
                    _issue_summary(issue, site, omit_space=adds_space_field)
                    for issue in issues
                    if self._spaces.allows("jira", _issue_space_key(issue))
                ]
                if isinstance(issues, list)
                else [],
                **_page_fields(has_more, next_token, cursor_field="next_page_token"),
            }

        return search

    def _jira_get_op(self, issue_key: str | None, fields: list[str] | None) -> _Operation:
        key = _issue_key(issue_key)
        requested = _string_list(fields, "fields") or list(_DEFAULT_ISSUE_READ_FIELDS)
        adds_space_field = self._adds_space_field(requested)
        params: dict[str, str | int] = {
            "expand": "transitions",
            "fields": ",".join([*requested, "project"] if adds_space_field else requested),
        }

        async def get_issue(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(access_token, site, "jira", "GET", f"/rest/api/3/issue/{key}", params=params),
            )
            self._require_space("jira", _issue_space_key(data))
            transitions = data.get("transitions")
            issue = _issue_summary(data, site, omit_space=adds_space_field)
            issue["transitions"] = (
                [_transition_summary(item) for item in transitions] if isinstance(transitions, list) else []
            )
            return {"issue": issue}

        return get_issue

    @tool(offer_background_after=True, offer_output_path=True)
    async def jira_read(
        self,
        action: Literal["search", "get"],
        jql: str | None = None,
        issue_key: str | None = None,
        fields: list[str] | None = None,
        max_results: int = 20,
        next_page_token: str | None = None,
    ) -> str:
        """Search Jira issues or get one issue with its available transitions.

        - `search`: `jql` (optional: `max_results`, `fields`, `next_page_token`). Use `assignee = currentUser()` or
          `reporter = currentUser()` for the requester's own issues.
        - `get`: `issue_key` (optional: `fields`; defaults to summary, status, assignee, reporter, priority,
          issuetype, updated, and description).

        Args:
            action: Read operation (`search` or `get`).
            jql: Jira Query Language expression, for example "project = PROJ AND statusCategory != Done ORDER BY updated DESC".
            issue_key: Issue key such as PROJ-123, or a numeric issue ID.
            fields: Jira field IDs to include, such as ["summary", "labels"], or ["*all"] for every field.
            max_results: Maximum issues to return for `search`; values above 50 are capped.
            next_page_token: The next_page_token from a previous `search` call, to fetch the next page.

        """
        return await self._dispatch(
            "jira",
            action,
            {
                "search": lambda: self._jira_search_op(jql, fields, max_results, next_page_token),
                "get": lambda: self._jira_get_op(issue_key, fields),
            },
        )

    def _jira_create_op(
        self,
        project_key: str | None,
        summary: str | None,
        issue_type: str | None,
        description: str | None,
        fields: dict | None,
    ) -> _Operation:
        if not isinstance(project_key, str) or not JIRA_PROJECT_KEY_PATTERN.fullmatch(project_key):
            msg = "project_key must be a Jira project key such as PROJ."
            raise _InvalidArgumentError(msg)
        self._require_space("jira", project_key)
        issue_fields: dict[str, object] = {
            **_extra_fields(fields),
            "project": {"key": project_key.upper()},
            "summary": _required_text(summary, "summary"),
            "issuetype": {"name": _required_text("Task" if issue_type is None else issue_type, "issue_type")},
        }
        if description is not None:
            issue_fields["description"] = _adf_document(_required_text(description, "description"))

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

        return create

    def _jira_update_op(
        self,
        issue_key: str | None,
        summary: str | None,
        description: str | None,
        fields: dict | None,
    ) -> _Operation:
        key = _issue_key(issue_key)
        issue_fields = _extra_fields(fields)
        if summary is not None:
            issue_fields["summary"] = _required_text(summary, "summary")
        if description is not None:
            issue_fields["description"] = _adf_document(_required_text(description, "description"))
        if not issue_fields:
            msg = "Provide summary, description, or fields to update."
            raise _InvalidArgumentError(msg)

        async def update(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await self._require_issue_space(access_token, site, key)
            await request_json(
                access_token,
                site,
                "jira",
                "PUT",
                f"/rest/api/3/issue/{key}",
                json_body={"fields": issue_fields},
            )
            return {"issue": _issue_reference({"key": key}, site), "updated_fields": sorted(issue_fields)}

        return update

    def _jira_comment_op(self, issue_key: str | None, comment: str | None) -> _Operation:
        key = _issue_key(issue_key)
        body = {"body": _adf_document(_required_text(comment, "comment"))}

        async def add_comment(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await self._require_issue_space(access_token, site, key)
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

        return add_comment

    def _jira_transition_op(self, issue_key: str | None, transition: str | None) -> _Operation:
        key = _issue_key(issue_key)
        requested = _required_text(transition, "transition").strip()

        async def transition_issue(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await self._require_issue_space(access_token, site, key)
            path = f"/rest/api/3/issue/{key}/transitions"
            data = _mapping(await request_json(access_token, site, "jira", "GET", path))
            available = [_transition_summary(item) for item in data.get("transitions") or [] if isinstance(item, dict)]
            match = _resolve_transition(requested, available)
            await request_json(access_token, site, "jira", "POST", path, json_body={"transition": {"id": match["id"]}})
            return {"issue": _issue_reference({"key": key}, site), "transition": match}

        return transition_issue

    @tool(offer_background_after=False, offer_output_path=False)
    async def jira_write(
        self,
        action: Literal["create", "update", "comment", "transition"],
        project_key: str | None = None,
        issue_key: str | None = None,
        summary: str | None = None,
        issue_type: str | None = None,
        description: str | None = None,
        fields: dict | None = None,
        comment: str | None = None,
        transition: str | None = None,
    ) -> str:
        """Create, update, comment on, or transition a Jira issue.

        - `create`: `project_key`, `summary` (optional: `issue_type` defaults to "Task", `description`, `fields`).
        - `update`: `issue_key` and at least one of `summary`, `description`, or `fields`.
        - `comment`: `issue_key`, `comment`.
        - `transition`: `issue_key`, `transition`.

        Args:
            action: Write operation (`create`, `update`, `comment`, or `transition`).
            project_key: Project key such as PROJ.
            issue_key: Issue key such as PROJ-123, or a numeric issue ID.
            summary: Issue summary line.
            issue_type: Issue type name such as Task, Bug, or Story (defaults to "Task" for `create`).
            description: Plain-text description (replaces the current description on `update`).
            fields: Additional Jira fields keyed by field ID, for example {"labels": ["docs"]}.
            comment: Plain-text comment; blank lines separate paragraphs.
            transition: Transition ID from jira_read(action="get") such as "31", or a transition or target status name.

        """
        return await self._dispatch(
            "jira",
            action,
            {
                "create": lambda: self._jira_create_op(project_key, summary, issue_type, description, fields),
                "update": lambda: self._jira_update_op(issue_key, summary, description, fields),
                "comment": lambda: self._jira_comment_op(issue_key, comment),
                "transition": lambda: self._jira_transition_op(issue_key, transition),
            },
        )

    def _confluence_search_op(self, cql: str | None, limit: int | None, cursor: str | None) -> _Operation:
        expand = "content.history,content.history.ownedBy,content.version"
        params: dict[str, str | int] = {
            "cql": self._restricted_query("confluence", _required_text(cql, "cql"), "cql"),
            "limit": _page_size(10 if limit is None else limit),
            "excerpt": "indexed",
            "expand": f"{expand},content.space" if self._spaces.confluence is not None else expand,
        }
        if next_cursor := _cursor(cursor, "cursor"):
            # Search's own next links pair the cursor with next=true.
            params.update(cursor=next_cursor, next="true")

        async def search(access_token: str, site: AtlassianSite) -> dict[str, object]:
            data = _mapping(
                await request_json(access_token, site, "confluence", "GET", "/wiki/rest/api/search", params=params),
            )
            links = _mapping(data.get("_links"))
            results = data.get("results")
            return {
                "results": [
                    _search_result_summary(item, links.get("base"))
                    for item in results
                    if self._spaces.allows("confluence", _search_result_space_key(item))
                ]
                if isinstance(results, list)
                else [],
                **_page_fields(*_next_cursor(links)),
            }

        return search

    def _confluence_get_op(self, page_id: str | None) -> _Operation:
        page = _page_id(page_id)
        # Any content type is matched, so an ID that is not a page is reported as such instead of as missing.
        params = {"cql": f"id = {page}", "limit": 1, "expand": "body.storage,version,space,history,history.ownedBy"}

        async def get_page(access_token: str, site: AtlassianSite) -> dict[str, object]:
            path = "/wiki/rest/api/content/search"
            data = _mapping(await request_json(access_token, site, "confluence", "GET", path, params=params))
            results = data.get("results")
            if not isinstance(results, list) or not results:
                raise _page_not_found()
            item = _mapping(results[0])
            space = _mapping(item.get("space"))
            self._require_space("confluence", space.get("key"))
            content_type = item.get("type")
            if content_type != "page":
                described = (
                    "a blog post" if content_type == "blogpost" else f"Confluence content of type {content_type}"
                )
                raise AtlassianError(
                    code="not_a_page",
                    message=(
                        f"This ID belongs to {described}, not a page, "
                        'and confluence_read(action="get") reads pages only.'
                    ),
                    content_type=content_type if isinstance(content_type, str) else None,
                )
            links = _mapping(item.get("_links"))
            return {
                "page": {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "space_key": space.get("key"),
                    "space_name": space.get("name"),
                    "version": _mapping(item.get("version")).get("number"),
                    **_content_people(item),
                    "body_storage": _mapping(_mapping(item.get("body")).get("storage")).get("value"),
                    "url": _absolute_url(_mapping(data.get("_links")).get("base"), links.get("webui")),
                },
            }

        return get_page

    def _confluence_list_attachments_op(self, page_id: str | None, limit: int | None, cursor: str | None) -> _Operation:
        page = _page_id(page_id)
        params: dict[str, str | int] = {
            "cql": f"type = attachment AND container = {page}",
            "limit": _page_size(25 if limit is None else limit),
            "expand": "version",
        }
        if next_cursor := _cursor(cursor, "cursor"):
            params["cursor"] = next_cursor

        async def list_attachments(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await self._require_page_space(access_token, site, page)
            path = "/wiki/rest/api/content/search"
            data = _mapping(await request_json(access_token, site, "confluence", "GET", path, params=params))
            results = data.get("results")
            return {
                "page_id": page,
                "attachments": [_attachment_summary(item) for item in results] if isinstance(results, list) else [],
                **_page_fields(*_next_cursor(data.get("_links"))),
            }

        return list_attachments

    def _confluence_download_op(
        self,
        page_id: str | None,
        attachment_id: str | None,
        filename: str | None,
    ) -> _Operation:
        page = _page_id(page_id)
        confluence_attachment_id = _confluence_attachment_id(attachment_id)
        context = get_tool_runtime_context()
        if context is None or context.storage_path is None:
            raise AtlassianError(
                code="attachment_context_unavailable",
                message="Attachment downloads require a conversation with MindRoom attachment storage.",
            )
        storage_path = context.storage_path
        inline_limit = inline_attachment_byte_limit(self._runtime_paths)
        max_bytes = min(inline_limit, RETAINED_MEDIA_MAX_BYTES)
        # Raising the setting helps only while an operator has lowered it below the fixed attachment limit.
        max_bytes_setting = INLINE_ATTACHMENT_BYTES_ENV if inline_limit < RETAINED_MEDIA_MAX_BYTES else None

        async def download_attachment(access_token: str, site: AtlassianSite) -> dict[str, object]:
            # Checking the page suffices: Confluence answers 404 for an attachment of another page under this path.
            await self._require_page_space(access_token, site, page)
            path = f"/wiki/rest/api/content/{page}/child/attachment/{confluence_attachment_id}/download"
            downloaded = await download(
                access_token,
                site,
                "confluence",
                path,
                max_bytes=max_bytes,
                max_bytes_setting=max_bytes_setting,
            )
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

        return download_attachment

    @tool(offer_background_after=True, offer_output_path=True)
    async def confluence_read(
        self,
        action: Literal["search", "get", "list_attachments", "download_attachment"],
        cql: str | None = None,
        page_id: str | None = None,
        attachment_id: str | None = None,
        filename: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> str:
        """Search Confluence, read a page, or list and download page attachments.

        - `search`: `cql` (optional: `limit` default 10, `cursor`). Each result names who created, owns, and last
          edited it; use `creator = currentUser()` or `contributor = currentUser()` for the requester's own content.
        - `get`: `page_id`. Returns storage-format body, current version number, and people metadata.
        - `list_attachments`: `page_id` (optional: `limit` default 25, `cursor`).
        - `download_attachment`: `page_id`, `attachment_id` (optional: `filename`). Returns a turn-scoped context
          attachment ID (`att_...`); open it with `attachment(action="get", attachment_id=...)` or save with
          `mindroom_output_path`.

        Args:
            action: Read operation (`search`, `get`, `list_attachments`, or `download_attachment`).
            cql: Confluence Query Language expression, for example 'type = page AND text ~ "release plan"'.
            page_id: Numeric Confluence page ID.
            attachment_id: Confluence attachment ID from confluence_read(action="list_attachments"), such as att123456.
            filename: Optional title from confluence_read(action="list_attachments"), used only when the download names no file.
            limit: Maximum results to return (defaults to 10 for `search`, 25 for `list_attachments`; values above 50 are capped).
            cursor: The next_cursor from a previous call, to fetch the next page.

        """
        return await self._dispatch(
            "confluence",
            action,
            {
                "search": lambda: self._confluence_search_op(cql, limit, cursor),
                "get": lambda: self._confluence_get_op(page_id),
                "list_attachments": lambda: self._confluence_list_attachments_op(page_id, limit, cursor),
                "download_attachment": lambda: self._confluence_download_op(page_id, attachment_id, filename),
            },
        )

    def _confluence_create_op(
        self,
        space_key: str | None,
        title: str | None,
        body_storage: str,
        parent_page_id: str | None,
    ) -> _Operation:
        if not isinstance(space_key, str) or not CONFLUENCE_SPACE_KEY_PATTERN.fullmatch(space_key):
            msg = "space_key must be a Confluence space key such as DOCS."
            raise _InvalidArgumentError(msg)
        resolved_space_key = space_key
        self._require_space("confluence", resolved_space_key)
        body: dict[str, object] = {
            "status": "current",
            "title": _required_text(title, "title"),
            "body": _storage_body(_required_text(body_storage, "body_storage")),
        }
        parent = _page_id(parent_page_id, field_name="parent_page_id") if parent_page_id is not None else None
        if parent is not None:
            body["parentId"] = parent

        async def create(access_token: str, site: AtlassianSite) -> dict[str, object]:
            if parent is not None:
                await self._require_page_space(access_token, site, parent)
            spaces = _mapping(
                await request_json(
                    access_token,
                    site,
                    "confluence",
                    "GET",
                    "/wiki/api/v2/spaces",
                    params={"keys": resolved_space_key, "limit": 1},
                ),
            ).get("results")
            space_id = next(
                (
                    space.get("id")
                    for space in spaces or []
                    if isinstance(space, dict)
                    and isinstance(space.get("key"), str)
                    and space["key"].casefold() == resolved_space_key.casefold()
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

        return create

    def _confluence_update_op(
        self,
        page_id: str | None,
        title: str | None,
        body_storage: str,
        version_number: int | None,
        version_message: str | None,
    ) -> _Operation:
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

        async def update(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await self._require_page_space(access_token, site, page)
            data = await request_json(
                access_token,
                site,
                "confluence",
                "PUT",
                f"/wiki/api/v2/pages/{page}",
                json_body=body,
            )
            return {"page": self._written_page(data)}

        return update

    def _confluence_comment_op(self, page_id: str | None, body_storage: str) -> _Operation:
        page = _page_id(page_id)
        body = {"pageId": page, "body": _storage_body(_required_text(body_storage, "body_storage"))}

        async def add_comment(access_token: str, site: AtlassianSite) -> dict[str, object]:
            await self._require_page_space(access_token, site, page)
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

        return add_comment

    @tool(offer_background_after=False, offer_output_path=False)
    async def confluence_write(
        self,
        action: Literal["create", "update", "comment"],
        body_storage: str,
        space_key: str | None = None,
        page_id: str | None = None,
        title: str | None = None,
        parent_page_id: str | None = None,
        version_number: int | None = None,
        version_message: str | None = None,
    ) -> str:
        """Create, update, or comment on a Confluence page.

        - `create`: `space_key`, `title`, `body_storage` (optional: `parent_page_id`).
        - `update`: `page_id`, `title`, `body_storage`, `version_number` (optional: `version_message`).
        - `comment`: `page_id`, `body_storage`.

        Args:
            action: Write operation (`create`, `update`, or `comment`).
            body_storage: Page or comment body in Confluence storage format (XHTML), for example "<p>Hello</p>".
            space_key: Key of the space to create the page in, such as DOCS.
            page_id: Numeric Confluence page ID.
            title: Page title (may be unchanged on `update`).
            parent_page_id: Optional numeric ID of the parent page for `create`.
            version_number: Page's current version number from confluence_read(action="get") plus one (for `update`).
            version_message: Optional change note stored with the new version (for `update`).

        """
        return await self._dispatch(
            "confluence",
            action,
            {
                "create": lambda: self._confluence_create_op(space_key, title, body_storage, parent_page_id),
                "update": lambda: self._confluence_update_op(
                    page_id,
                    title,
                    body_storage,
                    version_number,
                    version_message,
                ),
                "comment": lambda: self._confluence_comment_op(page_id, body_storage),
            },
        )

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
    """The default Atlassian connection, optionally pinned to one site, read-only access, and spaces per agent."""

    def __init__(
        self,
        site_url: str | None = None,
        cloud_id: str | None = None,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        write: bool = True,
        jira_spaces: list[str] | None = None,
        confluence_spaces: list[str] | None = None,
    ) -> None:
        super().__init__(
            provider=atlassian_oauth_provider(),
            function_prefix="",
            products=ATLASSIAN_PRODUCTS,
            write=write,
            site_url=site_url,
            cloud_id=cloud_id,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            jira_spaces=jira_spaces,
            confluence_spaces=confluence_spaces,
        )
