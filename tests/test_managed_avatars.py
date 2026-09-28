"""Tests for choosing the avatars managed agents, teams, and rooms display."""

from __future__ import annotations

import functools
import io
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import nio
import pytest
from PIL import Image
from structlog.testing import capture_logs

from mindroom import bot as bot_module
from mindroom import constants as constants_mod
from mindroom import managed_avatars
from mindroom.access_policy import resolve_room_policy
from mindroom.bounded_bytes import ByteLimitExceededError
from mindroom.config.main import Config
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix.users import AgentMatrixUser
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import TEST_PASSWORD

_PINNED_COMMIT = "f685f674404ebb9709d707f49298450211a7d603"
# The suite-wide fixture in conftest.py disables stock downloads; keep the real one to test it directly.
_real_download_stock_avatar = managed_avatars._download_stock_avatar


def _image_bytes(image_format: str = "PNG", size: tuple[int, int] = (512, 300)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 90)).save(buffer, format=image_format)
    return buffer.getvalue()


class _FakeDownloads:
    """Record stock downloads and answer each with the configured image."""

    def __init__(self, payload: bytes | BaseException) -> None:
        self.payload = payload
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload

    @property
    def names(self) -> list[str]:
        return [url.rsplit("/", 1)[1].removesuffix(".png") for url in self.urls]


@pytest.fixture
def runtime_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> constants_mod.RuntimePaths:
    """Point workspace overrides and the repository's bundled avatars at empty temporary directories."""
    monkeypatch.setattr(constants_mod, "_avatars_dir", lambda _runtime_paths: tmp_path / "avatars")
    monkeypatch.setattr(constants_mod, "_bundled_avatars_dir", lambda: tmp_path / "bundled")
    return constants_mod.resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> _FakeDownloads:
    """Replace the network download with a recorder that returns a valid image."""
    fake = _FakeDownloads(_image_bytes())
    monkeypatch.setattr(managed_avatars, "_download_stock_avatar", fake)
    return fake


def _config(runtime_paths: constants_mod.RuntimePaths, *, agents: dict, teams: dict | None = None) -> Config:
    raw = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-5"}},
        "router": {"model": "default"},
        "agents": agents,
        "teams": teams or {},
    }
    return Config.validate_with_runtime(raw, runtime_paths)


def _cache_path(runtime_paths: constants_mod.RuntimePaths, name: str) -> Path:
    return runtime_paths.storage_root / "avatars" / "stock" / f"{_PINNED_COMMIT[:12]}-{name}.png"


def test_stock_avatar_names_match_the_assets_repository() -> None:
    """The stock set mirrors the painted agent avatars at the pinned assets commit."""
    assert managed_avatars._STOCK_AVATAR_NAMES == (
        "analyst",
        "builder",
        "calculator",
        "code",
        "computer",
        "data",
        "email",
        "finance",
        "general",
        "helper",
        "home",
        "mind",
        "mind-logo",
        "news",
        "phone",
        "planner",
        "research",
        "router",
        "security",
        "shell",
        "storyteller",
        "summary",
        "writer",
    )


def test_stock_avatar_url_pins_the_assets_commit() -> None:
    """Stock images come from one immutable commit of the assets repository."""
    assert managed_avatars._stock_avatar_url("code") == (
        f"https://raw.githubusercontent.com/mindroom-ai/assets/{_PINNED_COMMIT}/avatars/painted/agents/code.png"
    )


@pytest.mark.asyncio
async def test_bundled_avatar_is_used_without_download(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    tmp_path: Path,
) -> None:
    """Avatars bundled in the repository's avatars/ directory win over a stock download."""
    bundled = tmp_path / "bundled" / "agents" / "code.png"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(_image_bytes())

    assert await managed_avatars.entity_avatar_path("agents", "code", runtime_paths) == bundled
    assert downloads.urls == []


@pytest.mark.asyncio
async def test_agent_named_after_stock_avatar_downloads_it_once(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """An agent named like a stock picture receives it, and later calls reuse the cached file."""
    first = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)
    second = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)

    assert first == second == _cache_path(runtime_paths, "code")
    assert first.is_file()
    assert downloads.urls == [managed_avatars._stock_avatar_url("code")]


@pytest.mark.asyncio
async def test_downloaded_avatar_is_reencoded_as_256px_png(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """Whatever the server sends, Matrix receives a clean 256x256 PNG."""
    downloads.payload = _image_bytes("JPEG", (640, 480))

    path = await managed_avatars.entity_avatar_path("agents", "mind", runtime_paths)

    assert path is not None
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == (256, 256)
        assert image.mode == "RGB"


@pytest.mark.asyncio
async def test_unknown_entities_get_stable_non_identity_picks(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """Every other entity gets a stable stock picture, never the Mind or Router identity."""
    first = await managed_avatars.entity_avatar_path("agents", "tax_helper", runtime_paths)
    second = await managed_avatars.entity_avatar_path("agents", "tax_helper", runtime_paths)
    for index in range(200):
        await managed_avatars.entity_avatar_path("teams", f"team_{index}", runtime_paths)

    assert first == second
    assert first is not None
    assert set(downloads.names) <= set(managed_avatars._STOCK_AVATAR_NAMES) - {"mind", "mind-logo", "router"}
    assert len(set(downloads.names)) > 10


@pytest.mark.asyncio
async def test_workspace_avatar_wins_without_network(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    tmp_path: Path,
) -> None:
    """A workspace file replaces the stock picture for the same agent."""
    override = tmp_path / "avatars" / "agents" / "mind.png"
    override.parent.mkdir(parents=True)
    override.write_bytes(b"png")

    assert await managed_avatars.entity_avatar_path("agents", "mind", runtime_paths) == override
    assert downloads.urls == []


@pytest.mark.asyncio
async def test_room_served_by_one_agent_uses_that_agents_avatar(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """The starter personal room shows its sole agent's picture."""
    config = _config(runtime_paths, agents={"mind": {"display_name": "Mind", "rooms": ["personal"]}})

    path = await managed_avatars.room_avatar_path("personal", config, runtime_paths)

    assert path == _cache_path(runtime_paths, "mind")
    assert downloads.names == ["mind"]


@pytest.mark.asyncio
async def test_room_served_by_one_team_uses_that_teams_avatar(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,  # noqa: ARG001
) -> None:
    """A room served only by a team shows the team's picture."""
    config = _config(
        runtime_paths,
        agents={"general": {"display_name": "General"}},
        teams={"ops": {"display_name": "Ops", "role": "Ops", "agents": ["general"], "rooms": ["war_room"]}},
    )

    path = await managed_avatars.room_avatar_path("war_room", config, runtime_paths)

    assert path is not None
    assert path == await managed_avatars.entity_avatar_path("teams", "ops", runtime_paths)


@pytest.mark.asyncio
async def test_shared_room_gets_stable_stock_avatar(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """A room served by several entities gets its own stable stock picture."""
    config = _config(
        runtime_paths,
        agents={
            "mind": {"display_name": "Mind", "rooms": ["lobby"]},
            "code": {"display_name": "Code", "rooms": ["lobby"]},
        },
    )

    path = await managed_avatars.room_avatar_path("lobby", config, runtime_paths)

    assert path is not None
    assert path == await managed_avatars.room_avatar_path("lobby", config, runtime_paths)
    assert len(downloads.names) == 1
    assert downloads.names[0] not in {"mind", "router"}


@pytest.mark.asyncio
async def test_room_avatar_file_wins(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    tmp_path: Path,
) -> None:
    """A room's own avatar file wins over its agent's picture."""
    override = tmp_path / "avatars" / "rooms" / "personal.png"
    override.parent.mkdir(parents=True)
    override.write_bytes(b"png")
    config = _config(runtime_paths, agents={"mind": {"display_name": "Mind", "rooms": ["personal"]}})

    assert await managed_avatars.room_avatar_path("personal", config, runtime_paths) == override
    assert downloads.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        httpx.ConnectError("offline"),
        ByteLimitExceededError("too big"),
        b"not an image",
    ],
    ids=["network", "oversize", "invalid-image"],
)
async def test_unavailable_stock_avatar_returns_none_and_warns(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    payload: bytes | BaseException,
) -> None:
    """Avatars are cosmetic, so a failed download leaves the entity without one."""
    downloads.payload = payload

    with capture_logs() as logs:
        path = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)

    assert path is None
    assert not _cache_path(runtime_paths, "code").exists()
    assert [(log["event"], log["avatar"]) for log in logs if log["log_level"] == "warning"] == [
        ("stock_avatar_unavailable", "code"),
    ]


@pytest.mark.asyncio
async def test_unwritable_cache_returns_none_and_warns(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,  # noqa: ARG001
) -> None:
    """A cache directory that cannot be created leaves the entity without an avatar."""
    blocker = runtime_paths.storage_root / "avatars" / "stock"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"not a directory")

    with capture_logs() as logs:
        path = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)

    assert path is None
    assert [log["event"] for log in logs if log["log_level"] == "warning"] == ["stock_avatar_unavailable"]


def _serve(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    """Route the real downloader's HTTP client through a mock transport."""
    monkeypatch.setattr(managed_avatars.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=handler))


@pytest.mark.asyncio
async def test_downloader_fetches_the_pinned_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real downloader returns the response body for the requested URL."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"image-bytes")

    _serve(monkeypatch, httpx.MockTransport(handler))
    url = managed_avatars._stock_avatar_url("code")

    assert await _real_download_stock_avatar(url) == b"image-bytes"
    assert requested == [url]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404), httpx.HTTPStatusError),
        (httpx.Response(302, headers={"location": "https://example.com/x.png"}), httpx.HTTPStatusError),
        (httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)), ByteLimitExceededError),
    ],
    ids=["not-found", "redirect", "oversize"],
)
async def test_downloader_rejects_bad_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    error: type[Exception],
) -> None:
    """Error statuses, redirects, and bodies over 8 MiB are refused."""
    _serve(monkeypatch, httpx.MockTransport(lambda _request: response))

    with pytest.raises(error):
        await _real_download_stock_avatar(managed_avatars._stock_avatar_url("code"))


def _code_bot(runtime_paths: constants_mod.RuntimePaths, *, avatar_url: str | None) -> bot_module.AgentBot:
    """Build the `code` agent bot whose Matrix profile reports the given avatar."""
    config = _config(runtime_paths, agents={"code": {"display_name": "Code"}})
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="code",
            user_id="@mindroom_code:localhost",
            display_name="Code",
            password=TEST_PASSWORD,
        ),
        storage_path=runtime_paths.storage_root,
        config=config,
        runtime_paths=runtime_paths,
        rooms=[],
    )
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_code:localhost"
    client.get_profile.return_value = nio.ProfileGetResponse(displayname="Code", avatar_url=avatar_url)
    bot.client = client
    return bot


@pytest.fixture
def set_user_avatar(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Capture profile avatar uploads instead of talking to Matrix."""
    set_avatar = AsyncMock(return_value=True)
    monkeypatch.setattr(bot_module, "set_user_avatar_from_file", set_avatar)
    return set_avatar


@pytest.mark.asyncio
async def test_bot_sets_resolved_avatar_when_profile_has_none(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    set_user_avatar: AsyncMock,
) -> None:
    """An agent without a profile picture receives its stock avatar."""
    bot = _code_bot(runtime_paths, avatar_url=None)

    await bot._set_avatar_if_available()

    set_user_avatar.assert_awaited_once_with(bot.client, _cache_path(runtime_paths, "code"))
    assert downloads.names == ["code"]


@pytest.mark.asyncio
async def test_bot_keeps_existing_profile_avatar_without_downloading(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    set_user_avatar: AsyncMock,
) -> None:
    """A profile that already has a picture never triggers a stock download."""
    bot = _code_bot(runtime_paths, avatar_url="mxc://localhost/custom")

    await bot._set_avatar_if_available()

    assert downloads.urls == []
    set_user_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_skips_avatar_when_none_is_available(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    set_user_avatar: AsyncMock,
) -> None:
    """An offline machine leaves the profile without a picture."""
    downloads.payload = httpx.ConnectError("offline")
    bot = _code_bot(runtime_paths, avatar_url=None)

    await bot._set_avatar_if_available()

    set_user_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_avatar_resolution_errors_never_escape(
    runtime_paths: constants_mod.RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    set_user_avatar: AsyncMock,
) -> None:
    """Avatars are cosmetic, so an unexpected resolver error cannot fail startup."""
    monkeypatch.setattr(bot_module, "entity_avatar_path", AsyncMock(side_effect=RuntimeError("boom")))
    bot = _code_bot(runtime_paths, avatar_url=None)

    await bot._set_avatar_if_available()

    set_user_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_managed_room_gets_its_single_agents_avatar(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly created room served by one agent shows that agent's stock avatar."""
    config = _config(runtime_paths, agents={"mind": {"display_name": "Mind", "rooms": ["personal"]}})
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.user_id = "@router:localhost"
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")
    client.room_get_state_event.return_value = nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND")
    monkeypatch.setattr(matrix_rooms, "create_room", AsyncMock(return_value="!new:localhost"))
    monkeypatch.setattr(matrix_rooms, "generate_room_topic_ai", AsyncMock(return_value="topic"))
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", AsyncMock(return_value=True))
    set_room_avatar = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "set_room_avatar_from_file", set_room_avatar)

    room_id = await matrix_rooms._ensure_room_exists(
        client=client,
        room_key="personal",
        config=config,
        runtime_paths=runtime_paths,
        room_policy=resolve_room_policy(config, "personal"),
    )

    assert room_id == "!new:localhost"
    set_room_avatar.assert_awaited_once_with(client, "!new:localhost", _cache_path(runtime_paths, "mind"))
    assert downloads.names == ["mind"]


@pytest.mark.asyncio
async def test_room_with_avatar_is_kept_without_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    """A room that already has a picture never resolves or downloads a default."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"url": "mxc://localhost/custom"},
        event_type="m.room.avatar",
        state_key="",
        room_id="!room:localhost",
    )
    resolve_avatar = AsyncMock(return_value=Path("unused.png"))
    set_room_avatar = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "set_room_avatar_from_file", set_room_avatar)

    await matrix_rooms._set_room_avatar(client, "!room:localhost", resolve_avatar=resolve_avatar, context="test")

    resolve_avatar.assert_not_awaited()
    set_room_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_space_avatar_prefers_bundled_then_falls_back_to_stock(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bundled root-space file is used when present; without it the stock mind-logo is downloaded."""
    bundled = tmp_path / "bundled" / "spaces" / "root_space.png"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(_image_bytes())
    assert await managed_avatars.root_space_avatar_path(runtime_paths) == bundled
    assert downloads.urls == []

    monkeypatch.setattr(constants_mod, "_bundled_avatars_dir", lambda: tmp_path / "missing")

    fallback = await managed_avatars.root_space_avatar_path(runtime_paths)
    assert fallback == _cache_path(runtime_paths, "mind-logo")
    assert downloads.names == ["mind-logo"]


@pytest.mark.parametrize("data", [b"not an image", _image_bytes()[:64]], ids=["garbage", "truncated"])
def test_undecodable_images_raise_value_error(data: bytes) -> None:
    """Pillow decode failures surface as ValueError so the event loop never imports Pillow."""
    with pytest.raises(ValueError, match="stock avatar"):
        managed_avatars._normalized_png(data)


@pytest.mark.asyncio
async def test_failed_download_creates_negative_cache_marker(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """A failed download creates a marker file to skip retries for 24 hours."""
    downloads.payload = httpx.ConnectError("offline")

    with capture_logs() as logs:
        path = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)

    assert path is None
    marker = managed_avatars._negative_cache_marker(_cache_path(runtime_paths, "code"))
    assert marker.is_file()
    assert [log["event"] for log in logs if log["log_level"] == "warning"] == ["stock_avatar_unavailable"]


@pytest.mark.asyncio
async def test_recent_failure_skips_download_quietly(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,
) -> None:
    """A download that failed recently is not retried and does not warn again on later starts."""
    cache_path = _cache_path(runtime_paths, "code")
    managed_avatars._record_download_failure(cache_path)

    with capture_logs() as logs:
        first = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)
        second = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)

    assert first is None
    assert second is None
    assert downloads.urls == []
    assert [log for log in logs if log["log_level"] == "warning"] == []
    assert [log["event"] for log in logs] == ["stock_avatar_unavailable_cached"] * 2


@pytest.mark.asyncio
async def test_successful_download_clears_negative_cache_marker(
    runtime_paths: constants_mod.RuntimePaths,
    downloads: _FakeDownloads,  # noqa: ARG001
) -> None:
    """A successful download after a failure clears the negative cache marker."""
    cache_path = _cache_path(runtime_paths, "code")
    managed_avatars._record_download_failure(cache_path)
    marker = managed_avatars._negative_cache_marker(cache_path)
    assert marker.is_file()
    # Make the marker appear old enough to trigger a retry.
    old_mtime = marker.stat().st_mtime - (25 * 3600)
    os.utime(marker, (old_mtime, old_mtime))

    path = await managed_avatars.entity_avatar_path("agents", "code", runtime_paths)

    assert path is not None
    assert not marker.exists()
