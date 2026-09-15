"""Allowlisted model catalog and bounded Matrix raster icon publication."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import warnings
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict
from urllib.parse import urlsplit

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.identity import valid_matrix_server_name

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["ModelCatalog", "ModelCatalogEntry"]

_MAX_ICON_BYTES = 1024 * 1024
_MAX_CACHE_ENTRIES = 256
_RASTER_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
logger = get_logger(__name__)


class ModelCatalogEntry(TypedDict):
    """Allowlisted JSON fields published for one configured model."""

    key: str
    display_name: str
    provider: str
    id: str
    icon_url: NotRequired[str]


def _matrix_uri(value: str) -> bool:
    try:
        uri = urlsplit(value)
        return (
            uri.scheme == "mxc"
            and valid_matrix_server_name(uri.netloc)
            and uri.path.startswith("/")
            and uri.path.count("/") == 1
            and len(uri.path) > 1
            and not uri.query
            and not uri.fragment
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        return False


def _read_raster(path: Path, config_dir: Path) -> tuple[bytes, str] | None:
    """Bound reads before decoding and verify actual bytes rather than extensions."""
    # Pillow can import NumPy; defer it to requested icon work off the event loop.
    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        directory = config_dir.resolve()
        path = path.resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(_MAX_ICON_BYTES + 1)
        if len(data) > _MAX_ICON_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime = _RASTER_TYPES.get(image.format or "")
                if mime is None:
                    return None
                image.verify()
            # verify() is a no-op for some decoders; load pixels as well.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        OSError,
        RuntimeError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    return data, mime


class ModelCatalog:
    """Own icon upload serialization and cache for one runtime Matrix client."""

    def __init__(self, *, client: nio.AsyncClient, runtime_paths: RuntimePaths) -> None:
        self._client = client
        self._config_dir = runtime_paths.config_path.parent
        self._uploads: OrderedDict[str, str] = OrderedDict()
        self._upload_lock = asyncio.Lock()

    async def _icon(self, value: str | None, key: str) -> str | None:
        if not value or _matrix_uri(value):
            return value or None
        # Never fetch network URLs or expose a configured filesystem path.
        if "://" in value or Path(value).is_absolute():
            logger.warning("model_catalog_icon_unavailable", model=key)
            return None
        raster = await asyncio.to_thread(_read_raster, self._config_dir / value, self._config_dir)
        if raster is None:
            logger.warning("model_catalog_icon_unavailable", model=key)
            return None
        data, mime = raster
        digest = hashlib.sha256(data).hexdigest()
        async with self._upload_lock:
            if digest in self._uploads:
                self._uploads.move_to_end(digest)
                return self._uploads[digest]
            with io.BytesIO(data) as stream:
                response, _ = await self._client.upload(
                    stream,
                    content_type=mime,
                    filename="model-icon",
                    filesize=len(data),
                )
            if not isinstance(response, nio.UploadResponse) or not _matrix_uri(response.content_uri):
                logger.warning("model_catalog_icon_upload_failed", model=key)
                return None
            self._uploads[digest] = response.content_uri
            if len(self._uploads) > _MAX_CACHE_ENTRIES:
                self._uploads.popitem(last=False)
            return response.content_uri

    async def snapshot(self, config: Config) -> tuple[list[ModelCatalogEntry], str]:
        """Publish only model labels/identifiers and Matrix-hosted icon references."""
        entries: list[ModelCatalogEntry] = []
        for key, model in sorted(config.models.items()):
            entry: ModelCatalogEntry = {
                "key": key,
                "display_name": model.display_name or key,
                "provider": model.provider,
                "id": model.id,
            }
            icon = await self._icon(model.icon, key)
            if icon is not None:
                entry["icon_url"] = icon
            entries.append(entry)
        serialized = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return entries, hashlib.sha256(serialized).hexdigest()
