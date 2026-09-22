"""Browser-generated files must be published through the workspace boundary."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.custom_tools.browser import BrowserTools


def test_default_browser_output_rejects_external_directory_link(tmp_path: Path) -> None:
    """Default output setup cannot create artifacts in a linked outside directory."""
    storage = tmp_path / "state"
    storage.mkdir()
    outside = tmp_path / "outside"
    (storage / "browser").symlink_to(outside, target_is_directory=True)
    runtime = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)
    tool = BrowserTools(runtime)

    with pytest.raises(ValueError, match="within its authorized root"):
        tool._resolve_output_dir()
    assert not outside.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["screenshot", "pdf", "download"])
async def test_browser_outputs_reject_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """An output directory changed during browser work cannot redirect file writes."""
    workspace = tmp_path / "workspace"
    output = workspace / "browser"
    output.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    staged = tmp_path / "browser-managed-download"
    staged.write_bytes(b"browser data")
    runtime = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    tool = BrowserTools(runtime)
    tool.bind_worker_display(":99", workspace)

    def swap() -> None:
        output.rename(workspace / "old-browser")
        output.symlink_to(outside, target_is_directory=True)

    async def capture(*, path: str | None = None, **_kwargs: object) -> bytes:
        swap()
        if path is not None:
            Path(path).write_bytes(b"browser data")
        return b"browser data"

    async def save_as(destination: Path) -> None:
        swap()
        destination.write_bytes(b"browser data")

    async def download_path() -> Path:
        swap()
        return staged

    page = SimpleNamespace(screenshot=capture, pdf=capture)
    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=object()))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab", SimpleNamespace(page=page))))
    if action == "screenshot":
        operation = tool._screenshot(
            profile_name="mindroom",
            target_id=None,
            full_page=False,
            ref=None,
            element=None,
            image_type=None,
        )
    elif action == "pdf":
        operation = tool._pdf("mindroom", None)
    else:
        operation = tool._save_worker_download(
            cast(
                "Any",
                SimpleNamespace(
                    suggested_filename="download.txt",
                    save_as=save_as,
                    path=download_path,
                ),
            )
        )
    with pytest.raises((OSError, ValueError)):
        await operation
    assert not list(outside.iterdir())
