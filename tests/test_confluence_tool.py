"""Confluence toolkit behavior on Cloud and Server URLs against a faked Confluence v1 REST API."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from mindroom.tools.confluence import confluence_tools

_SPACES = {"results": [{"id": 1, "key": "ENG", "name": "Engineering"}], "start": 0, "limit": 50, "size": 1}
_PAGE = {"id": "42", "type": "page", "title": "Runbook", "body": {"storage": {"value": "<p>Old</p>"}}}


@dataclass
class _Request:
    method: str
    path: str
    query: dict[str, list[str]]
    body: object


@dataclass
class _ConfluenceServer:
    """Answer the Confluence v1 REST endpoints the toolkit reaches below one REST base path."""

    base_path: str
    requests: list[_Request] = field(default_factory=list)

    def answer(self, request: _Request) -> tuple[int, object]:
        path = request.path.removeprefix(self.base_path)
        title = request.query.get("title", [""])[0]
        if (request.method, path) == ("GET", "/rest/api/content") and "title" not in request.query:
            start = int(request.query.get("start", ["0"])[0])
            batch = [{"id": str(number), "title": f"Page {number}"} for number in range(start, min(start + 50, 51))]
            return 200, {"results": batch}
        routes: dict[tuple[str, str], object] = {
            ("GET", "/rest/api/space"): _SPACES,
            ("GET", "/rest/api/content"): {"results": [_PAGE] if title in {"", "Runbook"} else []},
            ("POST", "/rest/api/content"): {"id": "77", "type": "page", "title": "New page"},
            ("GET", "/rest/api/content/42"): _PAGE,
            ("GET", "/rest/api/content/42/history"): {"lastUpdated": {"number": 4}},
            ("PUT", "/rest/api/content/42"): {"id": "42", "type": "page", "title": "Runbook v2"},
        }
        if (request.method, path) not in routes:
            return 404, {"message": f"No route for {request.method} {request.path}"}
        return 200, routes[request.method, path]


_SITES = [
    pytest.param(("https://example.atlassian.net", "/wiki"), id="cloud"),
    pytest.param(("https://confluence.example.test/confluence", "/confluence"), id="server"),
]


@pytest.fixture(params=_SITES)
def site(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[str, _ConfluenceServer]:
    """Return a Confluence URL and the fake server that answers every HTTP request sent for it."""
    url, base_path = request.param
    server = _ConfluenceServer(base_path)
    host = urlsplit(url).netloc

    def send(_session: requests.Session, prepared: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        assert prepared.url is not None
        parts = urlsplit(prepared.url)
        assert parts.netloc == host
        assert prepared.headers["Authorization"].startswith("Basic ")
        body = json.loads(prepared.body) if prepared.body else None
        recorded = _Request(prepared.method or "", parts.path, parse_qs(parts.query), body)
        server.requests.append(recorded)
        status, payload = server.answer(recorded)
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        response.request = prepared
        response.url = prepared.url
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return url, server


def _toolkit(url: str) -> object:
    return confluence_tools()(url=url, username="bot@example.test", api_key="token")


def test_get_page_content_returns_the_page_or_a_not_found_error(site: tuple[str, _ConfluenceServer]) -> None:
    """A page lookup returns the page itself, and a missing page names the page and space."""
    url, server = site
    toolkit = _toolkit(url)

    assert json.loads(toolkit.get_page_content("Engineering", "Runbook")) == _PAGE
    assert json.loads(toolkit.get_page_content("Engineering", "Missing")) == {
        "error": "Page 'Missing' not found in space 'Engineering'",
    }
    lookup = next(request for request in server.requests if request.path.endswith("/rest/api/content"))
    assert lookup.query == {
        "type": ["page"],
        "start": ["0"],
        "limit": ["1"],
        "expand": ["body.storage"],
        "spaceKey": ["ENG"],
        "title": ["Runbook"],
    }


def test_create_page_posts_the_v1_content_body(site: tuple[str, _ConfluenceServer]) -> None:
    """Page creation sends atlassian-python-api 4.0.7's v1 content body under the space key."""
    url, server = site
    toolkit = _toolkit(url)

    assert json.loads(toolkit.create_page("Engineering", "New page", "<p>Hi</p>", parent_id="42")) == {
        "id": "77",
        "title": "New page",
    }
    assert server.requests[-1].body == {
        "type": "page",
        "title": "New page",
        "status": "current",
        "space": {"key": "ENG"},
        "body": {"storage": {"value": "<p>Hi</p>", "representation": "storage"}},
        "metadata": {
            "properties": {
                "content-appearance-draft": {"value": "fixed-width"},
                "content-appearance-published": {"value": "fixed-width"},
            },
        },
        "ancestors": [{"type": "page", "id": "42"}],
    }


def test_update_page_puts_the_next_version(site: tuple[str, _ConfluenceServer]) -> None:
    """A page update bumps the version from the page history and puts the v1 content body."""
    url, server = site
    toolkit = _toolkit(url)

    assert json.loads(toolkit.update_page("42", "Runbook v2", "<p>New</p>")) == {"status": "success", "id": "42"}
    update = server.requests[-1]
    assert (update.method, update.path, update.query) == (
        "PUT",
        f"{server.base_path}/rest/api/content/42",
        {
            "status": ["current"],
        },
    )
    assert update.body == {
        "id": "42",
        "type": "page",
        "title": "Runbook v2",
        "version": {"number": 5, "minorEdit": False},
        "metadata": {
            "properties": {
                "content-appearance-draft": {"value": "fixed-width"},
                "content-appearance-published": {"value": "fixed-width"},
            },
        },
        "body": {"storage": {"value": "<p>New</p>", "representation": "storage"}},
    }


def test_space_and_page_listings(site: tuple[str, _ConfluenceServer]) -> None:
    """Space details and every page of a space, read in batches of 50, come from the v1 listings."""
    url, server = site
    toolkit = _toolkit(url)

    assert ast.literal_eval(toolkit.get_all_space_detail()) == _SPACES["results"]
    assert ast.literal_eval(toolkit.get_all_page_from_space("ENG")) == [
        {"id": str(number), "title": f"Page {number}"} for number in range(51)
    ]
    listings = [request for request in server.requests if request.path.endswith("/rest/api/content")]
    assert [(listing.path, listing.query) for listing in listings] == [
        (f"{server.base_path}/rest/api/content", {"spaceKey": ["ENG"], "limit": ["50"], "type": ["page"]}),
        (
            f"{server.base_path}/rest/api/content",
            {"spaceKey": ["ENG"], "start": ["50"], "limit": ["50"], "type": ["page"]},
        ),
    ]
