"""Tests for the Microsoft 365 People toolkit."""
# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.tools.function import Function

from mindroom.custom_tools.microsoft_people import MicrosoftPeopleTools
from mindroom.oauth.microsoft import microsoft_people_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess
from tests.microsoft_graph_test_support import (
    FakeGraph,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
DANA = "6ea91a8d-e32e-41a1-b7bd-d2d185eed0e0"
SAM = "7d54cb02-aaa3-4016-9f9c-a4b49422dd9b"
SELECT = "id,displayName,mail,userPrincipalName,jobTitle,department,officeLocation,businessPhones"
NEXT_REPORTS = f"/users/{SAM}/directReports?$select={SELECT}&$skiptoken=reports2"
FUNCTIONS = (
    "microsoft_people_search",
    "microsoft_people_get_profile",
    "microsoft_people_list_direct_reports",
)


def _user(user_id: str = DANA, **overrides: object) -> dict[str, object]:
    return {
        "id": user_id,
        "displayName": "Dana Example",
        "mail": "dana@example.com",
        "userPrincipalName": "dana.example@example.onmicrosoft.com",
        "jobTitle": "Designer",
        "department": "Product",
        "officeLocation": "Building 2",
        "businessPhones": ["+1 425 555 0109"],
        "mobilePhone": "+1 425 555 0199",
        **overrides,
    }


def _person(user_id: str = DANA, **overrides: object) -> dict[str, object]:
    return {
        "id": user_id,
        "name": "Dana Example",
        "email": "dana@example.com",
        "job_title": "Designer",
        "department": "Product",
        "office_location": "Building 2",
        "business_phones": ["+1 425 555 0109"],
        **overrides,
    }


def _answer(payload: dict[str, object], status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}})


def _routes(routes: dict[str, httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each request by its raw path, as Graph would for the first matching resource."""

    def answer(request: httpx.Request) -> httpx.Response:
        return routes[_raw_path(request)]

    return answer


def _tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[MicrosoftPeopleTools, FakeGraph]:
    graph = FakeGraph(handler).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(microsoft_people_oauth_provider(), manager)
    tools = MicrosoftPeopleTools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(),
        runtime_config=None,
    )
    return tools, graph


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def _raw_path(request: httpx.Request) -> str:
    return request.url.raw_path.split(b"?")[0].decode("ascii")


def _description(tools: MicrosoftPeopleTools, name: str) -> str:
    """Return the description the model sees; deferred toolkits do not show toolkit instructions."""
    return " ".join(Function.from_callable(getattr(tools, name)).to_dict()["description"].split())


def test_registered_metadata_declares_a_read_only_requester_scoped_tool() -> None:
    metadata = TOOL_METADATA["microsoft_people"]

    assert metadata.display_name == "Microsoft 365 People"
    assert metadata.category == ToolCategory.PRODUCTIVITY
    assert metadata.auth_provider == "microsoft_people"
    assert metadata.setup_type == SetupType.OAUTH
    assert metadata.requires_primary_runtime is True
    assert metadata.file_access == ToolFileAccess.NONE
    assert metadata.consumes_workspace_paths is False
    assert metadata.function_names == FUNCTIONS
    assert not metadata.config_fields


def test_every_function_is_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _answer({}))

    assert tuple(tools.functions) == FUNCTIONS


def test_search_asks_graph_for_matching_names_and_email_addresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": [_user()]}))

    result = _payload(tools.microsoft_people_search("  Dana  "))

    [request] = graph.requests
    assert (request.method, request.url.path) == ("GET", "/v1.0/users")
    assert dict(request.url.params) == {
        "$search": '"displayName:Dana" OR "mail:Dana"',
        "$select": SELECT,
        "$top": "10",
    }
    assert request.headers["ConsistencyLevel"] == "eventual"
    assert request.headers["Prefer"] == "legacySearch=false"
    assert result["status"] == "ok"
    assert result["people"] == [_person()]


def test_search_escapes_quotes_and_backslashes_and_encodes_ampersands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": []}))

    tools.microsoft_people_search('Dana "DJ" A\\B & Co', limit=50)

    [request] = graph.requests
    clause = 'Dana \\"DJ\\" A\\\\B & Co'
    assert request.url.params["$search"] == f'"displayName:{clause}" OR "mail:{clause}"'
    assert request.url.params["$top"] == "50"
    assert b"%26" in request.url.query


def test_search_returns_only_the_directory_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users = [
        _user(mail=None, businessPhones=["+1 425 555 0100", 7]),
        _user(SAM, displayName="Sam Example", mail="", jobTitle=None, department=None, officeLocation=None),
        "not a user",
    ]
    tools, _graph = _tools(tmp_path, monkeypatch, _answer({"value": users}))

    people = _payload(tools.microsoft_people_search("example"))["people"]

    assert people == [
        _person(email="dana.example@example.onmicrosoft.com", business_phones=["+1 425 555 0100"]),
        _person(
            SAM,
            name="Sam Example",
            email="dana.example@example.onmicrosoft.com",
            job_title=None,
            department=None,
            office_location=None,
        ),
    ]
    assert all("mobile" not in json.dumps(person) for person in people)


@pytest.mark.parametrize("query", ["", "   ", 5, None, "x" * 101])
def test_search_refuses_an_empty_or_overlong_query_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": []}))

    assert _payload(tools.microsoft_people_search(query))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


@pytest.mark.parametrize("limit", [0, 51, True, "5", 2.5])
def test_search_refuses_a_limit_outside_one_to_fifty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": []}))

    assert _payload(tools.microsoft_people_search("Dana", limit=limit))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


def test_get_profile_returns_the_person_and_their_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = {"@odata.type": "#microsoft.graph.user", **_user(SAM, displayName="Sam Example", mail="sam@example.com")}
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                "/v1.0/users/dana@example.com": httpx.Response(200, json=_user()),
                f"/v1.0/users/{DANA}/manager": httpx.Response(200, json=manager),
            },
        ),
    )

    result = _payload(tools.microsoft_people_get_profile("dana@example.com"))

    assert [(request.method, _raw_path(request)) for request in graph.requests] == [
        ("GET", "/v1.0/users/dana@example.com"),
        ("GET", f"/v1.0/users/{DANA}/manager"),
    ]
    assert [dict(request.url.params) for request in graph.requests] == [{"$select": SELECT}, {"$select": SELECT}]
    assert result["status"] == "ok"
    assert result["person"] == _person()
    assert result["manager"] == _person(SAM, name="Sam Example", email="sam@example.com")


def test_get_profile_reports_no_manager_when_graph_finds_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {f"/v1.0/users/{DANA}": httpx.Response(200, json=_user()), f"/v1.0/users/{DANA}/manager": _not_found()},
        ),
    )

    result = _payload(tools.microsoft_people_get_profile(DANA))

    assert len(graph.requests) == 2
    assert result["status"] == "ok"
    assert (result["person"], result["manager"]) == (_person(), None)


def test_get_profile_leaves_out_a_manager_that_is_not_a_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contact = {"@odata.type": "#microsoft.graph.orgContact", "id": SAM, "displayName": "Sam Contact"}
    tools, _graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                f"/v1.0/users/{DANA}": httpx.Response(200, json=_user()),
                f"/v1.0/users/{DANA}/manager": httpx.Response(200, json=contact),
            },
        ),
    )

    assert _payload(tools.microsoft_people_get_profile(DANA))["manager"] is None


def test_get_profile_finds_an_email_address_that_is_not_the_sign_in_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "o'brien@example.com"
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                "/v1.0/users/o%27brien@example.com": _not_found(),
                "/v1.0/users": httpx.Response(200, json={"value": [{"id": DANA}]}),
                f"/v1.0/users/{DANA}": httpx.Response(200, json=_user(mail=address)),
                f"/v1.0/users/{DANA}/manager": _not_found(),
            },
        ),
    )

    result = _payload(tools.microsoft_people_get_profile(address))

    assert [_raw_path(request) for request in graph.requests] == [
        "/v1.0/users/o%27brien@example.com",
        "/v1.0/users",
        f"/v1.0/users/{DANA}",
        f"/v1.0/users/{DANA}/manager",
    ]
    assert dict(graph.requests[1].url.params) == {"$filter": "mail eq 'o''brien@example.com'", "$select": "id"}
    assert result["person"] == _person(email=address)


@pytest.mark.parametrize("matches", [[], [{"id": DANA}, {"id": SAM}]])
def test_get_profile_reports_not_found_unless_exactly_one_user_has_the_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matches: list[dict[str, str]],
) -> None:
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                "/v1.0/users/dana@example.com": _not_found(),
                "/v1.0/users": httpx.Response(200, json={"value": matches}),
            },
        ),
    )

    result = _payload(tools.microsoft_people_get_profile("dana@example.com"))

    assert (result["status"], result["code"]) == ("error", "not_found")
    assert len(graph.requests) == 2


@pytest.mark.parametrize("me", ["me", "ME", "Me"])
def test_get_profile_reads_the_requester_and_their_manager_as_me(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    me: str,
) -> None:
    manager = {"@odata.type": "#microsoft.graph.user", **_user(SAM, displayName="Sam Example", mail="sam@example.com")}
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes({"/v1.0/me": httpx.Response(200, json=_user()), "/v1.0/me/manager": httpx.Response(200, json=manager)}),
    )

    result = _payload(tools.microsoft_people_get_profile(me))

    assert [(_raw_path(request), dict(request.url.params)) for request in graph.requests] == [
        ("/v1.0/me", {"$select": SELECT}),
        ("/v1.0/me/manager", {"$select": SELECT}),
    ]
    assert result["person"] == _person()
    assert result["manager"] == _person(SAM, name="Sam Example", email="sam@example.com")


def test_get_profile_reports_no_manager_for_me(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(
        tmp_path,
        monkeypatch,
        _routes({"/v1.0/me": httpx.Response(200, json=_user()), "/v1.0/me/manager": _not_found()}),
    )

    result = _payload(tools.microsoft_people_get_profile("me"))

    assert (result["status"], result["person"], result["manager"]) == ("ok", _person(), None)


def test_get_profile_keeps_the_original_not_found_when_the_email_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                "/v1.0/users/dana@example.com": _not_found(),
                "/v1.0/users": httpx.Response(400, json={"error": {"code": "Request_UnsupportedQuery"}}),
            },
        ),
    )

    result = _payload(tools.microsoft_people_get_profile("dana@example.com"))

    assert (result["code"], result["graph_code"]) == ("not_found", "Request_ResourceNotFound")
    assert len(graph.requests) == 2


def test_get_profile_does_not_look_up_an_id_by_email(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, lambda _request: _not_found())

    result = _payload(tools.microsoft_people_get_profile(DANA))

    assert (result["code"], result["graph_code"]) == ("not_found", "Request_ResourceNotFound")
    assert len(graph.requests) == 1


def test_get_profile_reports_a_manager_lookup_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                f"/v1.0/users/{DANA}": httpx.Response(200, json=_user()),
                f"/v1.0/users/{DANA}/manager": httpx.Response(
                    403,
                    json={"error": {"code": "Authorization_RequestDenied"}},
                ),
            },
        ),
    )

    result = _payload(tools.microsoft_people_get_profile(DANA))

    assert (result["status"], result["code"]) == ("error", "forbidden")


def test_list_direct_reports_returns_only_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = {
        "value": [
            {"@odata.type": "#microsoft.graph.user", **_user()},
            {"@odata.type": "#microsoft.graph.orgContact", "id": "c1", "displayName": "Vendor Contact"},
            {"@odata.type": "#microsoft.graph.group", "id": "g1", "displayName": "Some Group"},
        ],
        "@odata.nextLink": f"{GRAPH}{NEXT_REPORTS}",
    }
    tools, graph = _tools(tmp_path, monkeypatch, _answer(reports))

    result = _payload(tools.microsoft_people_list_direct_reports(SAM))

    [request] = graph.requests
    assert (request.method, _raw_path(request)) == ("GET", f"/v1.0/users/{SAM}/directReports")
    assert dict(request.url.params) == {"$select": SELECT}
    assert result["direct_reports"] == [_person()]
    assert result["next_page_token"] == NEXT_REPORTS


def test_list_direct_reports_follows_its_own_page_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": []}))

    result = _payload(tools.microsoft_people_list_direct_reports(SAM, page_token=NEXT_REPORTS))

    assert (result["direct_reports"], result["next_page_token"]) == ([], None)
    [request] = graph.requests
    expected = httpx.URL(f"{GRAPH}{NEXT_REPORTS}")
    assert (request.method, request.url.raw_path) == ("GET", expected.raw_path)


@pytest.mark.parametrize(
    "page_token",
    [
        f"{GRAPH}{NEXT_REPORTS}",
        f"/users/{SAM}/directReports",
        f"/users/{SAM}/../../me/messages/x/directReports?$skiptoken=a",
        "/users/%2e%2e/directReports?$skiptoken=a",
        "/me/messages?$skiptoken=a",
        f"/users/{SAM}/directReportsX?$skiptoken=a",
        "",
    ],
)
def test_list_direct_reports_refuses_a_foreign_page_token_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": []}))

    result = _payload(tools.microsoft_people_list_direct_reports(SAM, page_token=page_token))

    assert result["code"] == "invalid_page_token"
    assert graph.requests == []


def test_list_direct_reports_of_me_pages_from_the_requester(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    next_page = f"/me/directReports?$select={SELECT}&$skiptoken=mine2"
    pages = iter(
        [
            httpx.Response(200, json={"value": [_user()], "@odata.nextLink": f"{GRAPH}{next_page}"}),
            httpx.Response(200, json={"value": [_user(SAM, displayName="Sam Example")]}),
        ],
    )
    tools, graph = _tools(tmp_path, monkeypatch, lambda _request: next(pages))

    first = _payload(tools.microsoft_people_list_direct_reports("Me"))
    second = _payload(tools.microsoft_people_list_direct_reports("Me", page_token=first["next_page_token"]))

    assert first["next_page_token"] == next_page
    assert (first["direct_reports"], second["direct_reports"]) == ([_person()], [_person(SAM, name="Sam Example")])
    assert second["next_page_token"] is None
    assert [(_raw_path(request), dict(request.url.params)) for request in graph.requests] == [
        ("/v1.0/me/directReports", {"$select": SELECT}),
        ("/v1.0/me/directReports", {"$select": SELECT, "$skiptoken": "mine2"}),
    ]


def test_list_direct_reports_finds_an_email_address_that_is_not_the_sign_in_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                "/v1.0/users/sam@example.com/directReports": _not_found(),
                "/v1.0/users": httpx.Response(200, json={"value": [{"id": SAM}]}),
                f"/v1.0/users/{SAM}/directReports": httpx.Response(200, json={"value": [_user()]}),
            },
        ),
    )

    result = _payload(tools.microsoft_people_list_direct_reports("sam@example.com"))

    assert [_raw_path(request) for request in graph.requests] == [
        "/v1.0/users/sam@example.com/directReports",
        "/v1.0/users",
        f"/v1.0/users/{SAM}/directReports",
    ]
    assert result["direct_reports"] == [_person()]


def test_people_are_escaped_as_single_path_segments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({"value": []}))

    tools.microsoft_people_list_direct_reports("kim+test@example.com")

    assert _raw_path(graph.requests[0]) == "/v1.0/users/kim%2Btest@example.com/directReports"


def test_guest_sign_in_names_reach_graph_with_encoded_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guest = "dana_partner.example#EXT#@example.onmicrosoft.com"
    encoded = "/v1.0/users/dana_partner.example%23EXT%23@example.onmicrosoft.com"
    tools, graph = _tools(
        tmp_path,
        monkeypatch,
        _routes(
            {
                encoded: httpx.Response(200, json=_user()),
                f"/v1.0/users/{DANA}/manager": _not_found(),
                f"{encoded}/directReports": httpx.Response(200, json={"value": []}),
            },
        ),
    )

    assert _payload(tools.microsoft_people_get_profile(guest))["status"] == "ok"
    assert _payload(tools.microsoft_people_list_direct_reports(guest))["status"] == "ok"
    assert [_raw_path(request) for request in graph.requests] == [
        encoded,
        f"/v1.0/users/{DANA}/manager",
        f"{encoded}/directReports",
    ]


@pytest.mark.parametrize("bad_person", ["", " ", "..", "a/b", "a\\b", "a?b", "#", "a##b", "a\nb", 7, None])
def test_malformed_people_are_refused_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_person: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _answer({}))

    results = [
        tools.microsoft_people_get_profile(bad_person),  # type: ignore[arg-type]
        tools.microsoft_people_list_direct_reports(bad_person),  # type: ignore[arg-type]
    ]

    assert [_payload(result)["code"] for result in results] == ["invalid_argument", "invalid_argument"]
    assert graph.requests == []


def test_descriptions_point_the_agent_at_the_ids_and_addresses_it_needs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _answer({}))

    assert "ask the requester which one they mean" in _description(tools, "microsoft_people_search")
    person = Function.from_callable(tools.microsoft_people_get_profile).to_dict()["parameters"]["properties"]["person"]
    assert "email address" in person["description"]
    assert '"me" for the requester' in person["description"]


@pytest.mark.parametrize(("status", "code"), [(403, "forbidden"), (404, "not_found")])
def test_graph_errors_keep_their_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, code: str) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _answer({"error": {"code": "Authorization_RequestDenied"}}, status))

    result = _payload(tools.microsoft_people_search("Dana"))

    assert (result["status"], result["code"], result["graph_code"]) == ("error", code, "Authorization_RequestDenied")


def test_throttling_reports_the_retry_delay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(
        tmp_path,
        monkeypatch,
        lambda _request: httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}}),
    )

    result = _payload(tools.microsoft_people_list_direct_reports(SAM))

    assert (result["code"], result["retry_after_seconds"]) == ("throttled", 7)
