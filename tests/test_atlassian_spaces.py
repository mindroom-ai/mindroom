"""Per-agent read-only access and Jira and Confluence space limits for the Atlassian toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.custom_tools.atlassian import AtlassianTools
from mindroom.custom_tools.atlassian_spaces import AtlassianSpaces, UnconfinableQueryError
from mindroom.oauth.atlassian import atlassian_function_names, atlassian_oauth_provider
from mindroom.tool_system.agno_toolkits import toolkit_function_names
from mindroom.tool_system.metadata import (
    ToolConfigOverrideError,
    get_tool_by_name,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.atlassian_test_support import (
    SITE_URL,
    FakeGateway,
    gateway_url,
    json_body,
    publish_grant,
    runtime_paths,
    save_client_config,
    site,
    tool_context,
    worker_target,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.agno_toolkits import AnyToolkit

TOKEN = "alice-access-token"  # noqa: S105
CONTENT_SEARCH = gateway_url("confluence", "/wiki/rest/api/content/search")


def _connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored: dict[str, object] | None = None,
    **options: object,
) -> tuple[AnyToolkit, FakeGateway]:
    """Build the agent's toolkit through the catalog, from ``stored`` settings and an agent tool entry's ``options``."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, TOKEN)
    gateway = FakeGateway(sites_by_token={TOKEN: [site()]}).install(monkeypatch)
    tool = get_tool_by_name(
        "atlassian",
        paths,
        credentials_manager=manager,
        credential_overrides=stored,
        tool_config_overrides=validate_authored_tool_entry_overrides(
            "atlassian",
            options,
            tool_metadata=builtin_tool_registry().entries,
        ),
        worker_target=worker_target(),
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )
    return tool, gateway


async def _call(tool: AnyToolkit, function_name: str, **kwargs: object) -> dict[str, Any]:
    return json.loads(await tool.functions[function_name](**kwargs))


def _issue(key: str, project: str) -> dict[str, object]:
    return {"id": "1", "key": key, "fields": {"summary": "Work", "project": {"key": project}}}


def _page_in(space_key: str) -> dict[str, object]:
    return {"results": [{"id": "123", "type": "page", "space": {"key": space_key}}]}


@pytest.mark.parametrize(
    ("product", "query", "expected"),
    [
        ("confluence", "type = page", 'space in ("DOCS", "~alice") AND (type = page)'),
        (
            "confluence",
            'text ~ "order by" ORDER BY created DESC',
            'space in ("DOCS", "~alice") AND (text ~ "order by") ORDER BY created DESC',
        ),
        (
            "confluence",
            "title ~ 'it\\'s order by' order  by title",
            'space in ("DOCS", "~alice") AND (title ~ \'it\\\'s order by\') order  by title',
        ),
        ("confluence", "ORDER BY lastmodified", 'space in ("DOCS", "~alice") ORDER BY lastmodified'),
        ("confluence", "text ~ reorder by", 'space in ("DOCS", "~alice") AND (text ~ reorder by)'),
        (
            "jira",
            'summary ~ "a ) b" AND (status = Done OR status = Open) ORDER BY key',
            'project in ("ENG") AND (summary ~ "a ) b" AND (status = Done OR status = Open)) ORDER BY key',
        ),
        ("jira", "(x) ORDER BY key", 'project in ("ENG") AND ((x)) ORDER BY key'),
        ("jira", "(status = Done ORDER BY key)", 'project in ("ENG") AND ((status = Done ORDER BY key))'),
    ],
)
def test_queries_are_limited_to_allowed_spaces_with_order_by_last(product: str, query: str, expected: str) -> None:
    """The space clause wraps the whole condition, and a trailing ORDER BY outside quotes stays last."""
    spaces = AtlassianSpaces.from_config(jira_spaces=["ENG", "eng"], confluence_spaces=["DOCS", "~alice"])

    assert spaces.restrict_query(product, query) == expected  # type: ignore[arg-type]
    assert AtlassianSpaces().restrict_query(product, query) == query  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "query",
    [
        'key = ENG-1) OR (project = OPS AND summary ~ "acquisition"',
        "status = Done) OR (project = OPS",
        "(status = Done",
        'summary ~ "unclosed',
        "summary ~ don't) OR (project = OPS AND summary ~ 'x",
        "summary ~ a\\(b\\)",
        "status = Done ORDER BY key) OR (project = OPS",
    ],
)
def test_queries_that_could_escape_the_space_clause_are_refused(query: str) -> None:
    """Only a query that is one closed operand is limited, so no query can probe other spaces through paging."""
    spaces = AtlassianSpaces.from_config(jira_spaces=["ENG"], confluence_spaces=None)

    if "don't" in query:
        # Quotes pair up inside a word too, so this is one balanced query whose second quote closes the first.
        assert spaces.restrict_query("jira", query).startswith('project in ("ENG") AND (')
        return
    with pytest.raises(UnconfinableQueryError):
        spaces.restrict_query("jira", query)
    assert spaces.restrict_query("confluence", query) == query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function_name", "kwargs"),
    [
        ("jira_search_issues", {"jql": 'key = ENG-1) OR (project = OPS AND summary ~ "acquisition"', "max_results": 1}),
        ("confluence_search", {"cql": 'id = 1) OR (space = OPS AND text ~ "acquisition"', "limit": 1}),
    ],
)
async def test_escaping_search_is_refused_before_any_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    kwargs: dict[str, object],
) -> None:
    """A search that would close the space clause's parentheses never reaches Atlassian, so paging reveals nothing."""
    tool, gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"], confluence_spaces=["DOCS"])

    result = await _call(tool, function_name, **kwargs)

    assert result["code"] == "invalid_argument"
    assert "a closing parenthesis without an opening one" in result["message"]
    assert gateway.requests == []


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"jira_spaces": ["ENG-1"]}, ValueError),
        ({"confluence_spaces": ['DOCS") OR (space = "OPS']}, ValueError),
        ({"jira_spaces": "ENG"}, TypeError),
    ],
)
def test_invalid_space_keys_fail_at_construction(tmp_path: Path, options: dict[str, object], error: type) -> None:
    """A key that could change the space clause keeps the toolkit from being built."""
    paths = runtime_paths(tmp_path)

    with pytest.raises(error):
        AtlassianTools(
            runtime_paths=paths,
            credentials_manager=save_client_config(paths),
            worker_target=worker_target(),
            **options,  # type: ignore[arg-type]
        )


def test_space_limits_are_set_per_agent_only() -> None:
    """Agent tool entries may pin spaces, while installation-wide tool defaults may not."""
    metadata = builtin_tool_registry().entries["atlassian"]

    assert {field.name for field in metadata.config_fields or []} == {"site_url", "cloud_id", "write"}
    assert {field.name for field in metadata.agent_override_fields or []} == {"jira_spaces", "confluence_spaces"}
    with pytest.raises(ToolConfigOverrideError, match="unknown authored override"):
        validate_authored_tool_entry_overrides(
            "atlassian",
            {"confluence_spaces": ["DOCS"]},
            config_path_prefix="defaults.tools",
            tool_metadata=builtin_tool_registry().entries,
        )


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (["ENG", " ops "], ["ENG", "ops"]),
        ("ENG, OPS", ["ENG", "OPS"]),
        (["Team Docs"], None),
        (["ENG-1"], None),
    ],
)
def test_space_keys_are_checked_when_the_config_loads(value: object, accepted: list[str] | None) -> None:
    """A space name or an issue key in a space list fails config validation instead of dropping the tool."""
    entries = builtin_tool_registry().entries
    if accepted is None:
        with pytest.raises(
            ToolConfigOverrideError,
            match=r"agents\.helper\.tools\.atlassian\.jira_spaces: .* is not a valid .* such as ENG\.",
        ):
            validate_authored_tool_entry_overrides(
                "atlassian",
                {"jira_spaces": value},
                config_path_prefix="agents.helper.tools",
                tool_metadata=entries,
            )
    else:
        validated = validate_authored_tool_entry_overrides("atlassian", {"jira_spaces": value}, tool_metadata=entries)
        assert validated == {"jira_spaces": accepted}


def test_stored_settings_never_widen_the_agent_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saved settings cannot set space lists, and the agent entry's write: false wins over a saved true."""
    tool, _gateway = _connected(
        tmp_path,
        monkeypatch,
        stored={"write": True, "jira_spaces": ["OPS"], "confluence_spaces": []},
        write=False,
        jira_spaces=["ENG"],
    )

    assert tuple(toolkit_function_names(tool)) == atlassian_function_names(write=False)
    assert tool.instructions == "The `jira_*` functions may use only these Jira spaces: ENG."


def test_model_is_told_the_allowed_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The toolkit's instructions name the allowed keys of each offered product, and nothing when unrestricted."""
    limited, _gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"], confluence_spaces=["DOCS", "~alice"])
    unrestricted = AtlassianTools(
        runtime_paths=runtime_paths(tmp_path),
        credentials_manager=save_client_config(runtime_paths(tmp_path)),
        worker_target=worker_target(),
    )

    assert limited.instructions == (
        "The `jira_*` functions may use only these Jira spaces: ENG. "
        "The `confluence_*` functions may use only these Confluence spaces: DOCS, ~alice."
    )
    assert unrestricted.instructions is None


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"write": False}, atlassian_function_names(write=False)),
        ({"jira_spaces": []}, atlassian_function_names(("confluence",))),
        ({"confluence_spaces": [], "write": False}, atlassian_function_names(("jira",), write=False)),
    ],
)
def test_read_only_and_empty_space_lists_remove_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    """write: false offers only reads, and an empty space list offers none of that product's functions."""
    tool, _gateway = _connected(tmp_path, monkeypatch, **options)

    assert tuple(toolkit_function_names(tool)) == expected


@pytest.mark.asyncio
async def test_jira_search_returns_only_issues_in_allowed_spaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JQL is narrowed to the allowed spaces, and issues the query still let through are dropped."""
    tool, gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"])
    gateway.route(
        "POST",
        gateway_url("jira", "/rest/api/3/search/jql"),
        {"issues": [_issue("ENG-1", "ENG"), _issue("OPS-1", "OPS"), {"key": "X-1"}], "isLast": True},
    )

    result = await _call(tool, "jira_search_issues", jql="status = Done ORDER BY updated DESC", fields=["summary"])
    everything = await _call(tool, "jira_search_issues", jql="status = Done", fields=["*all"])

    searched, all_fields = gateway.product_requests()
    assert json_body(searched) == {
        "jql": 'project in ("ENG") AND (status = Done) ORDER BY updated DESC',
        "maxResults": 20,
        "fields": ["summary", "project"],
    }
    assert json_body(all_fields)["fields"] == ["*all"]
    # The space is returned only when the model asked for it.
    assert result["issues"] == [
        {"key": "ENG-1", "id": "1", "url": f"{SITE_URL}/browse/ENG-1", "fields": {"summary": "Work"}},
    ]
    assert everything["issues"][0]["fields"]["project"] == {"key": "ENG"}


@pytest.mark.asyncio
async def test_jira_issue_read_returns_only_issues_in_allowed_spaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issue read returns an issue in an allowed space and nothing of an issue in another space."""
    tool, gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"])
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/OPS-1"), _issue("OPS-1", "OPS"))
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/ENG-1"), _issue("ENG-1", "eng"))

    allowed = await _call(tool, "jira_get_issue", issue_key="ENG-1", fields=["summary"])
    result = await _call(tool, "jira_get_issue", issue_key="OPS-1", fields=["summary"])

    assert gateway.product_requests()[0].url.params["fields"] == "summary,project"
    assert allowed["issue"]["fields"] == {"summary": "Work"}
    assert result == {
        "tool": "atlassian",
        "status": "error",
        "code": "space_not_allowed",
        "message": "This agent may use only these Jira spaces: ENG.",
        "allowed_spaces": ["ENG"],
    }


@pytest.mark.asyncio
async def test_jira_create_outside_allowed_spaces_sends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating an issue in another space is refused before any Atlassian call."""
    tool, gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"])

    gateway.route("POST", gateway_url("jira", "/rest/api/3/issue"), {"id": "1", "key": "ENG-2"})

    result = await _call(tool, "jira_create_issue", project_key="OPS", summary="New")
    assert gateway.requests == []
    # Extra fields cannot move the new issue into another space.
    created = await _call(
        tool,
        "jira_create_issue",
        project_key="ENG",
        summary="New",
        fields={"project": {"key": "OPS"}},
    )

    assert result["code"] == "space_not_allowed"
    assert created["status"] == "ok"
    assert json_body(gateway.product_requests()[0])["fields"]["project"] == {"key": "ENG"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function_name", "kwargs", "write_method", "write_path"),
    [
        ("jira_update_issue", {"summary": "Renamed"}, "PUT", "/rest/api/3/issue/ENG-1"),
        ("jira_add_comment", {"comment": "Done."}, "POST", "/rest/api/3/issue/ENG-1/comment"),
        ("jira_transition_issue", {"transition": "31"}, "POST", "/rest/api/3/issue/ENG-1/transitions"),
    ],
)
@pytest.mark.parametrize("project", ["ENG", "OPS"])
async def test_jira_writes_check_the_issues_space_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    kwargs: dict[str, object],
    write_method: str,
    write_path: str,
    project: str,
) -> None:
    """A write reads the issue's current space and changes nothing when it is not allowed."""
    tool, gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"])
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/ENG-1"), _issue("ENG-1", project))
    transitions = {"transitions": [{"id": "31", "name": "Start", "to": {"name": "In Progress"}}]}
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/ENG-1/transitions"), transitions)
    gateway.route(write_method, gateway_url("jira", write_path), {"id": "9"})

    result = await _call(tool, function_name, issue_key="ENG-1", **kwargs)

    lookup = gateway.product_requests()[0]
    assert (lookup.method, lookup.url.path, lookup.url.params["fields"]) == (
        "GET",
        f"/ex/jira/{site()['id']}/rest/api/3/issue/ENG-1",
        "project",
    )
    writes = [request for request in gateway.product_requests() if request.method == write_method]
    if project == "ENG":
        assert result["status"] == "ok"
        assert len(writes) == 1
    else:
        assert result["code"] == "space_not_allowed"
        assert writes == []


@pytest.mark.asyncio
async def test_confluence_search_returns_only_results_in_allowed_spaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CQL is narrowed to the allowed spaces, and results the query still let through are dropped."""
    tool, gateway = _connected(tmp_path, monkeypatch, confluence_spaces=["DOCS"])
    gateway.route(
        "GET",
        gateway_url("confluence", "/wiki/rest/api/search"),
        {
            "results": [
                {"content": {"id": "1", "type": "page", "space": {"key": "docs"}}, "title": "Allowed"},
                {"content": {"id": "2", "type": "page", "space": {"key": "OPS"}}, "title": "Other"},
                {"space": {"key": "DOCS"}, "entityType": "space", "title": "Docs space"},
                {"user": {"accountId": "557058:alice"}, "entityType": "user", "title": "Alice"},
            ],
            "_links": {"base": f"{SITE_URL}/wiki"},
        },
    )

    result = await _call(tool, "confluence_search", cql="type = page")

    params = gateway.product_requests()[0].url.params
    assert params["cql"] == 'space in ("DOCS") AND (type = page)'
    assert params["expand"] == "content.history,content.history.ownedBy,content.version,content.space"
    assert [item["title"] for item in result["results"]] == ["Allowed", "Docs space"]


@pytest.mark.asyncio
async def test_confluence_page_outside_allowed_spaces_is_not_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page read returns nothing of a page in another space."""
    tool, gateway = _connected(tmp_path, monkeypatch, confluence_spaces=["DOCS"])
    page = _page_in("OPS")
    page["results"][0]["body"] = {"storage": {"value": "<p>Secret</p>"}}  # type: ignore[index]
    gateway.route("GET", CONTENT_SEARCH, page)

    result = await _call(tool, "confluence_get_page", page_id="123")

    assert result["code"] == "space_not_allowed"
    assert "Secret" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function_name", "kwargs", "next_path"),
    [
        ("confluence_list_attachments", {}, "/wiki/rest/api/content/search"),
        (
            "confluence_download_attachment",
            {"attachment_id": "att456"},
            "/wiki/rest/api/content/123/child/attachment/att456/download",
        ),
        (
            "confluence_update_page",
            {"title": "T", "body_storage": "<p>x</p>", "version_number": 2},
            "/wiki/api/v2/pages/123",
        ),
        ("confluence_add_comment", {"body_storage": "<p>x</p>"}, "/wiki/api/v2/footer-comments"),
    ],
)
@pytest.mark.parametrize("space_key", ["DOCS", "OPS"])
async def test_confluence_page_functions_check_the_pages_space_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    kwargs: dict[str, object],
    next_path: str,
    space_key: str,
) -> None:
    """Listing, downloading, updating, and commenting read the page's space and go on only when it is allowed."""
    tool, gateway = _connected(tmp_path, monkeypatch, confluence_spaces=["DOCS"])
    lookups = iter([_page_in(space_key), {"results": []}])
    gateway.route("GET", CONTENT_SEARCH, lambda _request: httpx.Response(200, json=next(lookups)))

    with tool_runtime_context(tool_context(runtime_paths(tmp_path), tmp_path / "storage")):
        result = await _call(tool, function_name, page_id="123", **kwargs)

    lookup, *rest = gateway.product_requests()
    assert (lookup.url.params["cql"], lookup.url.params["expand"]) == ("id = 123", "space")
    if space_key == "DOCS":
        assert [request.url.path.split(site()["id"])[1] for request in rest] == [next_path]
    else:
        assert result["code"] == "space_not_allowed"
        assert rest == []


@pytest.mark.asyncio
async def test_confluence_writes_in_allowed_spaces_go_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page in an allowed space is updated and commented on after its space is read."""
    tool, gateway = _connected(tmp_path, monkeypatch, confluence_spaces=["DOCS"])
    gateway.route("GET", CONTENT_SEARCH, _page_in("DOCS"))
    gateway.route("PUT", gateway_url("confluence", "/wiki/api/v2/pages/123"), {"id": "123", "version": {"number": 2}})
    gateway.route("POST", gateway_url("confluence", "/wiki/api/v2/footer-comments"), {"id": "77"})

    updated = await _call(
        tool,
        "confluence_update_page",
        page_id="123",
        title="T",
        body_storage="<p>x</p>",
        version_number=2,
    )
    commented = await _call(tool, "confluence_add_comment", page_id="123", body_storage="<p>x</p>")

    assert (updated["status"], commented["status"]) == ("ok", "ok")
    assert [request.method for request in gateway.product_requests()] == ["GET", "PUT", "GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("space_key", "parent_space", "created"),
    [("OPS", None, False), ("DOCS", "OPS", False), ("DOCS", "DOCS", True), ("docs", None, True)],
)
async def test_confluence_create_page_checks_its_space_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    space_key: str,
    parent_space: str | None,
    created: bool,
) -> None:
    """A page is created only in an allowed space, under a parent page in an allowed space."""
    tool, gateway = _connected(tmp_path, monkeypatch, confluence_spaces=["DOCS"])
    gateway.route("GET", CONTENT_SEARCH, _page_in(parent_space or "DOCS"))
    gateway.route(
        "GET",
        gateway_url("confluence", "/wiki/api/v2/spaces"),
        lambda _request: httpx.Response(200, json={"results": [{"id": 98765, "key": "DOCS"}]}),
    )
    gateway.route("POST", gateway_url("confluence", "/wiki/api/v2/pages"), {"id": "555", "title": "Notes"})

    result = await _call(
        tool,
        "confluence_create_page",
        space_key=space_key,
        title="Notes",
        body_storage="<p>Hi</p>",
        parent_page_id="12" if parent_space else None,
    )

    posts = [request for request in gateway.product_requests() if request.method == "POST"]
    assert (result["status"], len(posts)) == (("ok", 1) if created else ("error", 0))
    if not created:
        assert result["code"] == "space_not_allowed"


# One call per function, each naming an item in a space the agent may not use.
_OUTSIDE_CALLS: dict[str, dict[str, object]] = {
    "jira_search_issues": {"jql": "status = Done"},
    "jira_get_issue": {"issue_key": "OPS-1"},
    "jira_create_issue": {"project_key": "OPS", "summary": "New"},
    "jira_update_issue": {"issue_key": "OPS-1", "summary": "Renamed"},
    "jira_add_comment": {"issue_key": "OPS-1", "comment": "Done."},
    "jira_transition_issue": {"issue_key": "OPS-1", "transition": "31"},
    "confluence_search": {"cql": "type = page"},
    "confluence_get_page": {"page_id": "123"},
    "confluence_list_attachments": {"page_id": "123"},
    "confluence_download_attachment": {"page_id": "123", "attachment_id": "att456"},
    "confluence_create_page": {"space_key": "OPS", "title": "T", "body_storage": "<p>x</p>", "parent_page_id": "12"},
    "confluence_update_page": {"page_id": "123", "title": "T", "body_storage": "<p>x</p>", "version_number": 2},
    "confluence_add_comment": {"page_id": "123", "body_storage": "<p>x</p>"},
}


def test_every_function_has_an_outside_space_call() -> None:
    """A new function fails here until the outside-space check below covers it."""
    assert set(_OUTSIDE_CALLS) == set(atlassian_function_names())


@pytest.mark.asyncio
@pytest.mark.parametrize("function_name", sorted(_OUTSIDE_CALLS))
async def test_no_function_reaches_outside_the_allowed_spaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
) -> None:
    """Every function refuses, or returns nothing, for items Atlassian reports in another space, and only looks them up."""
    tool, gateway = _connected(tmp_path, monkeypatch, jira_spaces=["ENG"], confluence_spaces=["DOCS"])
    lookups = {
        ("POST", gateway_url("jira", "/rest/api/3/search/jql")): {"issues": [_issue("OPS-1", "OPS")], "isLast": True},
        ("GET", gateway_url("jira", "/rest/api/3/issue/OPS-1")): _issue("OPS-1", "OPS"),
        ("GET", gateway_url("confluence", "/wiki/rest/api/search")): {
            "results": [{"content": {"id": "123", "type": "page", "space": {"key": "OPS"}}, "title": "Other"}],
        },
        ("GET", CONTENT_SEARCH): _page_in("OPS"),
    }
    for (method, url), body in lookups.items():
        gateway.route(method, url, body)

    with tool_runtime_context(tool_context(runtime_paths(tmp_path), tmp_path / "storage")):
        result = await _call(tool, function_name, **_OUTSIDE_CALLS[function_name])

    if function_name.endswith(("_search", "_search_issues")):
        assert (result["status"], result.get("issues", result.get("results"))) == ("ok", [])
    else:
        assert result["code"] == "space_not_allowed"
    requests = {(request.method, str(request.url.copy_with(query=None))) for request in gateway.product_requests()}
    assert requests <= set(lookups)
