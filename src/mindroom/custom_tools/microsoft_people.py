"""Microsoft 365 People tools that read the organization directory as the requester through Microsoft Graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.microsoft_graph import (
    MicrosoftGraphError,
    graph_error_payload,
    graph_session,
    graph_tool_result,
    next_page_token,
    page_path,
    path_segment,
)
from mindroom.oauth.microsoft import microsoft_people_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.custom_tools.microsoft_graph import GraphSession
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_TOOL_NAME = "microsoft_people"
_MAX_LIMIT = 50
# A search sends the query twice, percent-encoded, and Graph refuses an advanced query over 2,048 URL characters.
_MAX_QUERY_CHARACTERS = 100
# The work contact fields; a person's mobile phone and other personal fields are never read.
_SELECT = "id,displayName,mail,userPrincipalName,jobTitle,department,officeLocation,businessPhones"
# Graph searches the directory only as an advanced query, with eventual consistency, and v1.0 refuses a search
# for an encoded ampersand unless legacy search is off.
_SEARCH_HEADERS = {"ConsistencyLevel": "eventual", "Prefer": "legacySearch=false"}
_USER_TYPE = "#microsoft.graph.user"


def _search(query: object) -> str:
    """Return the `$search` that matches display name words in any order, or an email address by its beginning."""
    text = query.strip() if isinstance(query, str) else ""
    if not text or len(text) > _MAX_QUERY_CHARACTERS:
        msg = f"query must be a name or email address of 1 to {_MAX_QUERY_CHARACTERS} characters."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    # Each clause is double-quoted, so its own quotes and backslashes are backslash-escaped.
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"displayName:{escaped}" OR "mail:{escaped}"'


def _top(limit: object) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        msg = f"limit must be an integer from 1 to {_MAX_LIMIT}."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return str(limit)


def _user_path(person: object) -> str:
    """Return the Graph path of a user named by id, sign-in name, or email address.

    A guest's sign-in name holds `#EXT#`, which Graph takes with each `#` percent-encoded, so the parts between
    hashes are checked and escaped one by one.
    """
    parts = person.split("#") if isinstance(person, str) else [person]
    try:
        return "/users/" + "%23".join(path_segment(part, "person") for part in parts)
    except MicrosoftGraphError:
        msg = "person must be a user id, an email address, or a sign-in name."
        raise MicrosoftGraphError(code="invalid_argument", message=msg) from None


def _user_id_by_mail(session: GraphSession, address: str) -> str | None:
    """Return the id of the one user with this email address, which need not be their sign-in name."""
    literal = address.replace("'", "''")
    payload = session.json("GET", "/users", params={"$filter": f"mail eq '{literal}'", "$select": "id"})
    ids = [entry.get("id") for entry in payload.get("value") or [] if isinstance(entry, dict)]
    return ids[0] if len(ids) == 1 and isinstance(ids[0], str) else None


def _read_user(session: GraphSession, person: str, suffix: str = "") -> dict[str, Any]:
    """Read a user, or one of its relationships, by id, sign-in name, or email address.

    A user's path takes only an id or a sign-in name, so an email address Graph does not find there is looked up
    by mail.
    """
    params = {"$select": _SELECT}
    try:
        return session.json("GET", f"{_user_path(person)}{suffix}", params=params)
    except MicrosoftGraphError as exc:
        user_id = _user_id_by_mail(session, person) if exc.code == "not_found" and "@" in person else None
        if user_id is None:
            raise
        return session.json("GET", f"{_user_path(user_id)}{suffix}", params=params)


def _is_user(entry: Mapping[str, Any]) -> bool:
    """Tell a user from a contact or another directory object; Graph names the type of every object that is not one."""
    return entry.get("@odata.type", _USER_TYPE) == _USER_TYPE


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _person(user: Mapping[str, Any]) -> dict[str, object]:
    phones = user.get("businessPhones")
    return {
        "id": user.get("id"),
        "name": user.get("displayName"),
        "email": _text(user.get("mail")) or _text(user.get("userPrincipalName")),
        "job_title": user.get("jobTitle"),
        "department": user.get("department"),
        "office_location": user.get("officeLocation"),
        "business_phones": [phone for phone in phones if isinstance(phone, str)] if isinstance(phones, list) else [],
    }


def _people(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    return [_person(entry) for entry in payload.get("value") or [] if isinstance(entry, dict) and _is_user(entry)]


class MicrosoftPeopleTools(Toolkit):
    """Look up people, their managers, and their direct reports in the requester's organization directory."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
    ) -> None:
        if credentials_manager is None:
            msg = "MicrosoftPeopleTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._provider = microsoft_people_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        super().__init__(
            name=_TOOL_NAME,
            tools=[
                self.microsoft_people_search,
                self.microsoft_people_get_profile,
                self.microsoft_people_list_direct_reports,
            ],
        )

    def _send[T](
        self,
        build: Callable[[], T],
        run: Callable[[GraphSession, T], Mapping[str, object]],
    ) -> str:
        """Refuse malformed arguments before any request, then send the requests and render their result."""
        try:
            request = build()
        except MicrosoftGraphError as exc:
            return graph_error_payload(_TOOL_NAME, exc)

        def operation() -> Mapping[str, object]:
            with graph_session(
                self._provider,
                self._runtime_paths,
                self._credentials_manager,
                self._worker_target,
                self._config,
            ) as session:
                return run(session, request)

        return graph_tool_result(_TOOL_NAME, operation)

    def microsoft_people_search(self, query: str, limit: int = 10) -> str:
        """Search the requester's organization directory for people by name or email address.

        Use it to find the person the requester means, and their email address for mail, calendar, and Teams.
        Names match by whole words or their beginnings, in any order, so "dan exam" finds Dana Example; email
        addresses match from their beginning. When several people match, ask the requester which one they mean
        instead of guessing. Someone added to the directory in the last few minutes may not show up yet.

        Args:
            query: A name, part of a name, or the beginning of an email address, up to 100 characters.
            limit: Maximum number of people to return, from 1 to 50.

        """
        return self._send(
            lambda: {"$search": _search(query), "$select": _SELECT, "$top": _top(limit)},
            lambda session, params: {
                "people": _people(session.json("GET", "/users", params=params, headers=_SEARCH_HEADERS)),
            },
        )

    def microsoft_people_get_profile(self, person: str) -> str:
        """Read one person's directory profile and their manager.

        manager is null when the directory names no manager for them, or names a contact instead of a user.

        Args:
            person: The person's id from microsoft_people_search, their email address, or their sign-in name
                (userPrincipalName).

        """

        def run(session: GraphSession, _path: str) -> Mapping[str, object]:
            profile = _read_user(session, person)
            try:
                manager = session.json("GET", f"{_user_path(profile.get('id'))}/manager", params={"$select": _SELECT})
            except MicrosoftGraphError as exc:
                # Graph answers 404 for a person without a manager.
                if exc.code != "not_found":
                    raise
                manager = {}
            return {"person": _person(profile), "manager": _person(manager) if manager and _is_user(manager) else None}

        return self._send(lambda: _user_path(person), run)

    def microsoft_people_list_direct_reports(self, person: str, page_token: str | None = None) -> str:
        """List the people who report directly to one person.

        Contacts and other directory entries that are not user accounts are left out, so a page may hold fewer
        people, or none, while next_page_token still continues the listing.

        Args:
            person: The manager's id from microsoft_people_search, their email address, or their sign-in name
                (userPrincipalName).
            page_token: The next_page_token of the previous call, to continue the same listing.

        """

        def build() -> str | None:
            if page_token is not None:
                return page_path(page_token, "/directReports")
            _user_path(person)
            return None

        def run(session: GraphSession, next_page: str | None) -> Mapping[str, object]:
            payload = session.json("GET", next_page) if next_page else _read_user(session, person, "/directReports")
            return {"direct_reports": _people(payload), "next_page_token": next_page_token(payload)}

        return self._send(build, run)
