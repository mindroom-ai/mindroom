"""Image delivery preserves content while bounding workspace access and payloads."""

import io
import json
import os
import random
from pathlib import Path

import pytest
from agno.tools.function import ToolResult
from PIL import Image

from mindroom import media_delivery


def image_bytes(size: tuple[int, int] = (80, 60), *, image_format: str = "PNG") -> bytes:
    """Image bytes."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (37, 119, 211)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_image_result_carries_image_bytes_separately() -> None:
    """Image result carries image bytes separately."""
    data = image_bytes()
    result = media_delivery.image_result(data, metadata={"path": "plots/result.png"})
    assert isinstance(result, ToolResult)
    assert result.images
    assert result.images[0].content == data
    assert result.images[0].mime_type == "image/png"
    receipt = json.loads(result.content)
    assert receipt["path"] == "plots/result.png"
    assert receipt["view_status"] == "ready"
    assert receipt["width"] == 80
    assert receipt["height"] == 60
    assert "base64" not in result.content


def test_large_image_is_resized_with_disclosure() -> None:
    """Large image is resized with disclosure."""
    result = media_delivery.image_result(image_bytes((3000, 1000)), metadata={})
    assert result.images
    receipt = json.loads(result.content)
    assert receipt["resized"] is True
    assert receipt["original_width"] == 3000
    assert max(receipt["width"], receipt["height"]) <= 2048
    with Image.open(io.BytesIO(result.images[0].content)) as decoded:
        assert decoded.size == (receipt["width"], receipt["height"])


@pytest.mark.parametrize(("size", "orientation"), [((3000, 1000), 1), ((80, 60), 6)])
def test_cmyk_jpeg_can_be_resized_and_oriented(size: tuple[int, int], orientation: int) -> None:
    """JPEG color modes must remain viewable when preparation re-encodes pixels."""
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    Image.new("CMYK", size, (255, 0, 0, 0)).save(buffer, format="JPEG", exif=exif)

    result = media_delivery.image_result(buffer.getvalue(), metadata={})

    assert result.images
    receipt = json.loads(result.content)
    assert receipt["view_status"] == "ready"
    assert receipt["converted"] is True
    with Image.open(io.BytesIO(result.images[0].content)) as decoded:
        assert decoded.mode == "RGB"
        assert decoded.size == (receipt["width"], receipt["height"])
        assert decoded.getpixel((0, 0)) == (0, 255, 255)


def test_oversized_transparent_image_does_not_reveal_hidden_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload reduction must not expose RGB values hidden by an alpha channel."""
    pixels = random.Random(0).randbytes(128 * 128 * 3)  # noqa: S311 - deterministic visual fixture
    source = Image.frombytes("RGB", (128, 128), pixels).convert("RGBA")
    source.putalpha(0)
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    monkeypatch.setattr(media_delivery, "_MAX_IMAGE_BYTES", 20_000)

    result = media_delivery.image_result(buffer.getvalue(), metadata={"path": "transparent.png"})

    assert not result.images
    receipt = json.loads(result.content)
    assert receipt["view_status"] == "error"
    assert "payload limit" in receipt["message"]
    assert receipt["path"] == "transparent.png"


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n"])
def test_invalid_image_returns_explicit_error(data: bytes) -> None:
    """Invalid image returns explicit error."""
    result = media_delivery.image_result(data, metadata={"path": "result.png"})
    assert not result.images
    assert json.loads(result.content)["view_status"] == "error"
    assert json.loads(result.content)["path"] == "result.png"


def test_oversize_bytes_rejected_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversize bytes rejected before decode."""
    monkeypatch.setattr(media_delivery, "MAX_SOURCE_BYTES", 10)
    result = media_delivery.image_result(image_bytes(), metadata={})
    assert not result.images
    assert "limit" in json.loads(result.content)["message"]


def test_path_view_uses_workspace_and_retains_file(tmp_path: Path) -> None:
    """Path view uses workspace and retains file."""
    path = tmp_path / "fixture.png"
    data = image_bytes()
    path.write_bytes(data)
    result = media_delivery.view_agent_image("fixture.png", workspace=tmp_path, file_access="workspace")
    assert result.images
    assert result.images[0].content == data
    assert path.read_bytes() == data
    assert json.loads(result.content)["path"] == "fixture.png"


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "missing", "directory"])
def test_path_view_rejects_unavailable_or_unauthorized_files(tmp_path: Path, kind: str) -> None:
    """Path view rejects unavailable or unauthorized files."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    (workspace / "link.png").symlink_to(outside)
    requested = {
        "traversal": "../outside.png",
        "absolute": str(outside),
        "symlink": "link.png",
        "missing": "missing.png",
        "directory": ".",
    }[kind]
    result = media_delivery.view_agent_image(requested, workspace=workspace, file_access="workspace")
    assert not result.images
    assert json.loads(result.content)["view_status"] == "error"


def test_animation_is_reduced_to_first_frame_with_disclosure() -> None:
    """Animation cannot silently imply that every frame reached the model."""
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    result = media_delivery.image_result(buffer.getvalue(), metadata={})
    assert result.images
    assert json.loads(result.content)["first_frame_only"] is True
    with Image.open(io.BytesIO(result.images[0].content)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_decoded_pixel_limit_rejects_before_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compressed images cannot bypass the decoded size limit."""
    monkeypatch.setattr(media_delivery, "_MAX_PIXELS", 100)
    result = media_delivery.image_result(image_bytes(), metadata={})
    assert not result.images
    assert "pixel" in result.content


def test_viewed_image_has_persistable_identity() -> None:
    """The bounded view marker survives serialization for history replay."""
    result = media_delivery.image_result(image_bytes(), metadata={})
    assert result.images
    assert result.images[0].id.startswith("mindroom_viewed_")


def test_path_view_rejects_symlink_swap_during_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace process cannot swap a validated file for an outside symlink."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "image.png"
    path.write_bytes(image_bytes())
    outside = tmp_path / "private.png"
    outside.write_bytes(image_bytes((17, 13)))
    original_open = os.open

    def swap_open(file: object, flags: int, *args: object, **kwargs: object) -> int:
        if str(file) == "image.png":
            path.unlink()
            path.symlink_to(outside)
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_open)
    result = media_delivery.view_agent_image("image.png", workspace=workspace, file_access="workspace")
    assert not result.images
    assert json.loads(result.content)["view_status"] == "error"
