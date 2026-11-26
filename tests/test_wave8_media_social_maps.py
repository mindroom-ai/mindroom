"""Tests for Wave 8 Batch 3 native media, social, maps & brand toolkits."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import googlemaps.exceptions
import httpx
import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.spotify import SpotifyTools
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.brandfetch import brandfetch_tools
from mindroom.tools.google_maps import google_maps_tools
from mindroom.tools.reddit import reddit_tools

if TYPE_CHECKING:
    from pathlib import Path


def test_batch3_action_scoped_grants_and_no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch 3 toolkits declare per-toolkit action_scoped_grants and never read host os.environ."""
    registry = builtin_tool_registry()
    assert registry.entries["spotify"].action_scoped_grants == (
        "spotify_search",
        "spotify_library",
        "spotify_playlist_write",
    )
    assert registry.entries["reddit"].action_scoped_grants == ("reddit_read", "reddit_write")
    assert registry.entries["google_maps"].action_scoped_grants == ("google_maps_places", "google_maps_routes")
    assert registry.entries["brandfetch"].action_scoped_grants == ("brandfetch_lookup",)

    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza-host-secret")
    monkeypatch.setenv("BRANDFETCH_API_KEY", "bf-host-secret")
    monkeypatch.setenv("BRANDFETCH_CLIENT_ID", "bf-host-cid")

    assert "required" in json.loads(reddit_tools()().reddit_read(action="trending_subreddits"))["error"]
    with pytest.raises(ValueError, match="Google Maps API key"):
        google_maps_tools()(key=None)
    assert "API key is required" in json.loads(brandfetch_tools()().brandfetch_lookup("nike.com"))["error"]


def test_spotify_search_playlist_playback_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spotify uses Feb 2026 playlist endpoints (/me/playlists, /playlists/{id}/items), playback, and path checks."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    manager = get_runtime_credentials_manager(paths)
    toolkit = SpotifyTools(runtime_paths=paths, credentials_manager=manager, worker_target=None)
    monkeypatch.setattr(toolkit, "_current_access_token", lambda: "tok-123")
    seen_calls: list[tuple[str, str, dict[str, object] | None]] = []

    responses: dict[tuple[str, str], tuple[int, dict[str, object] | None]] = {
        (
            "GET",
            "/v1/search",
        ): (
            200,
            {
                "tracks": {
                    "items": [
                        {
                            "id": "t1",
                            "name": "Track One",
                            "artists": [{"name": "Artist"}],
                            "album": {"name": "Album"},
                            "uri": "spotify:track:t1",
                            "popularity": 80,
                        },
                    ],
                },
            },
        ),
        (
            "POST",
            "/v1/me/playlists",
        ): (
            201,
            {
                "id": "pl1",
                "name": "Focus",
                "description": "",
                "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
                "uri": "spotify:playlist:pl1",
            },
        ),
        (
            "GET",
            "/v1/me/playlists",
        ): (
            200,
            {
                "items": [
                    {
                        "id": "pl1",
                        "name": "Focus",
                        "owner": {"display_name": "Listener"},
                        "items": {"total": 2},
                        "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
                        "uri": "spotify:playlist:pl1",
                        "public": False,
                    },
                ],
            },
        ),
        (
            "GET",
            "/v1/playlists/pl1",
        ): (
            200,
            {
                "id": "pl1",
                "name": "Focus",
                "description": "",
                "public": False,
                "owner": {"display_name": "Listener"},
                "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
            },
        ),
        (
            "GET",
            "/v1/playlists/pl1/items",
        ): (
            200,
            {
                "items": [
                    {
                        "item": {
                            "id": "t1",
                            "name": "Track One",
                            "artists": [{"name": "Artist"}],
                            "uri": "spotify:track:t1",
                        },
                    },
                    {
                        "track": {
                            "id": "t2",
                            "name": "Track Two",
                            "artists": [{"name": "Artist Two"}],
                            "uri": "spotify:track:t2",
                        },
                    },
                ],
            },
        ),
        ("POST", "/v1/playlists/pl1/items"): (201, {"snapshot_id": "snap-add"}),
        ("DELETE", "/v1/playlists/pl1/items"): (200, {"snapshot_id": "snap-del"}),
        ("PUT", "/v1/me/player/play"): (204, None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        seen_calls.append((request.method, request.url.path, body))
        entry = responses.get((request.method, request.url.path))
        if entry is None:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        status_code, payload = entry
        return httpx.Response(status_code) if payload is None else httpx.Response(status_code, json=payload)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    tracks = json.loads(toolkit.spotify_search(action="search", query="focus"))
    assert tracks[0]["id"] == "t1"

    playlist = json.loads(
        toolkit.spotify_playlist_write(action="create", name="Focus", track_uris=["spotify:track:t1"]),
    )
    assert playlist["id"] == "pl1"
    assert playlist["tracks_added"] == 1

    added = json.loads(
        toolkit.spotify_playlist_write(
            action="add_tracks",
            playlist_id="pl1",
            track_uris=["spotify:track:t2"],
            position=1,
        ),
    )
    assert added == {"success": True, "tracks_added": 1, "snapshot_id": "snap-add"}

    user_playlists = json.loads(toolkit.spotify_library(action="user_playlists"))
    assert user_playlists[0]["track_count"] == 2

    loaded = json.loads(toolkit.spotify_library(action="playlist", playlist_id="pl1", include_tracks=True))
    assert [t["id"] for t in loaded["tracks"]] == ["t1", "t2"]
    assert loaded["total_tracks"] == 2
    assert loaded["tracks_truncated"] is False

    removed = json.loads(
        toolkit.spotify_playlist_write(
            action="remove_tracks",
            playlist_id="pl1",
            track_uris=["spotify:track:t1"],
        ),
    )
    assert removed == {"success": True, "tracks_removed": 1, "snapshot_id": "snap-del"}

    played = json.loads(toolkit.spotify_play(track_uri="spotify:track:t1"))
    assert played["success"] is True
    bad = json.loads(toolkit.spotify_library(action="playlist", playlist_id="../escape"))
    assert "Invalid playlist_id" in bad["error"]

    assert ("POST", "/v1/me/playlists", {"name": "Focus", "description": "", "public": False}) in seen_calls
    assert ("POST", "/v1/playlists/pl1/items", {"uris": ["spotify:track:t1"]}) in seen_calls
    assert ("POST", "/v1/playlists/pl1/items", {"uris": ["spotify:track:t2"], "position": 1}) in seen_calls
    assert ("DELETE", "/v1/playlists/pl1/items", {"items": [{"uri": "spotify:track:t1"}]}) in seen_calls
    assert all("/users/" not in path and not path.endswith("/tracks") for _, path, _ in seen_calls)


def test_reddit_read_write_and_allowed_subreddits_boundary() -> None:
    """Reddit enforces allowed_subreddits (including empty list) and validates IDs."""
    fake_reply = SimpleNamespace(
        id="r1",
        body="hi",
        score=1,
        permalink="/r/python/comments/p1/_/r1/",
        created_utc=1.0,
        author="bot",
        parent_id="t3_p1",
        subreddit="python",
    )
    fake_sub_post = SimpleNamespace(
        id="p1",
        title="Hello",
        author="alice",
        subreddit=SimpleNamespace(display_name="python"),
        reply=lambda body: fake_reply,  # noqa: ARG005
    )
    fake_sub = SimpleNamespace(
        display_name="python",
        title="Python",
        description="desc",
        subscribers=100,
        active_user_count=5,
        created_utc=1.0,
        over18=False,
        public_description="pub",
        url="/r/python/",
        flair=SimpleNamespace(link_templates=[{"id": "tmpl-news-123", "text": "News"}]),
        top=lambda **_kwargs: [],
        submit=MagicMock(
            side_effect=lambda **kwargs: SimpleNamespace(
                id="p2",
                title=kwargs["title"],
                url="https://reddit.com/p2",
                permalink="/r/python/comments/p2/",
                created_utc=2.0,
                author="bot",
                link_flair_text="News" if kwargs.get("flair_id") == "tmpl-news-123" else None,
            ),
        ),
    )
    reddit = MagicMock()
    reddit.user.me.return_value = SimpleNamespace(name="bot")
    reddit.subreddit.return_value = fake_sub
    reddit.submission.return_value = fake_sub_post

    # Empty allowed_subreddits must block all writes, never act as unrestricted!
    blocked = reddit_tools()(reddit_instance=reddit, username="bot", password="pw", allowed_subreddits=[])  # noqa: S106
    assert "not in the allowed_subreddits scope" in blocked.reddit_write(
        action="create_post",
        subreddit="python",
        title="T",
        content="C",
    )

    toolkit = reddit_tools()(reddit_instance=reddit, username="bot", password="pw", allowed_subreddits=["Python"])  # noqa: S106
    stats = json.loads(toolkit.reddit_read(action="subreddit_stats", subreddit="python"))
    assert stats["subreddit_stats"]["subscribers"] == 100
    posted = json.loads(
        toolkit.reddit_write(action="create_post", subreddit="python", title="Hi", content="Body", flair="News"),
    )
    assert posted["post"]["id"] == "p2"
    assert fake_sub.submit.call_args.kwargs["flair_id"] == "tmpl-news-123"
    assert "Invalid flair" in toolkit.reddit_write(
        action="create_post",
        subreddit="python",
        title="Hi",
        content="Body",
        flair="Unknown",
    )
    replied = json.loads(
        toolkit.reddit_write(
            action="reply_to_post",
            post_id="https://www.reddit.com/r/python/comments/p1/hello/",
            content="Nice",
        ),
    )
    assert replied["reply"]["id"] == "r1"
    title_only = json.loads(
        toolkit.reddit_write(action="create_post", subreddit="python", title="Just a title", content="", is_self=True),
    )
    assert title_only["post"]["id"] == "p2"
    assert fake_sub.submit.call_args.kwargs["selftext"] == ""

    indented_md = "    def greet():\n        return 1\n"
    indented = json.loads(
        toolkit.reddit_write(
            action="create_post",
            subreddit="python",
            title="Code",
            content=indented_md,
            is_self=True,
        ),
    )
    assert indented["post"]["id"] == "p2"
    assert fake_sub.submit.call_args.kwargs["selftext"] == indented_md

    empty_link = json.loads(
        toolkit.reddit_write(action="create_post", subreddit="python", title="Link", content="", is_self=False),
    )
    assert "content is required" in empty_link["error"]

    bad = json.loads(toolkit.reddit_read(action="user_info", username="../admin"))
    assert "Invalid username" in bad["error"]


def test_google_maps_toggles_and_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google Maps respects explicit None toggles, stays offline on init, and dispatches calls."""
    disabled = google_maps_tools()(
        key="AIza-test",
        search_places=None,
        get_directions=None,
        validate_address=None,
        geocode_address=None,
        reverse_geocode=None,
        get_distance_matrix=None,
        get_elevation=None,
        get_timezone=None,
    )
    assert list(disabled.functions) == []

    toolkit = google_maps_tools()(key="AIza-test", get_elevation=False)
    toolkit.client = MagicMock()
    toolkit.client.geocode.return_value = [{"formatted_address": "Amsterdam"}]
    toolkit.client.directions.return_value = [{"summary": "A10"}]

    fake_place = SimpleNamespace(
        display_name=SimpleNamespace(text="Cafe"),
        formatted_address="1 Main St",
        rating=4.8,
        reviews=[SimpleNamespace(text=SimpleNamespace(text="Great"), rating=5)],
        id="place-1",
        international_phone_number="+1",
        website_uri="https://cafe.example",
        regular_opening_hours=SimpleNamespace(weekday_descriptions=["Mon: 9-5"]),
    )
    places_client = MagicMock()
    places_client.search_text.return_value = SimpleNamespace(places=[fake_place])
    monkeypatch.setattr(toolkit, "_get_places_client", lambda: places_client)

    assert json.loads(toolkit.google_maps_places(action="search_places", query="cafe"))[0]["name"] == "Cafe"
    assert (
        json.loads(toolkit.google_maps_places(action="geocode", address="Amsterdam"))[0]["formatted_address"]
        == "Amsterdam"
    )
    assert "disabled" in json.loads(toolkit.google_maps_places(action="elevation", lat=1.0, lng=2.0))["error"]
    assert (
        json.loads(toolkit.google_maps_routes(action="directions", origin="A", destination="B"))[0]["summary"] == "A10"
    )


def test_brandfetch_toggles_validation_and_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brandfetch respects explicit None toggles, rejects traversal queries, and dispatches both actions."""
    disabled = brandfetch_tools()(
        api_key="k",
        client_id="c",
        enable_search_by_identifier=None,
        enable_search_by_brand=None,
    )
    assert list(disabled.functions) == []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/brands/nike.com":
            assert request.headers["authorization"] == "Bearer bf_key"
            return httpx.Response(200, json={"name": "Nike"})
        if request.url.path == "/v2/search/Nike":
            assert request.url.params["c"] == "bf_cid"
            return httpx.Response(200, json=[{"name": "Nike", "domain": "nike.com"}])
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    default_tool = brandfetch_tools()(api_key="bf_key", client_id="bf_cid")
    assert json.loads(default_tool.brandfetch_lookup("nike.com"))["name"] == "Nike"
    assert "disabled" in json.loads(default_tool.brandfetch_lookup("Nike", action="by_brand"))["error"]

    all_tool = brandfetch_tools()(api_key="bf_key", client_id="bf_cid", all=True)
    assert json.loads(all_tool.brandfetch_lookup("Nike", action="by_brand"))[0]["domain"] == "nike.com"
    assert "Invalid query" in json.loads(all_tool.brandfetch_lookup("../secret"))["error"]


def test_reddit_app_only_isolation_ignores_ambient_praw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """App-only Reddit client pins official endpoints, ignores praw_* env vars, and stays read-only."""
    monkeypatch.setenv("praw_username", "host_user")
    monkeypatch.setenv("praw_password", "host_pass")
    monkeypatch.setenv("praw_refresh_token", "host_refresh")
    monkeypatch.setenv("praw_redirect_uri", "https://evil.example/cb")
    monkeypatch.setenv("praw_site", "missing_host_site")
    monkeypatch.setenv("praw_reddit_url", "https://evil.example")
    monkeypatch.setenv("praw_oauth_url", "https://evil-oauth.example")
    monkeypatch.setenv("praw_short_url", "https://evil.short")

    toolkit = reddit_tools()(client_id="cid", client_secret="csec", user_agent="TestAgent/1.0")  # noqa: S106
    assert toolkit.reddit is not None
    assert toolkit.reddit.read_only is True
    assert toolkit.reddit.config.username is None
    assert toolkit.reddit.config.password is None
    assert toolkit.reddit.config.refresh_token is None
    assert toolkit.reddit.config.redirect_uri is None
    assert toolkit.reddit.config.reddit_url == "https://www.reddit.com"
    assert toolkit.reddit.config.oauth_url == "https://oauth.reddit.com"
    assert toolkit.reddit.config.short_url == "https://redd.it"
    assert toolkit.reddit.config.check_for_updates is False
    assert "User authentication required" in toolkit.reddit_write(
        action="create_post",
        subreddit="python",
        title="Title",
        content="Body",
    )


def test_google_maps_sanitizes_transport_error_fixes_distance_matrix_avoid_and_pins_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Google Maps scrubs API keys from TransportError, accepts single avoid on distance_matrix, and pins endpoints."""
    api_key = "AIzaSySecretKeyForMapsRedactionTest99"
    monkeypatch.setenv("GOOGLE_CLOUD_UNIVERSE_DOMAIN", "evil.example")
    monkeypatch.setenv("GOOGLE_API_USE_MTLS_ENDPOINT", "always")

    toolkit = google_maps_tools()(key=api_key)
    assert toolkit.client.base_url == "https://maps.googleapis.com"
    places_client = toolkit._get_places_client()
    assert places_client.api_endpoint == "places.googleapis.com"
    assert places_client.universe_domain == "googleapis.com"

    # Real googlemaps.distance_matrix validation runs here; only _request is stubbed.
    seen_params: list[dict[str, object]] = []

    def fake_request(url: str, params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        seen_params.append({"url": url, **params})
        return {"rows": [{"elements": [{"status": "OK"}]}]}

    monkeypatch.setattr(toolkit.client, "_request", fake_request)
    dm = json.loads(
        toolkit.google_maps_routes(
            action="distance_matrix",
            origins=["Seattle"],
            destinations=["Portland"],
            avoid=["tolls"],
        ),
    )
    assert dm["rows"][0]["elements"][0]["status"] == "OK"
    assert seen_params[-1]["avoid"] == "tolls"

    multi_avoid = json.loads(
        toolkit.google_maps_routes(
            action="distance_matrix",
            origins=["Seattle"],
            destinations=["Portland"],
            avoid=["tolls", "highways"],
        ),
    )
    assert "at most one avoid" in multi_avoid["error"]

    # TransportError wrapping a URL with ?key=... must never leak the API key to tool output or logs.
    leaky = googlemaps.exceptions.TransportError(
        RuntimeError(
            f"HTTPSConnectionPool(host='maps.googleapis.com', port=443): Max retries exceeded with url: "
            f"/maps/api/geocode/json?address=Paris&key={api_key}",
        ),
    )
    toolkit.client.geocode = MagicMock(side_effect=leaky)
    toolkit.client.directions = MagicMock(side_effect=leaky)

    with caplog.at_level(logging.DEBUG):
        places_err = toolkit.google_maps_places(action="geocode", address="Paris")
        routes_err = toolkit.google_maps_routes(action="directions", origin="A", destination="B")

    assert api_key not in places_err
    assert "[REDACTED]" in places_err
    assert api_key not in routes_err
    assert "[REDACTED]" in routes_err
    assert api_key not in caplog.text


def test_spotify_playlist_403_preserves_metadata_and_reports_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public non-owned playlist 403 on /items keeps metadata; paged /items reports total_tracks and truncation."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    manager = get_runtime_credentials_manager(paths)
    toolkit = SpotifyTools(runtime_paths=paths, credentials_manager=manager, worker_target=None)
    monkeypatch.setattr(toolkit, "_current_access_token", lambda: "tok-123")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/playlists/pub1":
            return httpx.Response(
                200,
                json={
                    "id": "pub1",
                    "name": "Today Top Hits",
                    "description": "Public chart",
                    "public": True,
                    "owner": {"display_name": "Spotify"},
                    "external_urls": {"spotify": "https://open.spotify.com/playlist/pub1"},
                },
            )
        if request.url.path == "/v1/playlists/pub1/items":
            return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
        if request.url.path == "/v1/playlists/big1":
            return httpx.Response(
                200,
                json={
                    "id": "big1",
                    "name": "Big Owned",
                    "description": "",
                    "public": False,
                    "owner": {"display_name": "Me"},
                    "external_urls": {"spotify": "https://open.spotify.com/playlist/big1"},
                },
            )
        if request.url.path == "/v1/playlists/big1/items":
            return httpx.Response(
                200,
                json={
                    "total": 150,
                    "next": "https://api.spotify.com/v1/playlists/big1/items?offset=100&limit=100",
                    "items": [
                        {
                            "item": {
                                "id": "t1",
                                "name": "Track One",
                                "artists": [{"name": "Artist"}],
                                "uri": "spotify:track:t1",
                            },
                        },
                    ],
                },
            )
        return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    public_pl = json.loads(toolkit.spotify_library(action="playlist", playlist_id="pub1", include_tracks=True))
    assert public_pl["id"] == "pub1"
    assert public_pl["name"] == "Today Top Hits"
    assert public_pl["owner"] == "Spotify"
    assert public_pl["tracks"] == []
    assert public_pl["tracks_unavailable"] is True
    assert "permissions" in public_pl["tracks_note"] or "owned" in public_pl["tracks_note"]

    big_pl = json.loads(toolkit.spotify_library(action="playlist", playlist_id="big1", include_tracks=True))
    assert [t["id"] for t in big_pl["tracks"]] == ["t1"]
    assert big_pl["total_tracks"] == 150
    assert big_pl["tracks_truncated"] is True
