"""Behavior of the native Atlassian Cloud toolkit against a mocked Atlassian gateway."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom.credentials import CredentialsManager
from mindroom.custom_tools import atlassian_client
from mindroom.custom_tools.atlassian import AtlassianTools
from mindroom.oauth.atlassian import atlassian_function_names, atlassian_oauth_provider
from mindroom.oauth.credential_lifecycle import load_oauth_credentials_snapshot_sync, resolve_oauth_credential_context
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import tool_execution_identity
from tests.atlassian_test_support import (
    ALICE,
    BOB,
    CLOUD_ID,
    OTHER_CLOUD_ID,
    OTHER_SITE_URL,
    SITE_URL,
    FakeGateway,
    bearer,
    execution_identity,
    gateway_url,
    json_body,
    publish_grant,
    runtime_paths,
    save_client_config,
    site,
    worker_target,
)
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

TOKEN = "alice-access-token"  # noqa: S105


def _tool(
    paths: RuntimePaths,
    manager: CredentialsManager,
    *,
    requester_id: str | None = ALICE,
    site_url: str | None = None,
    cloud_id: str | None = None,
) -> AtlassianTools:
    return AtlassianTools(
        site_url=site_url,
        cloud_id=cloud_id,
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(requester_id) if requester_id else None,
    )


def _connected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AtlassianTools, FakeGateway]:
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, TOKEN)
    gateway = FakeGateway(sites_by_token={TOKEN: [site()]}).install(monkeypatch)
    return _tool(paths, manager), gateway


def test_registered_metadata_matches_the_toolkit() -> None:
    """The catalog advertises exactly the functions the toolkit registers, as a primary-runtime OAuth tool."""
    metadata = TOOL_METADATA["atlassian"]

    assert metadata.function_names == atlassian_function_names()
    assert metadata.auth_provider == "atlassian"
    assert metadata.requires_primary_runtime is True
    assert metadata.file_access is ToolFileAccess.NONE
    assert {field.name for field in metadata.config_fields or []} == {"site_url", "cloud_id"}


def test_toolkit_registers_every_function_under_its_catalog_name(tmp_path: Path) -> None:
    """Jira and Confluence functions are async and keep names that cannot collide with the API-token tools."""
    paths = runtime_paths(tmp_path)
    tool = _tool(paths, save_client_config(paths))

    assert tuple(tool.async_functions) == atlassian_function_names()
    assert not set(tool.async_functions) & set(TOOL_METADATA["jira"].function_names)
    assert not set(tool.async_functions) & set(TOOL_METADATA["confluence"].function_names)


@pytest.mark.asyncio
async def test_catalog_construction_applies_site_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An authored site_url override reaches the toolkit through the normal tool registry."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, TOKEN)
    gateway = FakeGateway(
        sites_by_token={TOKEN: [site(), site(OTHER_CLOUD_ID, OTHER_SITE_URL, name="acme")]},
    ).install(monkeypatch)
    gateway.route("POST", gateway_url("jira", "/rest/api/3/search/jql", OTHER_CLOUD_ID), {"issues": [], "isLast": True})
    tool = get_tool_by_name(
        "atlassian",
        paths,
        credentials_manager=manager,
        tool_config_overrides={"site_url": f"{OTHER_SITE_URL}/wiki"},
        worker_target=worker_target(),
        disable_sandbox_proxy=True,
    )

    entrypoint = tool.async_functions["jira_search_issues"].entrypoint
    assert entrypoint is not None
    result = json.loads(await entrypoint(jql="project = PROJ"))

    assert result["status"] == "ok"
    assert result["site"]["cloud_id"] == OTHER_CLOUD_ID


@pytest.mark.parametrize(("site_url", "cloud_id"), [("http://example.atlassian.net", None), (None, "not-a-uuid")])
def test_invalid_site_pin_fails_at_construction(tmp_path: Path, site_url: str | None, cloud_id: str | None) -> None:
    """A site pin that could redirect credentials or alter gateway paths is rejected before any call."""
    paths = runtime_paths(tmp_path)
    with pytest.raises(ValueError, match=r"site_url|cloud_id"):
        _tool(paths, save_client_config(paths), site_url=site_url, cloud_id=cloud_id)


@pytest.mark.asyncio
async def test_missing_connection_returns_requester_bound_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each requester gets their own connect link, and nothing reaches Atlassian before they connect."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    gateway = FakeGateway().install(monkeypatch)

    alice = json.loads(await _tool(paths, manager).jira_search_issues(jql="project = PROJ"))
    bob = json.loads(await _tool(paths, manager, requester_id=BOB).confluence_search(cql="type = page"))

    assert alice["status"] == "error"
    assert alice["oauth_connection_required"] is True
    assert alice["provider"] == "atlassian"
    assert "/api/oauth/atlassian/authorize?connect_token=" in alice["connect_url"]
    assert bob["connect_url"] != alice["connect_url"]
    assert ALICE not in json.dumps(alice)
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_no_requester_never_uses_shared_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials follow the requester, so an unscoped grant is never used for a call without one."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    provider = atlassian_oauth_provider()
    publish_oauth_credentials(
        provider,
        {"token": TOKEN, "client_id": "atlassian-client", "scopes": list(provider.scopes)},
        credentials_manager=manager,
        worker_target=None,
    )
    gateway = FakeGateway(sites_by_token={TOKEN: [site()]}).install(monkeypatch)

    result = json.loads(await _tool(paths, manager, requester_id=None).jira_get_issue(issue_key="PROJ-1"))

    assert result["oauth_connection_required"] is True
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_grant_missing_a_requested_scope_must_reconnect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored grant lacking any requested scope counts as disconnected before any Atlassian call."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    provider = atlassian_oauth_provider()
    publish_grant(provider, manager, TOKEN, scopes=[scope for scope in provider.scopes if scope != "write:jira-work"])
    gateway = FakeGateway(sites_by_token={TOKEN: [site()]}).install(monkeypatch)

    result = json.loads(await _tool(paths, manager).jira_search_issues(jql="project = PROJ"))

    assert result["oauth_connection_required"] is True
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed_through_the_atlassian_token_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh uses the scoped OAuth lifecycle, and the call carries the rotated access token."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    provider = atlassian_oauth_provider()
    publish_grant(provider, manager, "expired-token", expires_at=1.0)
    token_requests: list[httpx.Request] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        token_requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-token",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "scope": " ".join(provider.scopes),
                "token_type": "Bearer",
            },
        )

    def oauth_client(**kwargs: object) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(transport=httpx.MockTransport(token_endpoint), **kwargs)

    monkeypatch.setattr("mindroom.oauth.providers.AsyncOAuth2Client", oauth_client)
    gateway = FakeGateway(sites_by_token={"rotated-token": [site()]}).install(monkeypatch)
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/PROJ-1"), {"key": "PROJ-1", "id": "10001"})

    result = json.loads(await _tool(paths, manager).jira_get_issue(issue_key="PROJ-1"))

    assert result["status"] == "ok"
    assert [str(request.url) for request in token_requests] == ["https://auth.atlassian.com/oauth/token"]
    assert parse_qs(token_requests[0].content.decode())["refresh_token"] == ["expired-token-refresh"]
    assert {bearer(request) for request in gateway.requests} == {"rotated-token"}
    stored = load_oauth_credentials_snapshot_sync(
        resolve_oauth_credential_context(provider, paths, manager, worker_target()),
    ).credentials
    assert stored is not None
    assert (stored["token"], stored["refresh_token"]) == ("rotated-token", "rotated-refresh")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error", "expected_reason"),
    [(400, "invalid_grant", "refresh_rejected"), (503, "temporarily_unavailable", None)],
)
async def test_refresh_failures_ask_to_reconnect_or_to_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    error: str,
    expected_reason: str | None,
) -> None:
    """A rejected refresh asks the requester to reconnect, while a provider outage asks for a retry."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, "expired-token", expires_at=1.0)

    def oauth_client(**kwargs: object) -> AsyncOAuth2Client:
        response = httpx.Response(status_code, json={"error": error, "error_description": "secret-detail"})
        return AsyncOAuth2Client(transport=httpx.MockTransport(lambda _request: response), **kwargs)

    monkeypatch.setattr("mindroom.oauth.providers.AsyncOAuth2Client", oauth_client)
    gateway = FakeGateway().install(monkeypatch)

    result = json.loads(await _tool(paths, manager).jira_get_issue(issue_key="PROJ-1"))

    if expected_reason is None:
        assert result["code"] == "oauth_refresh_failed"
    else:
        assert result["oauth_connection_required"] is True
        assert result["reason"] == expected_reason
    assert "secret-detail" not in json.dumps(result)
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_unreadable_grant_asks_for_a_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant that cannot be decrypted points the requester at a reset instead of a new login."""
    active_key = base64.urlsafe_b64encode(b"a" * 32).decode()
    paths = runtime_paths(tmp_path, {"MINDROOM_CREDENTIALS_ENCRYPTION_KEY": active_key})
    manager = save_client_config(paths)
    wrong_key_manager = CredentialsManager(
        manager.base_path,
        shared_base_path=manager.shared_base_path,
        encryption_key=base64.urlsafe_b64encode(b"b" * 32).decode(),
    )
    publish_grant(atlassian_oauth_provider(), wrong_key_manager, TOKEN)
    gateway = FakeGateway(sites_by_token={TOKEN: [site()]}).install(monkeypatch)

    result = json.loads(await _tool(paths, manager).jira_get_issue(issue_key="PROJ-1"))

    assert result["oauth_connection_required"] is True
    assert result["reset_required"] is True
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_calls_follow_the_active_requester_not_the_constructing_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A toolkit built for one requester never lends that requester's grant to another active requester."""
    tool, gateway = _connected(tmp_path, monkeypatch)

    with tool_execution_identity(execution_identity(BOB)):
        result = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))

    assert result["oauth_connection_required"] is True
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_jira_search_uses_enhanced_jql_search_and_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Searches go to the current JQL endpoint on the resolved site and report the next page token."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "POST",
        gateway_url("jira", "/rest/api/3/search/jql"),
        {
            "issues": [{"id": "10001", "key": "PROJ-1", "self": "https://example/rest", "fields": {"summary": "Fix"}}],
            "nextPageToken": "page+2==",
            "isLast": False,
        },
    )

    result = json.loads(
        await tool.jira_search_issues(
            jql="project = PROJ",
            max_results=500,
            fields=["summary"],
            next_page_token="p1",  # noqa: S106
        ),
    )

    request = gateway.product_requests()[0]
    assert json_body(request) == {
        "jql": "project = PROJ",
        "maxResults": 50,
        "fields": ["summary"],
        "nextPageToken": "p1",
    }
    assert bearer(request) == TOKEN
    assert result["issues"] == [
        {"key": "PROJ-1", "id": "10001", "url": f"{SITE_URL}/browse/PROJ-1", "fields": {"summary": "Fix"}},
    ]
    assert result["has_more"] is True
    assert result["next_page_token"] == "page+2=="  # noqa: S105


@pytest.mark.asyncio
async def test_jira_get_issue_lists_available_transitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue reads expand transitions so the model can pick a workflow step."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("jira", "/rest/api/3/issue/PROJ-7"),
        {
            "id": "10007",
            "key": "PROJ-7",
            "fields": {"summary": "Ship"},
            "transitions": [{"id": "31", "name": "Start", "to": {"name": "In Progress"}}],
        },
    )

    result = json.loads(await tool.jira_get_issue(issue_key="proj-7", fields=["summary", "status"]))

    assert gateway.product_requests()[0].url.params["expand"] == "transitions"
    assert gateway.product_requests()[0].url.params["fields"] == "summary,status"
    assert result["issue"]["transitions"] == [{"id": "31", "name": "Start", "to_status": "In Progress"}]


@pytest.mark.asyncio
async def test_jira_create_issue_sends_description_as_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-text descriptions become Atlassian Document Format paragraphs."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route("POST", gateway_url("jira", "/rest/api/3/issue"), {"id": "10002", "key": "PROJ-2"})

    result = json.loads(
        await tool.jira_create_issue(
            project_key="proj",
            summary="New dashboard",
            issue_type="Story",
            description="First line\nsecond line\n\nNext paragraph",
            fields={"labels": ["ui"], "summary": "ignored"},
        ),
    )

    assert json_body(gateway.product_requests()[0])["fields"] == {
        "labels": ["ui"],
        "project": {"key": "PROJ"},
        "summary": "New dashboard",
        "issuetype": {"name": "Story"},
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "First line"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "second line"},
                    ],
                },
                {"type": "paragraph", "content": [{"type": "text", "text": "Next paragraph"}]},
            ],
        },
    }
    assert result["issue"] == {"key": "PROJ-2", "id": "10002", "url": f"{SITE_URL}/browse/PROJ-2"}


@pytest.mark.asyncio
async def test_jira_update_and_comment_write_the_requested_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates send only supplied fields, and comments are documents."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route("PUT", gateway_url("jira", "/rest/api/3/issue/PROJ-3"))
    gateway.route("POST", gateway_url("jira", "/rest/api/3/issue/PROJ-3/comment"), {"id": "900"})

    updated = json.loads(await tool.jira_update_issue(issue_key="PROJ-3", summary="Renamed", fields={"labels": []}))
    commented = json.loads(await tool.jira_add_comment(issue_key="PROJ-3", comment="Done."))
    nothing = json.loads(await tool.jira_update_issue(issue_key="PROJ-3"))

    update_request, comment_request = gateway.product_requests()
    assert json_body(update_request) == {"fields": {"labels": [], "summary": "Renamed"}}
    assert updated["updated_fields"] == ["labels", "summary"]
    assert json_body(comment_request)["body"]["content"][0]["content"] == [{"type": "text", "text": "Done."}]
    assert commented["comment"] == {"id": "900", "issue_key": "PROJ-3"}
    assert nothing["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_jira_transition_accepts_a_name_and_rejects_unavailable_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transition name resolves to its ID, and an unknown one never posts."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    transitions = gateway_url("jira", "/rest/api/3/issue/PROJ-4/transitions")
    posted: list[object] = []

    def transitions_endpoint(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json_body(request))
            return httpx.Response(204)
        return httpx.Response(200, json={"transitions": [{"id": "41", "name": "Close", "to": {"name": "Done"}}]})

    gateway.route("GET", transitions, transitions_endpoint)
    gateway.route("POST", transitions, transitions_endpoint)

    moved = json.loads(await tool.jira_transition_issue(issue_key="PROJ-4", transition="done"))
    rejected = json.loads(await tool.jira_transition_issue(issue_key="PROJ-4", transition="Reopen"))

    assert posted == [{"transition": {"id": "41"}}]
    assert moved["transition"] == {"id": "41", "name": "Close", "to_status": "Done"}
    assert rejected["code"] == "transition_not_available"
    assert rejected["available_transitions"] == [{"id": "41", "name": "Close", "to_status": "Done"}]


@pytest.mark.asyncio
async def test_confluence_search_round_trips_encoded_cursors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cursor is a percent-encoded URL component, so a literal plus survives the round trip."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("confluence", "/wiki/rest/api/search"),
        {
            "results": [
                {
                    "content": {"id": "123", "type": "page"},
                    "title": "Release plan",
                    "excerpt": "Plan",
                    "url": "/spaces/DOCS/pages/123",
                    "resultGlobalContainer": {"title": "Docs"},
                    "lastModified": "2026-01-01T00:00:00Z",
                },
            ],
            "_links": {
                "base": f"{SITE_URL}/wiki",
                "next": "/rest/api/search?cql=type%3Dpage&limit=10&cursor=a%2Bb%3D%3D+c",
            },
        },
    )

    first = json.loads(await tool.confluence_search(cql="type = page"))
    await tool.confluence_search(cql="type = page", cursor=first["next_cursor"])

    assert first["results"] == [
        {
            "id": "123",
            "type": "page",
            "title": "Release plan",
            "excerpt": "Plan",
            "space": "Docs",
            "last_modified": "2026-01-01T00:00:00Z",
            "url": f"{SITE_URL}/wiki/spaces/DOCS/pages/123",
        },
    ]
    assert first["has_more"] is True
    assert first["next_cursor"] == "a+b==+c"
    assert gateway.product_requests()[1].url.params["cursor"] == "a+b==+c"
    assert gateway.product_requests()[1].url.params["next"] == "true"


@pytest.mark.asyncio
async def test_confluence_get_page_reads_storage_body_through_cql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page reads use CQL content search with the classic read scopes."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("confluence", "/wiki/rest/api/content/search"),
        {
            "results": [
                {
                    "id": "123",
                    "title": "Runbook",
                    "space": {"key": "OPS", "name": "Operations"},
                    "version": {"number": 7},
                    "body": {"storage": {"value": "<p>Steps</p>"}},
                    "_links": {"webui": "/spaces/OPS/pages/123/Runbook"},
                },
            ],
            "_links": {"base": f"{SITE_URL}/wiki"},
        },
    )

    result = json.loads(await tool.confluence_get_page(page_id="123"))

    params = gateway.product_requests()[0].url.params
    assert params["cql"] == "id = 123 AND type = page"
    assert params["expand"] == "body.storage,version,space"
    assert result["page"] == {
        "id": "123",
        "title": "Runbook",
        "space_key": "OPS",
        "space_name": "Operations",
        "version": 7,
        "body_storage": "<p>Steps</p>",
        "url": f"{SITE_URL}/wiki/spaces/OPS/pages/123/Runbook",
    }


@pytest.mark.asyncio
async def test_confluence_create_page_resolves_space_then_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writes use the v2 API, and an unknown space key never creates anything."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    spaces = gateway_url("confluence", "/wiki/api/v2/spaces")

    def spaces_endpoint(request: httpx.Request) -> httpx.Response:
        key = request.url.params["keys"]
        return httpx.Response(200, json={"results": [{"id": 98765, "key": "DOCS"}] if key == "DOCS" else []})

    gateway.route("GET", spaces, spaces_endpoint)
    gateway.route(
        "POST",
        gateway_url("confluence", "/wiki/api/v2/pages"),
        {
            "id": "555",
            "title": "Notes",
            "version": {"number": 1},
            "_links": {"base": f"{SITE_URL}/wiki", "webui": "/x"},
        },
    )

    created = json.loads(
        await tool.confluence_create_page(
            space_key="DOCS",
            title="Notes",
            body_storage="<p>Hi</p>",
            parent_page_id="12",
        ),
    )
    missing = json.loads(await tool.confluence_create_page(space_key="NOPE", title="Notes", body_storage="<p>Hi</p>"))

    posts = [request for request in gateway.product_requests() if request.method == "POST"]
    assert [json_body(request) for request in posts] == [
        {
            "status": "current",
            "title": "Notes",
            "body": {"representation": "storage", "value": "<p>Hi</p>"},
            "parentId": "12",
            "spaceId": "98765",
        },
    ]
    assert created["page"] == {"id": "555", "title": "Notes", "version": 1, "url": f"{SITE_URL}/wiki/x"}
    assert missing["code"] == "space_not_found"


@pytest.mark.asyncio
async def test_confluence_update_and_comment_use_v2_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Updates carry the new version number, and comments are footer comments on the page."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route("PUT", gateway_url("confluence", "/wiki/api/v2/pages/123"), {"id": "123", "version": {"number": 8}})
    gateway.route("POST", gateway_url("confluence", "/wiki/api/v2/footer-comments"), {"id": "77"})

    updated = json.loads(
        await tool.confluence_update_page(
            page_id="123",
            title="Runbook",
            body_storage="<p>New</p>",
            version_number=8,
            version_message="Refresh steps",
        ),
    )
    commented = json.loads(await tool.confluence_add_comment(page_id="123", body_storage="<p>Thanks</p>"))
    stale = json.loads(
        await tool.confluence_update_page(page_id="123", title="Runbook", body_storage="<p>x</p>", version_number=1),
    )

    update_request, comment_request = gateway.product_requests()
    assert json_body(update_request) == {
        "id": "123",
        "status": "current",
        "title": "Runbook",
        "body": {"representation": "storage", "value": "<p>New</p>"},
        "version": {"number": 8, "message": "Refresh steps"},
    }
    assert json_body(comment_request) == {
        "pageId": "123",
        "body": {"representation": "storage", "value": "<p>Thanks</p>"},
    }
    assert updated["page"]["version"] == 8
    assert commented["comment"] == {"id": "77", "page_id": "123"}
    assert stale["code"] == "invalid_argument"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function_name", "kwargs"),
    [
        ("jira_get_issue", {"issue_key": "../../rest/api/3/myself"}),
        ("jira_get_issue", {"issue_key": "PROJ-1?expand=x"}),
        ("jira_add_comment", {"issue_key": "PROJ-1/../2", "comment": "x"}),
        ("jira_create_issue", {"project_key": "PROJ/1", "summary": "x"}),
        ("confluence_get_page", {"page_id": "123/../456"}),
        ("confluence_get_page", {"page_id": "https://example.atlassian.net/wiki/pages/1"}),
        ("confluence_get_page", {"page_id": "１２３"}),  # noqa: RUF001
        (
            "confluence_create_page",
            {"space_key": "DOCS", "title": "t", "body_storage": "b", "parent_page_id": "1 OR 1"},
        ),
        ("confluence_search", {"cql": "type = page", "cursor": "abc\ndef"}),
        ("jira_search_issues", {"jql": " "}),
    ],
)
async def test_unsafe_arguments_are_rejected_before_any_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    kwargs: dict[str, object],
) -> None:
    """IDs and cursors that could alter a gateway path or query are refused before authentication."""
    tool, gateway = _connected(tmp_path, monkeypatch)

    result = json.loads(await getattr(tool, function_name)(**kwargs))

    assert result["code"] == "invalid_argument"
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_site_pins_select_one_site_and_never_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned site must be reachable for the product; otherwise no product call is made."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, TOKEN)
    gateway = FakeGateway(
        sites_by_token={
            TOKEN: [
                site(),
                site(OTHER_CLOUD_ID, OTHER_SITE_URL, scopes=["read:jira-work"], name="acme"),
                {"id": "../../escape", "url": SITE_URL, "scopes": ["search:confluence"]},
            ],
        },
    ).install(monkeypatch)
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/PROJ-1", OTHER_CLOUD_ID), {"key": "PROJ-1"})

    unpinned = json.loads(await _tool(paths, manager).jira_get_issue(issue_key="PROJ-1"))
    by_url = json.loads(await _tool(paths, manager, site_url=OTHER_SITE_URL).jira_get_issue(issue_key="PROJ-1"))
    by_cloud_id = json.loads(
        await _tool(paths, manager, site_url=SITE_URL, cloud_id=OTHER_CLOUD_ID.upper()).jira_get_issue(
            issue_key="PROJ-1",
        ),
    )
    no_confluence = json.loads(
        await _tool(paths, manager, cloud_id=OTHER_CLOUD_ID).confluence_get_page(page_id="1"),
    )

    assert unpinned["code"] == "site_selection_required"
    assert {entry["cloud_id"] for entry in unpinned["available_sites"]} == {CLOUD_ID, OTHER_CLOUD_ID}
    assert by_url["site"]["cloud_id"] == OTHER_CLOUD_ID
    assert by_cloud_id["site"]["cloud_id"] == OTHER_CLOUD_ID
    assert no_confluence["code"] == "site_not_found"
    assert no_confluence["configured_cloud_id"] == OTHER_CLOUD_ID
    assert [entry["cloud_id"] for entry in no_confluence["available_sites"]] == [CLOUD_ID]
    assert [str(request.url.copy_with(query=None)) for request in gateway.product_requests()] == [
        gateway_url("jira", "/rest/api/3/issue/PROJ-1", OTHER_CLOUD_ID),
        gateway_url("jira", "/rest/api/3/issue/PROJ-1", OTHER_CLOUD_ID),
    ]


@pytest.mark.asyncio
async def test_rejected_access_token_returns_reconnect_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway 401 asks the requester to reconnect instead of surfacing the provider response."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("jira", "/rest/api/3/issue/PROJ-1"),
        lambda _request: httpx.Response(401, json={"message": "Unauthorized; scope does not match"}),
    )

    result = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))

    assert result["oauth_connection_required"] is True
    assert result["reason"] == "access_rejected"
    assert "scope does not match" not in json.dumps(result)


@pytest.mark.asyncio
async def test_api_errors_keep_messages_but_drop_bodies_urls_and_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atlassian error messages help the model correct a request, without echoing the raw body or any URL."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "POST",
        gateway_url("jira", "/rest/api/3/issue"),
        lambda _request: httpx.Response(
            400,
            json={
                "errorMessages": ["See https://example.atlassian.net/secret?sig=abc for details"],
                "errors": {"summary": "Summary is required.\u0007"},
                "debug": "internal-body-marker",
            },
        ),
    )

    result = json.loads(await tool.jira_create_issue(project_key="PROJ", summary="x"))

    assert result["code"] == "invalid_request"
    assert result["status_code"] == 400
    assert result["messages"] == ["See <url> for details", "summary: Summary is required."]
    serialized = json.dumps(result)
    assert "internal-body-marker" not in serialized
    assert "sig=abc" not in serialized
    assert TOKEN not in serialized


@pytest.mark.asyncio
async def test_http_client_bounds_each_operation_and_never_follows_redirects() -> None:
    """The production client times out each operation and leaves every redirect to the caller."""
    async with atlassian_client._new_http_client() as client:
        assert client.timeout == httpx.Timeout(20.0)
        assert client.follow_redirects is False


@pytest.mark.asyncio
async def test_api_redirects_are_reported_instead_of_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An API redirect is an error, so the bearer and request never travel to its location."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("jira", "/rest/api/3/issue/PROJ-1"),
        lambda _request: httpx.Response(302, headers={"location": "https://evil.example.com/steal"}),
    )

    result = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))

    assert (result["code"], result["status_code"]) == ("atlassian_error", 302)
    assert [request.url.host for request in gateway.product_requests()] == ["api.atlassian.com"]


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "headers", "body", "expected"),
    [
        (200, {"content-length": "9999"}, b"{}", ("response_too_large", None)),
        (200, {}, b'{"key": "' + b"x" * 2000 + b'"}', ("response_too_large", None)),
        (400, {}, b'{"errorMessages": ["' + b"x" * 2000 + b'"]}', ("invalid_request", [])),
        (400, {}, b'{"errorMessages": ["Bad field"]}', ("invalid_request", ["Bad field"])),
        (200, {}, b'{"key": "PROJ-1"}', ("ok", None)),
    ],
)
async def test_api_response_bodies_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
    expected: tuple[str, list[str] | None],
) -> None:
    """A body over the cap is never buffered whole; a failed call then reports its status without messages."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    # Large enough for site discovery, small enough for the oversized bodies below.
    monkeypatch.setattr(atlassian_client, "_MAX_JSON_RESPONSE_BYTES", 1024)
    gateway.route(
        "GET",
        gateway_url("jira", "/rest/api/3/issue/PROJ-1"),
        lambda _request: httpx.Response(status_code, headers=headers, content=_chunks(body[:600], body[600:])),
    )

    result = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))

    code, messages = expected
    assert len(gateway.product_requests()) == 1
    assert result.get("code", result["status"]) == code
    if code == "response_too_large":
        assert result["max_bytes"] == 1024
    if messages is not None:
        assert (result["status_code"], result["messages"]) == (status_code, messages)


@pytest.mark.asyncio
async def test_api_calls_have_an_overall_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A body that keeps trickling in cannot hold a call open past the deadline."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    monkeypatch.setattr(atlassian_client, "_REQUEST_DEADLINE_SECONDS", 0.05)

    async def trickle() -> AsyncIterator[bytes]:
        yield b"{"
        while True:
            await asyncio.sleep(0.01)
            yield b" "

    gateway.route(
        "GET",
        gateway_url("jira", "/rest/api/3/issue/PROJ-1"),
        lambda _request: httpx.Response(200, content=trickle()),
    )

    result = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))

    assert result["code"] == "request_timeout"


@pytest.mark.asyncio
async def test_transport_errors_report_only_the_failure_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Httpx messages can include URLs, so only the exception type reaches the model."""
    tool, gateway = _connected(tmp_path, monkeypatch)

    def failing(request: httpx.Request) -> httpx.Response:
        msg = f"connection reset while fetching {request.url}"
        raise httpx.ConnectError(msg, request=request)

    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/PROJ-1"), failing)

    result = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))

    assert result["code"] == "request_failed"
    assert result["message"] == "The Atlassian request failed (ConnectError)."
    assert "api.atlassian.com" not in json.dumps(result)


@pytest.mark.asyncio
async def test_jira_search_flags_pages_without_a_usable_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """More issues without a safe next page token are reported as an incomplete result."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route(
        "POST",
        gateway_url("jira", "/rest/api/3/search/jql"),
        {"issues": [], "nextPageToken": "bad token", "isLast": False},
    )

    result = json.loads(await tool.jira_search_issues(jql="project = PROJ"))

    assert (result["has_more"], result["next_page_token"]) == (True, None)
    assert "incomplete" in result["warning"]


@pytest.mark.asyncio
async def test_comment_line_endings_are_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows line endings split paragraphs like plain newlines and never reach Jira as text."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route("POST", gateway_url("jira", "/rest/api/3/issue/PROJ-3/comment"), {"id": "901"})

    await tool.jira_add_comment(issue_key="PROJ-3", comment="First\r\nline\r\n\r\nSecond")

    assert json_body(gateway.product_requests()[0])["body"]["content"] == [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "First"}, {"type": "hardBreak"}, {"type": "text", "text": "line"}],
        },
        {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]},
    ]


@pytest.mark.parametrize("function_name", ["jira_create_issue", "jira_update_issue"])
def test_extra_fields_accept_any_json_value(tmp_path: Path, function_name: str) -> None:
    """The model-facing schema lets custom fields carry lists, strings, and objects."""
    paths = runtime_paths(tmp_path)
    function = _tool(paths, save_client_config(paths)).async_functions[function_name]

    function.process_entrypoint()

    field_schema = function.parameters["properties"]["fields"]["anyOf"][0]
    assert field_schema == {"type": "object", "additionalProperties": True}


@pytest.mark.asyncio
async def test_one_site_listed_per_product_counts_as_one_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate Jira and Confluence entries for the same cloud ID merge instead of asking for a site pin."""
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, TOKEN)
    gateway = FakeGateway(
        sites_by_token={TOKEN: [site(scopes=["read:jira-work"]), site(scopes=["search:confluence"])]},
    ).install(monkeypatch)
    gateway.route("GET", gateway_url("jira", "/rest/api/3/issue/PROJ-1"), {"key": "PROJ-1"})
    gateway.route("GET", gateway_url("confluence", "/wiki/rest/api/search"), {"results": []})
    tool = _tool(paths, manager)

    issue = json.loads(await tool.jira_get_issue(issue_key="PROJ-1"))
    search = json.loads(await tool.confluence_search(cql="type = page"))

    assert issue["status"] == search["status"] == "ok"


@pytest.mark.asyncio
async def test_missing_page_is_reported_without_guessing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page that search cannot see is reported as not found or not viewable."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", "/wiki/rest/api/content/search"), {"results": []})

    result = json.loads(await tool.confluence_get_page(page_id="404"))

    assert result["code"] == "page_not_found"


@pytest.mark.asyncio
async def test_numeric_issue_ids_get_no_browse_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Browse links need an issue key, so a write addressed by numeric ID reports no URL."""
    tool, gateway = _connected(tmp_path, monkeypatch)
    gateway.route("PUT", gateway_url("jira", "/rest/api/3/issue/10001"))

    result = json.loads(await tool.jira_update_issue(issue_key="10001", summary="Renamed"))

    assert result["issue"] == {"key": "10001", "id": None, "url": None}
