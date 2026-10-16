"""The plain sentence each broker refusal carries, so an agent can tell its user what went wrong and where to fix it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["CONNECTIONS_PAGE_NOT_SERVED", "ScopeInfo", "git_error_text", "refusal_body"]

# Texts from the design spec (sections 12.5 and 14). They name only a service, provider, host, path without its
# query, or manager IDs, never a secret, token, or connect link: the links travel in their own fields, which the texts
# name. `git_error_text` rewrites those link names for git's ERR line, so it must follow any rewording of them.
_PATH_OUTSIDE = "{host}{path} is outside the paths {service} allows for this agent. "
_SHARED_MISSING = "No {service} key is set for this shared agent. "
_MESSAGES = {
    "credential_not_configured": "No {service} key is set for this agent. Ask the user to add one at manage_url, then retry.",
    "oauth_connection_required": (
        "The {provider} account for {service} must be connected again. "
        "Ask the user to reconnect at connect_url or manage_url."
    ),
    "oauth_refresh_failed": (
        "{provider} did not refresh the account token. "
        "Retry in a minute; if it keeps failing, ask the user to reconnect at manage_url."
    ),
    "path_not_allowed": _PATH_OUTSIDE + "Ask the user to add it at manage_url if the agent needs it.",
    "bad_request": (
        "The request path is ambiguous (dot segments, //, backslashes, or encoded forms of them) and was refused. "
        "Use a plain path."
    ),
    "method_not_allowed": (
        "TRACE and TRACK requests are refused because the server would echo the request, "
        "injected credential included, back to the agent."
    ),
    "tls_required": "Credentials are only added over HTTPS. Use https:// for {host}.",
    "host_mismatch": "The Host header does not match the host this connection was opened for.",
    "host_not_allowed": "{host} is not allowed by the administrator's egress policy.",
    "destination_blocked": "{host} resolves to a private or internal address that the administrator's egress policy does not let agents reach.",
    "request_body_too_large": "The request body is larger than the broker accepts.",
    "upstream_unreachable": "{host} could not be reached.",
    "upstream_timeout": "{host} did not answer in time.",
    "broker_error": "The credential broker hit an internal error. Retry; if it keeps failing, tell an administrator.",
    "proxy_authentication_required": "The sandbox's proxy credentials are missing or expired. Start a new tool call.",
}
_CONNECTABLE = (
    "No {service} credential is set for this agent. "
    "Ask the user to connect it at connect_url or add a key at manage_url, then retry."
)
_SHARED = _SHARED_MISSING + "Only its credential managers ({managers}) can add one, at manage_url."
_SHARED_WITHOUT_MANAGERS = _SHARED_MISSING + "Only an administrator can add one, at manage_url."
_PATH_SHARED = _PATH_OUTSIDE + "Only its credential managers ({managers}) can change this, at manage_url."
_PATH_ADMINISTRATOR = _PATH_OUTSIDE + "Only an administrator can change this, at manage_url."
# The spec words `bad_request` for the ambiguous-path refusal; a request the broker cannot read at all has no path.
_MALFORMED = "The request is malformed and was refused."
# A personal scope's keys, accounts, and services are set only on the personal page. Where it is not served, its
# refusals take their texts from `_WITHOUT_PAGE`, which names no page, or `_WITHOUT_PAGE_LINKED` when the body carries
# a connect link, and `refusal_body` drops `manage_url`, the dashboard. The `egress_credentials` tool ends its note
# with the same sentences, and the key and reconnect texts stay short enough for the 200-character reason phrase.
CONNECTIONS_PAGE_NOT_SERVED = (
    "on the Connections page, which this deployment does not serve. "
    "An administrator can enable trusted upstream auth or make the agent shared."
)
_WITHOUT_PAGE = {
    **_MESSAGES,
    "credential_not_configured": "No {service} key: each user sets this agent's keys " + CONNECTIONS_PAGE_NOT_SERVED,
    "oauth_connection_required": (
        "Reconnect the {provider} account for {service}: each user does it " + CONNECTIONS_PAGE_NOT_SERVED
    ),
    "oauth_refresh_failed": (
        "{provider} did not refresh the account token. Retry in a minute; if it keeps failing, tell an administrator."
    ),
    "path_not_allowed": _PATH_OUTSIDE + "Each user sets this agent's services " + CONNECTIONS_PAGE_NOT_SERVED,
}
_WITHOUT_PAGE_LINKED = {
    **_WITHOUT_PAGE,
    "credential_not_configured": (
        "No {service} credential is set for this agent. Ask the user to connect it at connect_url, then retry."
    ),
    "oauth_connection_required": (
        "The {provider} account for {service} must be connected again. Ask the user to reconnect at connect_url."
    ),
}


@dataclass(frozen=True)
class ScopeInfo:
    """Who can fix a missing key for one requester's scope.

    `shared` is true for a `shared` or unscoped agent, whose keys only its credential managers (`managers`, Matrix
    IDs) and administrators may set; a personal (`user` or `user_agent`) scope's user sets their own, on the personal
    page. `personal_page` says whether this deployment serves that page; without it nobody can set a personal key.
    """

    shared: bool
    managers: tuple[str, ...]
    personal_page: bool


def _template(
    code: str,
    *,
    path: str | None,
    managers: Sequence[str],
    shared: bool,
    personal_page: bool,
    connectable: bool,
    by_operator: bool,
) -> str:
    without_page = not (shared or personal_page)
    if code == "credential_not_configured" and connectable and not without_page:
        return _CONNECTABLE
    if code == "credential_not_configured" and shared:
        return _SHARED if managers else _SHARED_WITHOUT_MANAGERS
    if code == "path_not_allowed" and (by_operator or (shared and not managers)):
        return _PATH_ADMINISTRATOR
    if code == "path_not_allowed" and shared:
        return _PATH_SHARED
    if code == "bad_request" and path is None:
        return _MALFORMED
    texts = _MESSAGES
    if without_page:
        texts = _WITHOUT_PAGE_LINKED if connectable else _WITHOUT_PAGE
    try:
        return texts[code]
    except KeyError:
        msg = f"No refusal message for {code}."
        raise ValueError(msg) from None


def refusal_body(
    code: str,
    *,
    host: str | None = None,
    path: str | None = None,
    service: str | None = None,
    service_label: str | None = None,
    provider: str | None = None,
    managers: Sequence[str] | None = None,
    shared: bool = False,
    personal_page: bool = True,
    connectable: bool = False,
    by_operator: bool = False,
    **extra: object,
) -> dict[str, object]:
    """Return the JSON body for a refusal: its `error` code, `message`, `service` and `provider` when given, and `extra`.

    `host`, `path`, `service_label`, and `managers` only fill in the message; `path` is cut at its query, which could
    hold a secret, and `service_label` (a display name) names the service there instead of `service`.
    For `credential_not_configured`, `connectable` (the body carries a connect link) offers connecting an account as
    well as adding a key, and otherwise a `shared` scope names its `managers` (or an administrator when there are
    none); a `shared` `path_not_allowed` names them as the ones who can change the limit. A `path_not_allowed` set by
    the operator's service (`by_operator`) names an administrator whatever the scope, since only an administrator
    edits it.
    A personal scope without the personal page (`personal_page` false) has no page that sets its keys, accounts, or
    services: its texts say so and offer only a connect link, for `oauth_connection_required` too where `connectable`
    says the body carries one, and the body leaves out `manage_url`, which would be the dashboard.
    A `bad_request` with a `path` is the ambiguous-path refusal; without one, the request itself was malformed.
    Raises ValueError for a code without a text and KeyError when the text needs a value that was not given.
    """
    template = _template(
        code,
        path=path,
        managers=managers or (),
        shared=shared,
        personal_page=personal_page,
        connectable=connectable,
        by_operator=by_operator,
    )
    if "manage_url" not in template:
        # A body carries the manage link only where its text names it.
        extra.pop("manage_url", None)
    values = {
        "host": host,
        "path": None if path is None else path.split("?", 1)[0],
        "service": service_label or service,
        "provider": provider,
        "managers": ", ".join(managers or ()),
    }
    message = template.format_map({name: value for name, value in values.items() if value is not None})
    body: dict[str, object] = {"error": code}
    if service is not None:
        body["service"] = service
    if provider is not None:
        body["provider"] = provider
    return {**body, **extra, "message": message}


def git_error_text(body: dict[str, object]) -> str:
    """Return the message for git's ERR line, naming the body's `manage_url` itself, since git shows no other field.

    The connect link is never named: it carries a one-time token. Where the message offers it, the manage link
    stands in.
    """
    message = str(body["message"])
    link = body.get("manage_url")
    if not isinstance(link, str) or not link:
        return message
    message = message.replace("connect_url or manage_url", "manage_url").replace("connect_url", "manage_url")
    return message.replace("manage_url", link)
