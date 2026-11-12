"""Published reports that open only for people signed in through the Connections portal."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from mindroom.api import auth, main
from mindroom.api import report_publishing as report_routes
from mindroom.api.report_headers import _STATIC_SITE_CSP
from mindroom.report_publishing.store import PublishableReport, ReportPublishingStore
from tests.api.test_connections_signin import (
    _DASHBOARD_ROUTE,
    CHAT_ORIGIN,
    PORTAL_ORIGIN,
    _serve,
    _serve_with_trusted_upstream,
    hand_off,
    sign_in,
)
from tests.api.test_connections_signin import signin as signin  # noqa: PLC0414 - pytest fixture
from tests.api.test_report_publishing_api import _publish_public_report, _publish_static_site

if TYPE_CHECKING:
    from httpx import Response

SIGN_IN_PAGE = f"{CHAT_ORIGIN}/connections"
RETURN_COOKIE = "__Host-mindroom_report_return"
UNKNOWN_SLUG = "pub_" + "0" * 32
_SIGNED_IN = {"MINDROOM_PUBLISHED_REPORTS_ACCESS": "signed_in"}
# Every form a request for one slug can take: document, index, asset, and the ways a path can be spelled.
_PATH_FORMS = (
    "/reports/public/{slug}",
    "/reports/public/{slug}/",
    "/reports/public/{slug}/app.js",
    "/reports/public/{slug}//app.js",
    "/reports/public/{slug}%2Fapp.js",
    "/reports/public/{slug}/%2e%2e/{slug}/app.js",
    "/reports/public/{slug}/..%2F{slug}%2Fapp.js",
    "/reports/public/{slug}/~1.{fake}/app.js",
)


def _signed_in_mode(signin: dict[str, Any], **env: str) -> TestClient:
    _serve(signin, **{**_SIGNED_IN, **env})
    return signin["client"]


def _three_slugs(client: TestClient) -> dict[str, str]:
    """Publish a live and a revoked static site; return them with a slug that was never published."""
    live, _ = _publish_static_site(client)
    runtime_paths = main._app_runtime_paths(client.app)
    source_dir = runtime_paths.storage_root / "workspace-fixtures" / "site"
    store = ReportPublishingStore(runtime_paths.storage_root)
    revoked = store.publish_report(
        source=PublishableReport(
            source_type="static_site",
            source={"path": "site"},
            artifact_path=source_dir,
            title="Demo Site",
            requested_by="@alice:example.org",
            artifact_kind="static_site",
            artifact_root=source_dir.parent,
        ),
        published_by="@alice:example.org",
    ).slug
    store.revoke_public_report(revoked, revoked_by="@alice:example.org")
    return {"live": live, "revoked": revoked, "unknown": UNKNOWN_SLUG}


def _answer(response: Response, slug: str) -> tuple[int, str, list[tuple[str, str]]]:
    """Return what a response tells its reader, with the slug the request named blanked out."""
    headers = sorted((name, value.replace(slug, "SLUG")) for name, value in response.headers.multi_items())
    return response.status_code, response.text.replace(slug, "SLUG"), headers


def _view_link(client: TestClient, path: str) -> str:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


@pytest.mark.parametrize("form", _PATH_FORMS)
@pytest.mark.parametrize(
    ("method", "headers"),
    [("GET", {}), ("HEAD", {}), ("GET", {"Range": "bytes=0-3"}), ("GET", {"Accept": "text/html"})],
)
def test_signed_out_request_learns_nothing_of_a_report(
    signin: dict[str, Any],
    form: str,
    method: str,
    headers: dict[str, str],
) -> None:
    """Without a session, a live, a revoked, and an unknown slug get the same answer, and none of the report."""
    client = _signed_in_mode(signin)
    slugs = _three_slugs(client)
    answers = {}
    for kind, slug in slugs.items():
        fresh = TestClient(main.app, base_url=PORTAL_ORIGIN)
        path = form.format(slug=slug, fake="0" * 64)
        response = fresh.request(method, path, headers=headers, follow_redirects=False)
        assert "document.body.dataset.ready" not in response.text
        assert response.status_code != 301
        answers[kind] = _answer(response, slug)
    assert answers["live"] == answers["revoked"] == answers["unknown"]


def test_signed_out_browser_is_sent_to_sign_in_and_lands_on_the_report(signin: dict[str, Any]) -> None:
    """A signed-out visit goes to Chat's sign-in page, and Chat's hand-off comes back to the report."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)

    bounce = client.get(f"/reports/public/{slug}/", follow_redirects=False)
    assert bounce.status_code == 303
    assert bounce.headers["location"] == SIGN_IN_PAGE
    assert bounce.headers["cache-control"] == "no-store"
    assert bounce.headers["referrer-policy"] == "no-referrer"
    assert client.cookies[RETURN_COOKIE] == slug
    assert "samesite=none" in bounce.headers["set-cookie"].lower()

    landed = hand_off(client, "alice")
    assert landed.status_code == 303, landed.text
    assert landed.headers["location"] == f"/reports/public/{slug}"
    assert RETURN_COOKIE not in client.cookies

    page = client.get(landed.headers["location"])
    assert page.status_code == 200
    assert "app.js" in page.text


@pytest.mark.parametrize(
    ("path", "landing"),
    [
        (
            "/reports/public/{slug}/inner/page.html?region=emea&x=1",
            "/reports/public/{slug}/inner/page.html?region=emea&x=1",
        ),
        ("/reports/public/{slug}/Issue%20%2342%3F.html", "/reports/public/{slug}/Issue%20%2342%3F.html"),
        ("/reports/public/{slug}/~1.{fake}/app.js?v=2", "/reports/public/{slug}/app.js?v=2"),
        ("/reports/public/{slug}?next=//evil.example.org", "/reports/public/{slug}?next=//evil.example.org"),
    ],
)
def test_signed_out_deep_link_lands_on_the_page_asked_for(signin: dict[str, Any], path: str, landing: str) -> None:
    """Sign-in comes back to the page and query a signed-out browser asked for, inside the same report."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)

    bounce = client.get(path.format(slug=slug, fake="0" * 64), follow_redirects=False)
    assert (bounce.status_code, bounce.headers["location"]) == (303, SIGN_IN_PAGE)

    landed = hand_off(client, "alice")
    assert landed.headers["location"] == landing.format(slug=slug)
    assert RETURN_COOKIE not in client.cookies
    view = client.get(landed.headers["location"], follow_redirects=False)
    assert view.headers["location"].startswith(f"/reports/public/{slug}/~")


def test_hand_off_without_a_report_still_opens_the_portal(signin: dict[str, Any]) -> None:
    """A sign-in that no report started lands on the portal and sets no other cookie."""
    _signed_in_mode(signin)
    landed = hand_off(signin["client"], "alice")
    assert landed.headers["location"] == "/connections/"
    assert not any(RETURN_COOKIE in cookie for cookie in landed.headers.get_list("set-cookie"))


@pytest.mark.parametrize(
    "crafted",
    [
        "https://evil.example.org/",
        "//evil.example.org",
        "/\\evil.example.org",
        "/api/config",
        "../../api/config",
        UNKNOWN_SLUG + "/../../api/config",
        UNKNOWN_SLUG + "%2F..%2F..%2Fapi%2Fconfig",
        UNKNOWN_SLUG + "\\evil.example.org",
        UNKNOWN_SLUG.upper(),
        UNKNOWN_SLUG[:-1],
        UNKNOWN_SLUG + "/./api",
        UNKNOWN_SLUG + "/%2e%2e/%2e%2e/api/config",
        UNKNOWN_SLUG + "%2F..%252F..%252Fapi",
        UNKNOWN_SLUG + "%2F%252e%252e%252F..%252Fapi",
        UNKNOWN_SLUG + "/a%255Cb",
        UNKNOWN_SLUG + "%2F%25252e%25252e%2Fapi",
        quote(UNKNOWN_SLUG + "//evil.example.org/" + quote("../../x"), safe=""),
    ],
)
def test_hand_off_drops_a_crafted_way_back(signin: dict[str, Any], crafted: str) -> None:
    """Only the slug of a published report is a way back, to its path on this origin; anything else opens the portal."""
    client = _signed_in_mode(signin)
    client.cookies.set(RETURN_COOKIE, crafted)
    assert hand_off(client, "alice").headers["location"] == "/connections/"


def test_https_hand_off_ignores_a_way_back_a_sibling_host_could_plant(signin: dict[str, Any]) -> None:
    """Over HTTPS only the `__Host-` cookie counts, which no other host can set."""
    client = _signed_in_mode(signin)
    client.cookies.set("mindroom_report_return", UNKNOWN_SLUG)
    assert hand_off(client, "alice").headers["location"] == "/connections/"


def test_signed_out_browser_without_a_chat_sign_in_page_is_told_to_sign_in(signin: dict[str, Any]) -> None:
    """With no Chat page to send the browser to, a plain page says sign-in is required."""
    client = _signed_in_mode(signin, MINDROOM_CONNECTIONS_ALLOWED_ORIGINS='["capacitor://localhost"]')
    slug, _ = _publish_static_site(client)

    response = client.get(f"/reports/public/{slug}/", follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert "Sign-in required" in response.text
    assert RETURN_COOKIE not in client.cookies


def test_signed_in_person_sees_the_whole_static_site(signin: dict[str, Any]) -> None:
    """A session opens the site under a view link whose assets load without any cookie."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)
    assert sign_in(client, "alice").status_code == 200

    for entry in (f"/reports/public/{slug}", f"/reports/public/{slug}/"):
        assert _view_link(client, entry).startswith(f"/reports/public/{slug}/~")
    view = _view_link(client, f"/reports/public/{slug}/")
    assert view.endswith("/")

    cookieless = TestClient(main.app, base_url=PORTAL_ORIGIN)
    index = cookieless.get(view)
    script = cookieless.get(f"{view}app.js")
    image = cookieless.get(f"{view}image.png")
    assert index.status_code == script.status_code == image.status_code == 200
    assert "sandbox allow-scripts" in index.headers["content-security-policy"]
    assert "allow-same-origin" not in index.headers["content-security-policy"]
    assert index.headers["cache-control"] == "no-store, max-age=0"
    # Module scripts and CSS fonts are CORS requests from the sandbox's opaque origin.
    assert script.headers["access-control-allow-origin"] == "*"
    assert index.headers["x-robots-tag"] == "noindex, nofollow"
    assert index.headers["referrer-policy"] == "no-referrer"
    assert "document.body.dataset.ready" in script.text
    assert image.headers["content-type"].startswith("image/png")

    deep = _view_link(client, f"/reports/public/{slug}/app.js")
    assert cookieless.get(deep).text == script.text


def test_signed_in_person_opens_a_single_html_report(signin: dict[str, Any]) -> None:
    """An HTML report opens under a view link too, with its own unsandboxed policy."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_public_report(client)
    assert sign_in(client, "alice").status_code == 200

    response = client.get(_view_link(client, f"/reports/public/{slug}"))

    assert response.status_code == 200
    assert "Published report" in response.text
    assert "sandbox" not in response.headers["content-security-policy"]


def test_upstream_identity_opens_a_report_as_on_connections(
    signin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed upstream identity admits without a portal session."""
    signed_in = {**signin, "paths": replace(signin["paths"], process_env={**signin["paths"].process_env, **_SIGNED_IN})}
    client, upstream = _serve_with_trusted_upstream(signed_in, monkeypatch, signin["payload"])
    slug, _ = _publish_static_site(client)

    view = client.get(f"/reports/public/{slug}/", headers=upstream("alice"), follow_redirects=False)
    assert view.status_code == 303
    assert client.get(view.headers["location"]).status_code == 200
    signed_out = client.get(f"/reports/public/{slug}/app.js", follow_redirects=False)
    assert (signed_out.status_code, signed_out.headers["location"]) == (303, SIGN_IN_PAGE)


def test_view_link_opens_only_its_own_live_report_while_it_lasts(
    signin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged, expired, or other slug's view link opens nothing, and a revoke ends every view link at once."""
    client = _signed_in_mode(signin)
    slugs = _three_slugs(client)
    other, _ = _publish_public_report(client)
    assert sign_in(client, "alice").status_code == 200
    view = _view_link(client, f"/reports/public/{slugs['live']}/")
    other_view = _view_link(client, f"/reports/public/{other}")
    cookieless = TestClient(main.app, base_url=PORTAL_ORIGIN)
    assert cookieless.get(f"{view}app.js").status_code == 200

    token = view.removeprefix(f"/reports/public/{slugs['live']}/").rstrip("/")
    expires, _, signature = token.removeprefix("~").partition(".")
    forged = f"~{int(expires) + 3600}.{signature}"
    flipped = f"~{expires}.{'0' if signature[0] != '0' else '1'}{signature[1:]}"
    other_token = other_view.removeprefix(f"/reports/public/{other}/").rstrip("/")
    refused = [
        f"/reports/public/{slugs['live']}/{forged}/app.js",
        f"/reports/public/{slugs['live']}/{flipped}/app.js",
        f"/reports/public/{slugs['live']}/{other_token}/app.js",
    ]
    for path in refused:
        response = cookieless.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == SIGN_IN_PAGE

    later = time.time() + report_routes._VIEW_LINK_SECONDS + 1
    with monkeypatch.context() as patch:
        patch.setattr(report_routes.time, "time", lambda: later)
        expired = cookieless.get(f"{view}app.js", follow_redirects=False)
    assert (expired.status_code, expired.headers["location"]) == (303, SIGN_IN_PAGE)

    store = ReportPublishingStore(main._app_runtime_paths(client.app).storage_root)
    store.revoke_public_report(slugs["live"], revoked_by="@alice:example.org")
    assert cookieless.get(f"{view}app.js").status_code == 404
    assert client.get(f"{view}app.js").status_code == 404


def test_portal_session_counts_on_no_other_path_than_before(signin: dict[str, Any]) -> None:
    """The portal session counts on report requests only: not on the dashboard API, nor on the dashboard shell."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)
    assert sign_in(client, "alice").status_code == 200

    assert client.get(_DASHBOARD_ROUTE).status_code == 401
    # HEAD and a path without a slug reach the dashboard shell, which must refuse the session as on any other page.
    refused = client.head("/agents", follow_redirects=False)
    for method, path in [("HEAD", f"/reports/public/{slug}/"), ("GET", "/reports/public/")]:
        response = client.request(method, path, follow_redirects=False)
        assert (response.status_code, response.headers.get("location")) == (
            refused.status_code,
            refused.headers.get("location"),
        ), path


@pytest.mark.parametrize(
    ("method", "path", "honored"),
    [
        ("GET", "/reports/public/pub_" + "a" * 32, True),
        ("GET", "/reports/public/pub_" + "a" * 32 + "/x/y.js", True),
        ("HEAD", "/reports/public/pub_" + "a" * 32, False),
        ("POST", "/reports/public/pub_" + "a" * 32, False),
        ("GET", "/reports/public/", False),
        ("GET", "/reports/private/agent/x/y/z", False),
        ("GET", "/api/config", False),
    ],
)
def test_portal_session_path_rule(method: str, path: str, *, honored: bool) -> None:
    """The session counts only on GET requests the report routes answer; the dashboard shell answers the rest."""
    request = Request({"type": "http", "method": method, "path": path, "headers": []})
    assert auth._connections_session_path_honored(request) is honored


def test_unknown_access_value_refuses_to_start(signin: dict[str, Any]) -> None:
    """A typo never leaves reports open: startup refuses it, and so does every report request."""
    paths = replace(
        signin["paths"],
        process_env={**signin["paths"].process_env, "MINDROOM_PUBLISHED_REPORTS_ACCESS": "signed-in"},
    )
    with pytest.raises(ValueError, match="MINDROOM_PUBLISHED_REPORTS_ACCESS"):
        auth._build_auth_settings(paths)
    _serve(signin, MINDROOM_PUBLISHED_REPORTS_ACCESS="signed-in")
    slug, _ = _publish_static_site(signin["client"])
    with pytest.raises(ValueError, match="MINDROOM_PUBLISHED_REPORTS_ACCESS"):
        signin["client"].get(f"/reports/public/{slug}/")


@pytest.mark.parametrize("value", ["", "anyone_with_link"])
def test_open_mode_serves_reports_as_before_with_two_privacy_headers(signin: dict[str, Any], value: str) -> None:
    """Without sign-in required, a static site and an HTML report answer as before, plus two privacy headers."""
    client = _signed_in_mode(signin, MINDROOM_PUBLISHED_REPORTS_ACCESS=value)
    site, _ = _publish_static_site(client)

    bare = client.get(f"/reports/public/{site}", follow_redirects=False)
    assert (bare.status_code, bare.headers["location"]) == (301, f"{site}/")
    index = client.get(f"/reports/public/{site}/")
    volatile = {"etag", "last-modified", "content-length"}
    assert {name: value for name, value in index.headers.items() if name not in volatile} == {
        "content-type": "text/html; charset=utf-8",
        "accept-ranges": "bytes",
        "content-security-policy": _STATIC_SITE_CSP,
        "cache-control": "no-store, max-age=0",
        "pragma": "no-cache",
        "expires": "0",
        "x-robots-tag": "noindex, nofollow",
        "referrer-policy": "no-referrer",
    }
    assert index.text == "<!doctype html><script src='app.js'></script><img src='image.png'>"
    assert client.get(f"/reports/public/{UNKNOWN_SLUG}/").json() == {"detail": "Public report was not found."}


def test_signed_in_browser_gets_a_fresh_view_link_for_a_stale_one(
    signin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session turns an expired view link, or one missing its slash, into a fresh one for the same asset."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)
    assert sign_in(client, "alice").status_code == 200
    view = _view_link(client, f"/reports/public/{slug}/")
    token = view.removeprefix(f"/reports/public/{slug}/").rstrip("/")

    with monkeypatch.context() as patch:
        patch.setattr(report_routes, "_view_link_valid", lambda *_args: False)
        fresh = _view_link(client, f"{view}app.js")
    assert fresh.startswith(f"/reports/public/{slug}/~")
    assert fresh.endswith("/app.js")
    assert fresh.count("~") == 1
    slashless = _view_link(client, f"/reports/public/{slug}/{token}")
    assert slashless.startswith(f"/reports/public/{slug}/~")
    assert slashless.endswith("/")
    assert slashless.count("~") == 1


def test_signed_out_request_for_a_malformed_slug_remembers_no_way_back(signin: dict[str, Any]) -> None:
    """Only a well-formed slug is remembered across sign-in."""
    client = _signed_in_mode(signin)
    response = client.get("/reports/public/not-a-slug%C3%A9/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, SIGN_IN_PAGE)
    assert "set-cookie" not in response.headers


def test_sign_in_check_that_fails_on_the_server_is_not_a_sign_in_page(
    signin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sign-in check that cannot run answers its own error instead of sending the browser to sign in again."""
    client = _signed_in_mode(signin)

    async def unavailable(_request: Request) -> None:
        raise HTTPException(503, "Matrix OpenID verifier is unavailable.")

    monkeypatch.setattr(report_routes, "require_connections_user", unavailable)
    response = client.get(f"/reports/public/{UNKNOWN_SLUG}/", follow_redirects=False)
    assert response.status_code == 503


@pytest.mark.parametrize(
    "path",
    ["%2e%2e/%2e%2e/%2e%2e/%2e%2e/api/config", "app.js/%2E%2E/%2e%2e/x", "%2e/app.js", "a%5C..%5C..%5Capi"],
)
def test_signed_in_request_with_a_dot_segment_stays_inside_the_report(signin: dict[str, Any], path: str) -> None:
    """A path that a browser would resolve outside the report gets the report's not-found answer, not a redirect."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)
    assert sign_in(client, "alice").status_code == 200

    response = client.get(f"/reports/public/{slug}/{path}", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in response.headers


def test_view_link_keeps_the_asked_for_file_name_and_query(signin: dict[str, Any]) -> None:
    """The redirect to a view link names the same file, however its name is spelled, and keeps the query."""
    client = _signed_in_mode(signin)
    slug, _ = _publish_static_site(client)
    assert sign_in(client, "alice").status_code == 200

    index = _view_link(client, f"/reports/public/{slug}/?region=emea")
    assert index.endswith("/?region=emea")
    named = _view_link(client, f"/reports/public/{slug}/Issue%20%2342%3F.html?x=1")
    assert named.endswith("/Issue%20%2342%3F.html?x=1")
