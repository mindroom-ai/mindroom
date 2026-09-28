"""Choose the avatar each managed agent, team, and room displays in Matrix."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import time
import warnings
from typing import TYPE_CHECKING, Literal

import httpx

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.constants import resolve_avatar_path
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# Pinning one commit of the assets repository keeps every stock image immutable and reproducible.
_STOCK_AVATAR_COMMIT = "f685f674404ebb9709d707f49298450211a7d603"
_STOCK_AVATAR_URL = "https://raw.githubusercontent.com/mindroom-ai/assets/{commit}/avatars/painted/agents/{name}.png"
_STOCK_AVATAR_NAMES = (
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
# These stock avatars belong to one identity and are never handed out as stable picks.
_IDENTITY_AVATARS = frozenset({"mind", "mind-logo", "router"})
_STOCK_POOL = tuple(name for name in _STOCK_AVATAR_NAMES if name not in _IDENTITY_AVATARS)
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 20.0
_AVATAR_SIZE = (256, 256)
_FAILED_DOWNLOAD_RETRY_SECONDS = 24 * 60 * 60
_FAILED_MARKER_SUFFIX = ".failed"


def _stock_avatar_url(name: str) -> str:
    return _STOCK_AVATAR_URL.format(commit=_STOCK_AVATAR_COMMIT, name=name)


async def _download_stock_avatar(url: str) -> bytes:
    """Fetch one stock image, refusing redirects, error statuses, and oversized bodies."""
    async with (
        httpx.AsyncClient(follow_redirects=False, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        return await collect_bounded_bytes(response.aiter_bytes(), max_bytes=_MAX_DOWNLOAD_BYTES)


def _normalized_png(data: bytes) -> bytes:
    """Decode an untrusted image and re-encode it as a clean 256px RGB PNG.

    Pillow decode failures are raised as ValueError, so callers never import Pillow for its exception types.
    """
    # Pillow can import NumPy; defer it to avatar work off the event loop.
    from PIL import Image  # noqa: PLC0415

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                normalized = image.convert("RGB").resize(_AVATAR_SIZE, Image.Resampling.LANCZOS)
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        msg = f"Undecodable stock avatar: {exc}"
        raise ValueError(msg) from exc
    output = io.BytesIO()
    normalized.save(output, format="PNG")
    return output.getvalue()


def _write_cache_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_write_bytes_at(directory_fd, path.name, payload)
    finally:
        os.close(directory_fd)


def _stock_cache_dir(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / "avatars" / "stock"


def _download_failed_recently(marker: Path) -> bool:
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        # A missing marker, or a cache path blocked by a non-directory, means no recent failure is on record.
        return False
    return age < _FAILED_DOWNLOAD_RETRY_SECONDS


def clear_failed_stock_downloads(runtime_paths: RuntimePaths) -> None:
    """Forget recent stock download failures so the next avatar resolution retries them."""
    for marker in _stock_cache_dir(runtime_paths).glob(f"*{_FAILED_MARKER_SUFFIX}"):
        marker.unlink(missing_ok=True)


async def _stock_avatar_path(name: str, runtime_paths: RuntimePaths) -> Path | None:
    """Return the cached stock image, downloading it once; avatars are cosmetic, so failures return None."""
    path = _stock_cache_dir(runtime_paths) / f"{_STOCK_AVATAR_COMMIT[:12]}-{name}.png"
    if path.is_file():
        return path
    marker = path.with_suffix(_FAILED_MARKER_SUFFIX)
    if _download_failed_recently(marker):
        # The failure was already reported when it happened; retry after the window or an explicit avatar sync.
        logger.debug("stock_avatar_unavailable_cached", avatar=name)
        return None
    try:
        data = await _download_stock_avatar(_stock_avatar_url(name))
        payload = await asyncio.to_thread(_normalized_png, data)
        await asyncio.to_thread(_write_cache_file, path, payload)
    except (httpx.HTTPError, ByteLimitExceededError, ValueError, OSError) as exc:
        logger.warning("stock_avatar_unavailable", avatar=name, error=str(exc))
        with contextlib.suppress(OSError):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        return None
    return path


def _stock_pick(seed: str) -> str:
    """Pick a stable stock avatar for one entity so it keeps the same picture across restarts."""
    digest = hashlib.sha256(seed.encode()).digest()
    return _STOCK_POOL[int.from_bytes(digest[:8], "big") % len(_STOCK_POOL)]


async def entity_avatar_path(
    entity_type: Literal["agents", "teams"],
    entity_name: str,
    runtime_paths: RuntimePaths,
) -> Path | None:
    """Return the avatar for an agent or team: its own file, a matching stock image, or a stable stock pick."""
    avatar_path = resolve_avatar_path(entity_type, entity_name, runtime_paths)
    if avatar_path.exists():
        return avatar_path
    if entity_type == "agents" and entity_name in _STOCK_AVATAR_NAMES:
        return await _stock_avatar_path(entity_name, runtime_paths)
    return await _stock_avatar_path(_stock_pick(f"{entity_type}/{entity_name}"), runtime_paths)


async def room_avatar_path(room_key: str, config: Config, runtime_paths: RuntimePaths) -> Path | None:
    """Return the avatar for a managed room.

    A room's own file wins.
    A room served by exactly one configured agent or team shows that entity's avatar.
    Any other room receives a stable stock avatar.
    """
    avatar_path = resolve_avatar_path("rooms", room_key, runtime_paths)
    if avatar_path.exists():
        return avatar_path
    entities: list[tuple[Literal["agents", "teams"], str]] = [
        ("agents", name) for name, agent in config.agents.items() if room_key in agent.rooms
    ]
    entities.extend(("teams", name) for name, team in config.teams.items() if room_key in team.rooms)
    if len(entities) == 1:
        return await entity_avatar_path(*entities[0], runtime_paths)
    return await _stock_avatar_path(_stock_pick(f"rooms/{room_key}"), runtime_paths)


async def root_space_avatar_path(runtime_paths: RuntimePaths) -> Path | None:
    """Return the root-space avatar: a workspace override, the bundled file, or the stock mind-logo fallback.

    Source checkouts and Docker images ship with avatars/spaces/root_space.png.
    Wheel installs (uvx) lack bundled files and fall back to the stock mind-logo image.
    """
    avatar_path = resolve_avatar_path("spaces", "root_space", runtime_paths)
    if avatar_path.exists():
        return avatar_path
    return await _stock_avatar_path("mind-logo", runtime_paths)
