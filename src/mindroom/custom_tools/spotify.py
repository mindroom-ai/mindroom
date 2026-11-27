"""Native Spotify toolkit using MindRoom's OAuth connection per request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.service import current_oauth_access_token
from mindroom.oauth.spotify import spotify_oauth_provider
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty, safe_path_segment

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_SearchAction = Literal["search", "recommendations", "album_tracks", "artist_top_tracks"]
_SearchType = Literal["track", "playlist", "artist", "album"]
_LibraryAction = Literal[
    "current_user",
    "currently_playing",
    "user_playlists",
    "playlist",
    "my_top_tracks",
    "my_top_artists",
]
_TimeRange = Literal["short_term", "medium_term", "long_term"]
_PlaylistWriteAction = Literal["create", "update", "add_tracks", "remove_tracks"]

_SPOTIFY_API_BASE = "https://api.spotify.com/v1"


def _simplify_track(track: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": track.get("id"),
        "name": track.get("name"),
        "artists": [artist["name"] for artist in track.get("artists", [])],
        "album": track.get("album", {}).get("name"),
        "uri": track.get("uri"),
        "popularity": track.get("popularity"),
    }
    if rank is not None:
        item["rank"] = rank
    if "preview_url" in track:
        item["preview_url"] = track.get("preview_url")
    return item


def _simplify_playlist(playlist: dict[str, Any]) -> dict[str, Any]:
    items_meta = playlist.get("items") if isinstance(playlist.get("items"), dict) else playlist.get("tracks")
    return {
        "id": playlist.get("id"),
        "name": playlist.get("name"),
        "owner": playlist.get("owner", {}).get("display_name"),
        "track_count": items_meta.get("total") if isinstance(items_meta, dict) else None,
        "url": playlist.get("external_urls", {}).get("spotify"),
        "uri": playlist.get("uri"),
        "public": playlist.get("public"),
    }


def _extract_playlist_item(entry: object) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    media = entry.get("item") if isinstance(entry.get("item"), dict) else entry.get("track")
    if not isinstance(media, dict):
        return None
    return {
        "id": media.get("id"),
        "name": media.get("name"),
        "artists": [a["name"] for a in media.get("artists", []) if isinstance(a, dict) and "name" in a],
        "uri": media.get("uri"),
    }


def _simplify_artist(artist: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": artist.get("id"),
        "name": artist.get("name"),
        "genres": artist.get("genres", []),
        "popularity": artist.get("popularity"),
        "uri": artist.get("uri"),
        "followers": artist.get("followers", {}).get("total"),
    }
    if rank is not None:
        item["rank"] = rank
    return item


class SpotifyTools(Toolkit):
    """Search Spotify, inspect library/playback, manage playlists, and control playback via OAuth."""

    def __init__(
        self,
        default_market: str | None = "US",
        timeout: int = 30,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager,
        worker_target: ResolvedWorkerTarget | None,
        runtime_config: Config | None = None,
    ) -> None:
        self.default_market = default_market
        self.timeout = timeout
        self.base_url = _SPOTIFY_API_BASE
        self._provider = spotify_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        super().__init__(
            name="spotify",
            tools=[self.spotify_search, self.spotify_library, self.spotify_playlist_write, self.spotify_play],
        )

    def _current_access_token(self) -> str:
        """Return a current access token, refreshing one about to expire, or raise the connect prompt."""
        context = active_oauth_credential_context(
            self._provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            config=self._config,
        )
        token, _ = current_oauth_access_token(context, _refresh_failed)
        return token

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self._current_access_token()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(method=method, url=url, headers=headers, json=body, params=params)
            if response.status_code == 204 or (response.is_success and not response.content):
                return {"success": True}
            try:
                payload = response.json()
            except ValueError:
                if response.status_code >= 400:
                    return {
                        "error": {
                            "status": response.status_code,
                            "message": f"Failed to parse response: {response.text}",
                        },
                    }
                return {"error": f"Failed to parse response: {response.text}"}
            if isinstance(payload, dict):
                if response.status_code >= 400 and isinstance(payload.get("error"), dict):
                    payload["error"].setdefault("status", response.status_code)
                elif response.status_code >= 400 and "error" not in payload:
                    return {"error": {"status": response.status_code, "message": str(payload)}}
                return payload
            return {"data": payload}

    def _run_search(
        self,
        query: str | None,
        search_type: _SearchType,
        limit: int,
        market: str | None,
    ) -> Any:  # noqa: ANN401
        params: dict[str, Any] = {
            "q": require_non_empty(query, "query"),
            "type": search_type,
            "limit": max(1, min(limit, 50)),
        }
        if search_type in {"track", "album"} and (market or self.default_market):
            params["market"] = market or self.default_market
        result = self._make_request("search", params=params)
        if "error" in result:
            return result
        if search_type == "track":
            items = result.get("tracks", {}).get("items", [])
            return [_simplify_track(t) for t in items if isinstance(t, dict)]
        if search_type == "playlist":
            items = result.get("playlists", {}).get("items", [])
            return [_simplify_playlist(p) for p in items if isinstance(p, dict)]
        if search_type == "artist":
            items = result.get("artists", {}).get("items", [])
            return [_simplify_artist(a) for a in items if isinstance(a, dict)]
        items = result.get("albums", {}).get("items", [])
        return [
            {
                "id": album.get("id"),
                "name": album.get("name"),
                "artists": [artist["name"] for artist in album.get("artists", [])],
                "release_date": album.get("release_date"),
                "total_tracks": album.get("total_tracks"),
                "uri": album.get("uri"),
                "album_type": album.get("album_type"),
            }
            for album in items
            if isinstance(album, dict)
        ]

    def _run_recommendations(
        self,
        seed_tracks: list[str] | None,
        seed_artists: list[str] | None,
        seed_genres: list[str] | None,
        limit: int,
        target_energy: float | None,
        target_valence: float | None,
        target_danceability: float | None,
        target_tempo: float | None,
    ) -> Any:  # noqa: ANN401
        if not any((seed_tracks, seed_artists, seed_genres)):
            return {"error": "At least one seed (seed_tracks, seed_artists, or seed_genres) is required."}
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if seed_tracks:
            params["seed_tracks"] = ",".join(seed_tracks[:5])
        if seed_artists:
            params["seed_artists"] = ",".join(seed_artists[:5])
        if seed_genres:
            params["seed_genres"] = ",".join(seed_genres[:5])
        params.update(
            {
                key: value
                for key, value in (
                    ("target_energy", target_energy),
                    ("target_valence", target_valence),
                    ("target_danceability", target_danceability),
                    ("target_tempo", target_tempo),
                )
                if value is not None
            },
        )
        result = self._make_request("recommendations", params=params)
        if "error" in result:
            return result
        return [_simplify_track(t) for t in result.get("tracks", []) if isinstance(t, dict)]

    def _album_tracks(self, item_id: str | None, market: str | None) -> dict[str, Any]:
        safe_album = safe_path_segment(require_non_empty(item_id, "item_id"), "item_id")
        params = {"market": market or self.default_market} if (market or self.default_market) else None
        album = self._make_request(f"albums/{safe_album}", params=params)
        if "error" in album:
            return album
        return {
            "album": {
                "id": album.get("id"),
                "name": album.get("name"),
                "artists": [a["name"] for a in album.get("artists", [])],
                "release_date": album.get("release_date"),
                "total_tracks": album.get("total_tracks"),
                "uri": album.get("uri"),
            },
            "tracks": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "track_number": t.get("track_number"),
                    "duration_ms": t.get("duration_ms"),
                    "uri": t.get("uri"),
                    "artists": [a["name"] for a in t.get("artists", [])],
                }
                for t in album.get("tracks", {}).get("items", [])
                if isinstance(t, dict)
            ],
        }

    def _artist_top_tracks(self, item_id: str | None, market: str | None) -> Any:  # noqa: ANN401
        safe_artist = safe_path_segment(require_non_empty(item_id, "item_id"), "item_id")
        params = {"market": market or self.default_market} if (market or self.default_market) else None
        result = self._make_request(f"artists/{safe_artist}/top-tracks", params=params)
        if "error" in result:
            return result
        return [_simplify_track(t) for t in result.get("tracks", []) if isinstance(t, dict)]

    def spotify_search(
        self,
        action: _SearchAction,
        query: str | None = None,
        search_type: _SearchType = "track",
        item_id: str | None = None,
        limit: int = 10,
        market: str | None = None,
        seed_tracks: list[str] | None = None,
        seed_artists: list[str] | None = None,
        seed_genres: list[str] | None = None,
        target_energy: float | None = None,
        target_valence: float | None = None,
        target_danceability: float | None = None,
        target_tempo: float | None = None,
    ) -> str:
        """Search Spotify catalog or fetch recommendations/tracks.

        - `search`: `query`, `search_type` (`track`/`playlist`/`artist`/`album`), `limit`, `market`
        - `recommendations`: `seed_tracks`/`seed_artists`/`seed_genres`, audio targets, `limit`
        - `album_tracks` / `artist_top_tracks`: `item_id`, `market`
        """
        try:
            if action == "search":
                data: Any = self._run_search(query, search_type, limit, market)
            elif action == "recommendations":
                data = self._run_recommendations(
                    seed_tracks,
                    seed_artists,
                    seed_genres,
                    limit,
                    target_energy,
                    target_valence,
                    target_danceability,
                    target_tempo,
                )
            elif action == "album_tracks":
                data = self._album_tracks(item_id, market)
            else:
                data = self._artist_top_tracks(item_id, market)
            return json_dumps(data)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(str(exc))

    def _get_currently_playing(self) -> dict[str, Any]:
        result = self._make_request("me/player/currently-playing")
        if not result or result.get("success"):
            return {"message": "Nothing currently playing"}
        if "error" in result:
            return result
        track = result.get("item")
        return {
            "is_playing": result.get("is_playing"),
            "progress_ms": result.get("progress_ms"),
            "track": (
                {
                    "id": track.get("id"),
                    "name": track.get("name"),
                    "artists": [a["name"] for a in track.get("artists", [])],
                    "album": track.get("album", {}).get("name"),
                    "uri": track.get("uri"),
                    "duration_ms": track.get("duration_ms"),
                }
                if isinstance(track, dict)
                else None
            ),
            "device": result.get("device", {}).get("name"),
        }

    def _get_playlist(
        self,
        playlist_id: str | None,
        *,
        include_tracks: bool,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_id = safe_path_segment(require_non_empty(playlist_id, "playlist_id"), "playlist_id")
        fields = "id,name,description,public,owner(display_name),external_urls"
        result = self._make_request(f"playlists/{safe_id}", params={"fields": fields})
        if "error" in result:
            return result
        info: dict[str, Any] = {
            "id": result.get("id"),
            "name": result.get("name"),
            "description": result.get("description"),
            "public": result.get("public"),
            "owner": result.get("owner", {}).get("display_name"),
            "url": result.get("external_urls", {}).get("spotify"),
        }
        if include_tracks:
            item_limit = max(1, min(limit, 50))
            item_offset = max(0, offset)
            items_res = self._make_request(
                f"playlists/{safe_id}/items",
                params={"limit": item_limit, "offset": item_offset},
            )
            if "error" in items_res:
                err = items_res["error"]
                if isinstance(err, dict) and err.get("status") == 403:
                    info["tracks"] = []
                    info["tracks_unavailable"] = True
                    info["tracks_note"] = (
                        "Playlist track items are unavailable because Spotify restricts /playlists/{id}/items "
                        "to playlists owned or collaborated on by the authenticated user."
                    )
                    return info
                return items_res
            raw_items = items_res.get("items", [])
            tracks = [parsed for entry in raw_items if (parsed := _extract_playlist_item(entry)) is not None]
            info["tracks"] = tracks
            total_tracks = items_res.get("total")
            info["total_tracks"] = total_tracks if isinstance(total_tracks, int) else item_offset + len(tracks)
            info["tracks_truncated"] = bool(items_res.get("next")) or (
                isinstance(total_tracks, int) and (item_offset + len(raw_items)) < total_tracks
            )
        return info

    def spotify_library(
        self,
        action: _LibraryAction,
        playlist_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        time_range: _TimeRange = "medium_term",
        include_tracks: bool = True,
    ) -> str:
        """Read Spotify profile, playback, playlists, and top items.

        - `current_user` / `currently_playing`
        - `user_playlists` (`limit`)
        - `playlist`: `playlist_id` (`include_tracks`, `limit`, `offset`)
        - `my_top_tracks` / `my_top_artists` (`time_range`, `limit`)
        """
        try:
            capped = max(1, min(limit, 50))
            if action == "current_user":
                data: Any = self._make_request("me")
            elif action == "currently_playing":
                data = self._get_currently_playing()
            elif action == "user_playlists":
                res = self._make_request("me/playlists", params={"limit": capped})
                data = (
                    res
                    if "error" in res
                    else [_simplify_playlist(p) for p in res.get("items", []) if isinstance(p, dict)]
                )
            elif action == "playlist":
                data = self._get_playlist(playlist_id, include_tracks=include_tracks, limit=limit, offset=offset)
            elif action == "my_top_tracks":
                res = self._make_request("me/top/tracks", params={"time_range": time_range, "limit": capped})
                data = (
                    res
                    if "error" in res
                    else [
                        _simplify_track(t, rank=i + 1)
                        for i, t in enumerate(res.get("items", []))
                        if isinstance(t, dict)
                    ]
                )
            else:
                res = self._make_request("me/top/artists", params={"time_range": time_range, "limit": capped})
                data = (
                    res
                    if "error" in res
                    else [
                        _simplify_artist(a, rank=i + 1)
                        for i, a in enumerate(res.get("items", []))
                        if isinstance(a, dict)
                    ]
                )
            return json_dumps(data)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(str(exc))

    def _create_playlist(
        self,
        name: str | None,
        description: str | None,
        public: bool | None,
        track_uris: list[str] | None,
    ) -> dict[str, Any]:
        clean_name = require_non_empty(name, "name")
        playlist = self._make_request(
            "me/playlists",
            method="POST",
            body={"name": clean_name, "description": description or "", "public": bool(public)},
        )
        if "error" in playlist:
            return playlist
        tracks_added = 0
        if track_uris:
            pid = safe_path_segment(str(playlist.get("id", "")), "playlist_id")
            add_res = self._make_request(f"playlists/{pid}/items", method="POST", body={"uris": track_uris[:100]})
            if "error" in add_res:
                playlist["track_add_error"] = add_res["error"]
            else:
                tracks_added = len(track_uris[:100])
        out: dict[str, Any] = {
            "id": playlist.get("id"),
            "name": playlist.get("name"),
            "description": playlist.get("description"),
            "url": playlist.get("external_urls", {}).get("spotify"),
            "uri": playlist.get("uri"),
            "tracks_added": tracks_added,
        }
        if "track_add_error" in playlist:
            out["track_add_error"] = playlist["track_add_error"]
        return out

    def _update_playlist(
        self,
        playlist_id: str | None,
        name: str | None,
        description: str | None,
        public: bool | None,
    ) -> dict[str, Any]:
        safe_id = safe_path_segment(require_non_empty(playlist_id, "playlist_id"), "playlist_id")
        body = {
            key: value
            for key, value in (("name", name), ("description", description), ("public", public))
            if value is not None
        }
        if not body:
            return {"error": "Provide at least one field to update (name, description, or public)."}
        res = self._make_request(f"playlists/{safe_id}", method="PUT", body=body)
        if res.get("success") or "error" not in res:
            return {"success": True, "playlist_id": safe_id, "updated": body}
        return res

    def spotify_playlist_write(
        self,
        action: _PlaylistWriteAction,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
        playlist_id: str | None = None,
        track_uris: list[str] | None = None,
        position: int | None = None,
    ) -> str:
        """Create or modify Spotify playlists.

        - `create`: `name` (`description`, `public`, `track_uris`)
        - `update`: `playlist_id` (`name`, `description`, `public`)
        - `add_tracks`: `playlist_id`, `track_uris` (max 100; optional 0-indexed `position`)
        - `remove_tracks`: `playlist_id`, `track_uris`
        """
        try:
            if action == "create":
                data: dict[str, Any] = self._create_playlist(name, description, public, track_uris)
            elif action == "update":
                data = self._update_playlist(playlist_id, name, description, public)
            else:
                safe_id = safe_path_segment(require_non_empty(playlist_id, "playlist_id"), "playlist_id")
                if not track_uris:
                    return error_json("track_uris must be a non-empty list of Spotify track URIs.")
                if action == "add_tracks":
                    body: dict[str, Any] = {"uris": track_uris[:100]}
                    if position is not None:
                        body["position"] = position
                    res = self._make_request(f"playlists/{safe_id}/items", method="POST", body=body)
                    data = (
                        {"success": True, "tracks_added": len(track_uris[:100]), "snapshot_id": res["snapshot_id"]}
                        if "snapshot_id" in res
                        else res
                    )
                else:
                    res = self._make_request(
                        f"playlists/{safe_id}/items",
                        method="DELETE",
                        body={"items": [{"uri": uri} for uri in track_uris]},
                    )
                    data = (
                        {"success": True, "tracks_removed": len(track_uris), "snapshot_id": res["snapshot_id"]}
                        if "snapshot_id" in res
                        else res
                    )
            return json_dumps(data)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(str(exc))

    def spotify_play(
        self,
        track_uri: str | None = None,
        context_uri: str | None = None,
        device_id: str | None = None,
        position_ms: int = 0,
    ) -> str:
        """Start or resume Spotify playback on an active device."""
        try:
            params = {"device_id": device_id} if device_id else None
            body: dict[str, Any] = {}
            if track_uri:
                body["uris"] = [track_uri]
            if context_uri:
                body["context_uri"] = context_uri
            if position_ms:
                body["position_ms"] = position_ms
            result = self._make_request(
                "me/player/play",
                method="PUT",
                body=body or None,
                params=params,
            )
            if result.get("success") or not result.get("error"):
                return json_dumps({"success": True, "message": "Playback started"})
            if isinstance(result.get("error"), dict) and result["error"].get("reason") == "NO_ACTIVE_DEVICE":
                return error_json(
                    "No active Spotify device found. Please open Spotify on any device first.",
                    reason="NO_ACTIVE_DEVICE",
                )
            return json_dumps(result)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(str(exc))


def _refresh_failed(exc: OAuthProviderError) -> OAuthProviderError:
    logger.warning("spotify_oauth_refresh_failed", error_type=type(exc).__name__)
    return OAuthProviderError("Spotify authorization could not be refreshed. Retry this request shortly.")
