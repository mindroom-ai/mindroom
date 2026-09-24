"""Bounded image preparation shared by file viewing and browser captures.

Only image media is supported here today; callers retain source artifacts and
supply their own authorized resolution of files and attachment handles.
"""

from __future__ import annotations

import io
import json
import os
import warnings
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.media import Image

from mindroom.path_confinement import open_regular_file_within_root, resolve_path_within_root

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import ToolResult
    from PIL.Image import Image as PillowImage

    from mindroom.file_access import AuthorizedFile

VIEWED_IMAGE_ID_PREFIX = "mindroom_viewed_"
MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_PIXELS = 40_000_000
_MAX_EDGE = 2048
_SUPPORTED_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}


def media_error(message: str, *, metadata: dict[str, object]) -> ToolResult:
    """Keep the artifact receipt while explicitly reporting unsuccessful viewing."""
    from agno.tools.function import ToolResult  # noqa: PLC0415

    return ToolResult(content=json.dumps({**metadata, "view_status": "error", "message": message}, sort_keys=True))


def _encode_image(image: PillowImage) -> tuple[bytes, str]:
    if image.mode == "CMYK":
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    if len(data) <= _MAX_IMAGE_BYTES or "A" in image.getbands() or "transparency" in image.info:
        return data, "image/png"
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=85)
    return output.getvalue(), "image/jpeg"


def image_result(data: bytes, *, metadata: dict[str, object]) -> ToolResult:  # noqa: PLR0911
    """Deliver supported image bytes as model media with disclosed transformations."""
    from agno.tools.function import ToolResult  # noqa: PLC0415

    if not data:
        return media_error("Image file is empty.", metadata=metadata)
    if len(data) > MAX_SOURCE_BYTES:
        return media_error(f"Image exceeds the {MAX_SOURCE_BYTES}-byte source limit.", metadata=metadata)

    # Keep the image decoder lazy during slim worker startup.
    from PIL import Image as PILImage  # noqa: PLC0415
    from PIL import ImageOps, UnidentifiedImageError  # noqa: PLC0415

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(data)) as source:
                original_width, original_height = source.size
                if source.format not in _SUPPORTED_FORMATS:
                    return media_error("Viewing supports PNG, JPEG, GIF and WebP images.", metadata=metadata)
                if original_width * original_height > _MAX_PIXELS:
                    return media_error(f"Image exceeds the {_MAX_PIXELS}-pixel decoded limit.", metadata=metadata)
                animated = source.n_frames > 1 if source.format in {"GIF", "WEBP", "PNG"} else False
                source.seek(0)
                oriented = ImageOps.exif_transpose(source)
                assert oriented is not None
                original_format = source.format
                resized = max(oriented.size) > _MAX_EDGE
                oriented.thumbnail((_MAX_EDGE, _MAX_EDGE), PILImage.Resampling.LANCZOS)
                converted = original_format not in {"PNG", "JPEG"} or bool(source.getexif().get(274))
                if resized or animated or converted or len(data) > _MAX_IMAGE_BYTES:
                    data, mime_type = _encode_image(oriented)
                    converted = True
                else:
                    mime_type = "image/png" if original_format == "PNG" else "image/jpeg"
                if len(data) > _MAX_IMAGE_BYTES:
                    return media_error(
                        f"Prepared image exceeds the {_MAX_IMAGE_BYTES}-byte payload limit.",
                        metadata=metadata,
                    )
                width, height = oriented.size
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        PILImage.DecompressionBombError,
        PILImage.DecompressionBombWarning,
    ):
        return media_error("Image cannot be decoded safely as PNG, JPEG, GIF or WebP.", metadata=metadata)

    receipt = {
        **metadata,
        "view_status": "ready",
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "original_width": original_width,
        "original_height": original_height,
        "size_bytes": len(data),
        "resized": resized,
        "converted": converted,
        "first_frame_only": animated,
    }
    return ToolResult(
        content=json.dumps(receipt, sort_keys=True),
        images=[Image(id=f"{VIEWED_IMAGE_ID_PREFIX}{uuid4().hex}", content=data, mime_type=mime_type)],
    )


def _read_image_within_root(root: Path, relative: Path) -> bytes:
    """Read through directory descriptors so path swaps cannot escape the authorizing root."""
    with (
        open_regular_file_within_root(root, relative) as descriptor,
        os.fdopen(descriptor, "rb", closefd=False) as file,
    ):
        if os.fstat(descriptor).st_size > MAX_SOURCE_BYTES:
            message = f"Image exceeds the {MAX_SOURCE_BYTES}-byte source limit."
            raise ValueError(message)
        return file.read(MAX_SOURCE_BYTES + 1)


def view_image_path(path: str, *, workspace: Path) -> ToolResult:
    """Read one regular image confined to an already-authorized workspace."""
    metadata: dict[str, object] = {"path": path}
    if not isinstance(path, str) or not path.strip():
        return media_error("path must be a non-empty string.", metadata=metadata)
    try:
        root = workspace.resolve(strict=True)
        resolved = resolve_path_within_root(root, path, symlinks="internal", strict=True)
        relative = resolved.relative_to(root)
        if not relative.parts:
            return media_error("Image path must name a regular file.", metadata=metadata)
        metadata["path"] = relative.as_posix()
        data = _read_image_within_root(root, relative)
    except ValueError as exc:
        return media_error(str(exc), metadata=metadata)
    except (OSError, RuntimeError):
        return media_error(
            "Image file is missing, inaccessible, or outside the authorized workspace.",
            metadata=metadata,
        )
    return image_result(data, metadata=metadata)


def view_authorized_image(authorized: AuthorizedFile) -> ToolResult:
    """Read one regular image a caller authorized under the agent's file access."""
    metadata: dict[str, object] = {"path": str(authorized.path)}
    try:
        data = _read_image_within_root(authorized.root, authorized.path.relative_to(authorized.root))
    except ValueError as exc:
        return media_error(str(exc), metadata=metadata)
    except OSError:
        return media_error("Image file is missing or inaccessible.", metadata=metadata)
    return image_result(data, metadata=metadata)
