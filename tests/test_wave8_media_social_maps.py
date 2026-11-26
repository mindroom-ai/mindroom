"""Tests for Wave 8 Batch 3 native media, social, maps & brand toolkits."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

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
    """Spotify executes search, playlist mutations, playback, and rejects traversal IDs."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    manager = get_runtime_credentials_manager(paths)
    toolkit = SpotifyTools(runtime_paths=paths, credentials_manager=manager, worker_target=None)
    monkeypatch.setattr(toolkit, "_current_access_token", lambda: "tok-123")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
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
            )
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"id": "u1"})
        if request.url.path == "/v1/users/u1/playlists":
            return httpx.Response(
                200,
                json={
                    "id": "pl1",
                    "name": "Focus",
                    "description": "",
                    "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
                    "uri": "spotify:playlist:pl1",
                },
            )
        if request.url.path == "/v1/me/player/play":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"message": "not found"}})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    tracks = json.loads(toolkit.spotify_search(action="search", query="focus"))
    assert tracks[0]["id"] == "t1"
    playlist = json.loads(toolkit.spotify_playlist_write(action="create", name="Focus"))
    assert playlist["id"] == "pl1"
    played = json.loads(toolkit.spotify_play(track_uri="spotify:track:t1"))
    assert played["success"] is True
    bad = json.loads(toolkit.spotify_library(action="playlist", playlist_id="../escape"))
    assert "Invalid playlist_id" in bad["error"]


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
        flair=SimpleNamespace(link_templates=[{"text": "News"}]),
        top=lambda **_kwargs: [],
        submit=lambda **kwargs: SimpleNamespace(
            id="p2",
            title=kwargs["title"],
            url="https://reddit.com/p2",
            permalink="/r/python/comments/p2/",
            created_utc=2.0,
            author="bot",
            link_flair_text=kwargs.get("flair_id"),
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
    posted = json.loads(toolkit.reddit_write(action="create_post", subreddit="python", title="Hi", content="Body"))
    assert posted["post"]["id"] == "p2"
    replied = json.loads(
        toolkit.reddit_write(
            action="reply_to_post",
            post_id="https://www.reddit.com/r/python/comments/p1/hello/",
            content="Nice",
        ),
    )
    assert replied["reply"]["id"] == "r1"
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
