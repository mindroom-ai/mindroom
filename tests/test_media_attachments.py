"""Tool image retention confines writes and registration to authorized storage."""

import json
import os
from pathlib import Path

import pytest

from mindroom.media_delivery import image_result
from mindroom.tool_system.media_attachments import finalize_tool_media
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_attachments_tool import _tool_context
from tests.test_media_delivery import image_bytes


@pytest.mark.parametrize("directory", ["incoming_media", "attachments"])
def test_retention_rejects_symlinked_storage_directory(tmp_path: Path, directory: str) -> None:
    """Neither image nor metadata persistence may follow an outside directory link."""
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage / directory).symlink_to(outside, target_is_directory=True)

    with tool_runtime_context(_tool_context(storage)):
        result = finalize_tool_media(image_result(image_bytes(), metadata={}))

    receipt = json.loads(result.content)
    assert result.images
    assert "attachment_warning" in receipt
    assert "attachment_id" not in receipt
    assert list(outside.iterdir()) == []
    assert not list((storage / "attachments").glob("*.json"))


def test_retention_rejects_parent_swap_before_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A swap after opening the image cannot register replacement bytes outside storage."""
    storage = tmp_path / "storage"
    media = storage / "incoming_media"
    media.mkdir(parents=True)
    original_media = storage / "original-media"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = os.open
    swapped = False

    def swap_parent(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and not swapped:
            swapped = True
            media.rename(original_media)
            media.symlink_to(outside, target_is_directory=True)
            if str(path).endswith(".png"):
                (outside / Path(path).name).write_bytes(b"outside replacement")
        return descriptor

    monkeypatch.setattr(os, "open", swap_parent)

    with tool_runtime_context(_tool_context(storage)):
        result = finalize_tool_media(image_result(image_bytes(), metadata={}))

    assert swapped
    receipt = json.loads(result.content)
    assert result.images
    assert "attachment_warning" in receipt
    assert "attachment_id" not in receipt
    assert not list((storage / "attachments").glob("*.json"))
    assert list(original_media.iterdir()) == []
    assert all(path.read_bytes() == b"outside replacement" for path in outside.iterdir())


def test_retention_rejects_parent_swap_during_metadata_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final publication race cannot expose a handle resolving through a replacement parent."""
    media = tmp_path / "incoming_media"
    media.mkdir()
    original_media = tmp_path / "original-media"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_replace = os.replace

    def swap_on_publication(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if str(destination).endswith(".json"):
            media.rename(original_media)
            media.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(os, "replace", swap_on_publication)

    with tool_runtime_context(_tool_context(tmp_path)):
        result = finalize_tool_media(image_result(image_bytes(), metadata={}))

    receipt = json.loads(result.content)
    assert result.images
    assert "attachment_warning" in receipt
    assert "attachment_id" not in receipt
    assert list(outside.iterdir()) == []
    assert list(original_media.iterdir()) == []
    assert not list((tmp_path / "attachments").glob("*.json"))


def test_retention_cleans_image_after_metadata_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed registration removes both the unpublished metadata and retained image."""
    original_replace = os.replace

    def fail_metadata_publication(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if str(destination).endswith(".json"):
            message = "Metadata write failed"
            raise OSError(message)
        original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", fail_metadata_publication)

    with tool_runtime_context(_tool_context(tmp_path)):
        result = finalize_tool_media(image_result(image_bytes(), metadata={}))

    receipt = json.loads(result.content)
    assert result.images
    assert "attachment_warning" in receipt
    assert "attachment_id" not in receipt
    assert list((tmp_path / "incoming_media").iterdir()) == []
    assert list((tmp_path / "attachments").iterdir()) == []


def test_retention_does_not_traverse_replaced_directory_after_descriptor_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing retention cannot run path-based cleanup through a newly swapped directory."""
    media = tmp_path / "incoming_media"
    media.mkdir()
    original_media = tmp_path / "original-media"
    outside = tmp_path / "outside"
    outside.mkdir()
    existing = outside / "existing.png"
    existing.write_bytes(b"unrelated existing image")
    os.utime(existing, (1, 1))
    original_close = os.close
    swapped = False

    def swap_after_close(descriptor: int) -> None:
        nonlocal swapped
        original_close(descriptor)
        if not swapped and list((tmp_path / "attachments").glob("*.json")):
            swapped = True
            media.rename(original_media)
            media.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(os, "close", swap_after_close)

    with tool_runtime_context(_tool_context(tmp_path)):
        result = finalize_tool_media(image_result(image_bytes(), metadata={}))

    assert swapped
    assert result.images
    assert existing.read_bytes() == b"unrelated existing image"
