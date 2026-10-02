"""Contract every tool that follows the agent ``file_access`` setting must satisfy.

Each probe drives one tool's real path entry point and reports whether it read the requested file.
A tool declared ``ToolFileAccess.AGENT`` must appear here, so a new path tool cannot skip the contract.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, BinaryIO

import pytest

import mindroom.custom_tools.agno_compat_moviepy as moviepy_module
import mindroom.custom_tools.attachments as attachments_module
import mindroom.custom_tools.browser as browser_module
import mindroom.custom_tools.coding as coding_module
import mindroom.custom_tools.e2b as e2b_module
import mindroom.custom_tools.gmail as gmail_module
import mindroom.custom_tools.google_drive as google_drive_module
import mindroom.media_delivery as media_delivery_module
import mindroom.tools.agno_compat_airflow as airflow_module
import mindroom.tools.agno_compat_groq as groq_module
import mindroom.tools.agno_compat_openai as openai_module
import mindroom.tools.file as file_tool_module
import mindroom.tools.path_safety as path_safety_module
from mindroom.attachments import load_attachment
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.agno_compat_moviepy import MindRoomMoviePyVideoTools
from mindroom.custom_tools.attachments import AttachmentTools, resolve_send_attachments
from mindroom.custom_tools.coding import CodingTools
from mindroom.custom_tools.e2b import MindRoomE2BTools
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tools.agno_compat_airflow import MindRoomAirflowTools
from mindroom.tools.agno_compat_groq import MindRoomGroqTools
from mindroom.tools.agno_compat_openai import MindRoomOpenAITools
from mindroom.tools.file import file_tools
from tests.test_attachments_tool import _tool_context
from tests.test_browser_upload_safety import _capture_uploads, _upload, _upload_tool
from tests.test_e2b_tools import _FakeSandbox
from tests.test_google_drive_oauth_tool import (
    _FakeDriveService,
    _FakeMediaIoBaseUpload,
    _runtime_paths_with_google_drive_client,
    _valid_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

    from agno.tools.file import FileTools

    from mindroom.config.models import FileAccess

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=",
)
_TEXT = "contract text\n"


@dataclass(frozen=True)
class _ToolProbe:
    tool_name: str
    entry_point: str
    read: Callable[[Path, pytest.MonkeyPatch, Path, FileAccess, str], Awaitable[bool]]
    resolver_module: ModuleType | None
    """Module whose ``resolve_agent_file`` the probe calls; None for tools that run in a worker by default."""
    filename: str = "doc.png"


async def _register_attachment(
    tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    tool = AttachmentTools(tool_output_workspace_root=workspace, file_access=file_access)
    with tool_runtime_context(_tool_context(tmp_path)):
        payload = json.loads(await tool.register_attachment(raw_path))
    if payload["status"] != "ok":
        return False
    attachment = load_attachment(tmp_path, payload["attachment_id"])
    return attachment is not None and attachment.local_path.read_bytes() == _PNG


async def _send_attachment_path(
    tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    _attachments, _ids, newly_registered, error = resolve_send_attachments(
        _tool_context(tmp_path),
        attachment_ids=[],
        attachment_file_paths=[raw_path],
        workspace_root=workspace,
        file_access=file_access,
    )
    if error is not None or not newly_registered:
        return False
    attachment = load_attachment(tmp_path, newly_registered[0])
    return attachment is not None and attachment.local_path.read_bytes() == _PNG


async def _view_file(
    _tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    result = media_delivery_module.view_agent_image(raw_path, workspace=workspace, file_access=file_access)
    return bool(result.images)


async def _stage_gmail_attachment(
    tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    staging = tmp_path / "gmail-staging"
    staging.mkdir(exist_ok=True)
    try:
        staged = gmail_module._stage_attachments(workspace, [raw_path], staging, file_access=file_access)
    except ValueError:
        return False
    return [Path(path).read_bytes() for path in staged] == [_PNG]


async def _upload_to_google_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    monkeypatch.setattr("mindroom.custom_tools.google_drive.MediaIoBaseUpload", _FakeMediaIoBaseUpload)
    tool = GoogleDriveTools(
        runtime_paths=_runtime_paths_with_google_drive_client(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=_valid_credentials(),
        tool_output_workspace_root=workspace,
        file_access=file_access,
    )
    service = _FakeDriveService()
    tool.service = service
    result = json.loads(tool.upload_file(raw_path))
    if "error" in result:
        return False
    assert service.files_resource.create_kwargs is not None
    return service.files_resource.create_kwargs["media_body"].content == _PNG


async def _upload_in_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    tool, consumer, _root = _upload_tool(tmp_path, monkeypatch, workspace_root=workspace, file_access=file_access)
    consumed = _capture_uploads(consumer)
    try:
        await _upload(tool, [raw_path])
    except (OSError, ValueError):
        return False
    finally:
        await tool.aclose()
    return consumed == [_PNG]


async def _upload_to_e2b(
    _tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    monkeypatch.setattr("agno.tools.e2b.Sandbox", _FakeSandbox)
    tool = MindRoomE2BTools(api_key="test", tool_output_workspace_root=workspace, file_access=file_access)
    assert isinstance(tool.sandbox, _FakeSandbox)
    tool.upload_file(raw_path, "upload.bin")
    return tool.sandbox.files.stored.get("upload.bin") == _PNG


class _AudioEndpoint:
    """Record the bytes of every file an OpenAI or Groq audio request uploads."""

    def __init__(self) -> None:
        self.uploads: list[bytes] = []

    def create(self, *, file: tuple[str, BinaryIO], **_kwargs: object) -> str:
        self.uploads.append(file[1].read())
        return "transcript"


async def _transcribe_with_openai(
    _tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    endpoint = _AudioEndpoint()
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=endpoint))
    monkeypatch.setattr(openai_module, "OpenAIClient", lambda **_kwargs: client)
    tool = MindRoomOpenAITools(api_key="test", tool_output_workspace_root=workspace, file_access=file_access)
    tool.transcribe_audio(raw_path)
    return endpoint.uploads == [_PNG]


async def _transcribe_with_groq(
    _tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    endpoint = _AudioEndpoint()
    tool = MindRoomGroqTools(api_key="test", tool_output_workspace_root=workspace, file_access=file_access)
    monkeypatch.setattr(tool, "client", SimpleNamespace(audio=SimpleNamespace(transcriptions=endpoint)))
    tool.transcribe_audio(raw_path)
    return endpoint.uploads == [_PNG]


async def _extract_audio_with_moviepy(
    _tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    videos: list[bytes] = []

    def open_video(path: str) -> SimpleNamespace:
        videos.append(Path(path).read_bytes())
        audio = SimpleNamespace(write_audiofile=lambda output: Path(output).write_bytes(b"audio"))
        return SimpleNamespace(audio=audio, close=lambda: None)

    monkeypatch.setattr(moviepy_module, "VideoFileClip", open_video)
    tool = MindRoomMoviePyVideoTools(tool_output_workspace_root=workspace, file_access=file_access)
    tool.extract_audio(raw_path, "audio.wav")
    return videos == [_PNG]


async def _read_dag_file(
    _tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    tool = MindRoomAirflowTools(tool_output_workspace_root=workspace, file_access=file_access)
    return tool.read_dag_file(raw_path) == _TEXT


async def _read_with_file_tool(
    _tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    tool = file_tools()(base_dir=workspace, file_access=file_access)
    return tool.read_file(raw_path) == _TEXT


async def _read_with_coding_tool(
    _tmp_path: Path,
    _monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    tool = CodingTools(base_dir=str(workspace), file_access=file_access)
    return _TEXT.strip() in tool.read_file(raw_path)


_PROBES = (
    _ToolProbe("attachments", "register_attachment", _register_attachment, attachments_module),
    _ToolProbe("attachments", "view_file", _view_file, media_delivery_module),
    _ToolProbe("matrix_message", "send attachments", _send_attachment_path, attachments_module),
    _ToolProbe("gmail", "attachments", _stage_gmail_attachment, gmail_module),
    _ToolProbe("google_drive", "upload_file", _upload_to_google_drive, google_drive_module),
    _ToolProbe("browser", "upload", _upload_in_browser, browser_module),
    _ToolProbe("e2b", "upload_file", _upload_to_e2b, e2b_module),
    _ToolProbe("openai", "transcribe_audio", _transcribe_with_openai, openai_module),
    _ToolProbe("groq", "transcribe_audio", _transcribe_with_groq, groq_module),
    _ToolProbe("moviepy_video_tools", "extract_audio", _extract_audio_with_moviepy, moviepy_module),
    _ToolProbe("airflow", "read_dag_file", _read_dag_file, airflow_module, "doc.txt"),
    # `file` and `coding` resolve paths themselves and read through descriptors pinned
    # from their base directory; their own link-swap test is below.
    _ToolProbe("file", "read_file", _read_with_file_tool, None, "doc.txt"),
    _ToolProbe("coding", "read_file", _read_with_coding_tool, None, "doc.txt"),
)
_LINK_DEFENDING_PROBES = tuple(probe for probe in _PROBES if probe.resolver_module is not None)


def _probe_id(probe: _ToolProbe) -> str:
    return f"{probe.tool_name}:{probe.entry_point}"


def test_every_agent_file_access_tool_has_a_contract_probe(tmp_path: Path) -> None:
    """A tool that follows file_access must be added here, so it cannot skip the contract."""
    ensure_tool_registry_loaded(resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path))
    declared = {name for name, metadata in TOOL_METADATA.items() if metadata.file_access is ToolFileAccess.AGENT}
    assert declared == {probe.tool_name for probe in _PROBES}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return an agent workspace holding the contract files."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "doc.png").write_bytes(_PNG)
    (root / "doc.txt").write_text(_TEXT)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Return a directory outside the workspace holding the same contract files."""
    root = tmp_path / "outside"
    root.mkdir()
    (root / "doc.png").write_bytes(_PNG)
    (root / "doc.txt").write_text(_TEXT)
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", _PROBES, ids=_probe_id)
@pytest.mark.parametrize("file_access", ["workspace", "unrestricted"])
async def test_workspace_files_are_readable_in_both_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    probe: _ToolProbe,
    file_access: FileAccess,
) -> None:
    """Workspace files stay usable whatever the agent's file_access."""
    assert await probe.read(tmp_path, monkeypatch, workspace, file_access, probe.filename)


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", _PROBES, ids=_probe_id)
@pytest.mark.parametrize(("file_access", "readable"), [("workspace", False), ("unrestricted", True)])
async def test_outside_files_follow_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    outside: Path,
    probe: _ToolProbe,
    file_access: FileAccess,
    readable: bool,
) -> None:
    """Paths outside the workspace are refused in workspace mode and read in unrestricted mode."""
    assert await probe.read(tmp_path, monkeypatch, workspace, file_access, str(outside / probe.filename)) is readable


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", _LINK_DEFENDING_PROBES, ids=_probe_id)
async def test_workspace_root_replaced_by_link_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outside: Path,
    probe: _ToolProbe,
) -> None:
    """A workspace root that worker code replaced with a link never authorizes the link's target."""
    workspace = tmp_path / "workspace"
    workspace.symlink_to(outside, target_is_directory=True)

    assert not await probe.read(tmp_path, monkeypatch, workspace, "workspace", probe.filename)


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", _LINK_DEFENDING_PROBES, ids=_probe_id)
async def test_file_swapped_for_link_after_the_check_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    outside: Path,
    probe: _ToolProbe,
) -> None:
    """A checked file that worker code swaps for a link before the read is never followed."""
    assert probe.resolver_module is not None
    resolve = probe.resolver_module.resolve_agent_file

    def resolve_then_swap(*args: object, **kwargs: object) -> object:
        authorized = resolve(*args, **kwargs)
        checked = workspace / probe.filename
        checked.unlink()
        checked.symlink_to(outside / probe.filename)
        return authorized

    monkeypatch.setattr(probe.resolver_module, "resolve_agent_file", resolve_then_swap)

    assert not await probe.read(tmp_path, monkeypatch, workspace, "workspace", probe.filename)


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", ["file", "workspace"])
@pytest.mark.parametrize(
    ("probe", "resolver_module"),
    [(_PROBES[-2], file_tool_module), (_PROBES[-1], coding_module)],
    ids=["file:read_file", "coding:read_file"],
)
async def test_worker_path_tool_file_swapped_for_link_after_the_check_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    outside: Path,
    probe: _ToolProbe,
    resolver_module: ModuleType,
    swap: str,
) -> None:
    """A file or workspace that `file` or `coding` checked and then swapped for a link is never followed."""
    resolve = resolver_module.resolve_base_dir_path

    def resolve_then_swap(*args: object, **kwargs: object) -> Path:
        resolved = resolve(*args, **kwargs)
        if swap == "workspace":
            workspace.rename(tmp_path / "moved-workspace")
            workspace.symlink_to(outside, target_is_directory=True)
        else:
            checked = workspace / probe.filename
            checked.unlink()
            checked.symlink_to(outside / probe.filename)
        return resolved

    monkeypatch.setattr(resolver_module, "resolve_base_dir_path", resolve_then_swap)

    assert not await probe.read(tmp_path, monkeypatch, workspace, "workspace", probe.filename)


@pytest.mark.parametrize(
    "operation",
    [
        lambda file_tool, _coding: file_tool.save_file("new notes", "notes/new.txt"),
        lambda file_tool, _coding: file_tool.read_file("doc.txt"),
        lambda file_tool, _coding: file_tool.delete_file("doc.txt"),
        lambda _file, coding_tool: coding_tool.write_file("notes/new.txt", "new notes"),
        lambda _file, coding_tool: coding_tool.edit_file("doc.txt", "contract", "changed"),
    ],
    ids=["file:save_file", "file:read_file", "file:delete_file", "coding:write_file", "coding:edit_file"],
)
def test_worker_path_tool_refuses_a_workspace_replaced_by_link_after_construction(
    tmp_path: Path,
    workspace: Path,
    outside: Path,
    operation: Callable[[FileTools, CodingTools], str],
) -> None:
    """Once built, `file` and `coding` never follow a workspace that worker code replaced with a link."""
    file_tool = file_tools()(base_dir=workspace, enable_delete_file=True)
    coding_tool = CodingTools(base_dir=str(workspace))
    workspace.rename(tmp_path / "moved-workspace")
    workspace.symlink_to(outside, target_is_directory=True)

    result = operation(file_tool, coding_tool)

    assert result.startswith("Error")
    assert "unrestricted" not in result
    assert sorted(entry.name for entry in outside.iterdir()) == ["doc.png", "doc.txt"]
    assert (outside / "doc.txt").read_text() == _TEXT


@pytest.mark.parametrize(
    "operation",
    [
        lambda file_tool, _coding: file_tool.list_files(),
        lambda file_tool, _coding: file_tool.search_files("*.json"),
        lambda file_tool, _coding: file_tool.search_content("sk-"),
        lambda file_tool, _coding: file_tool.search_content("sk-", directory="."),
        lambda _file, coding_tool: coding_tool.ls(),
        lambda _file, coding_tool: coding_tool.ls("."),
        lambda _file, coding_tool: coding_tool.grep("sk-"),
        lambda _file, coding_tool: coding_tool.grep("sk-", path="."),
        lambda _file, coding_tool: coding_tool.find_files("*.json"),
        lambda _file, coding_tool: coding_tool.find_files("*.json", path="."),
    ],
    ids=[
        "file:list_files",
        "file:search_files",
        "file:search_content",
        "file:search_content-directory",
        "coding:ls",
        "coding:ls-path",
        "coding:grep",
        "coding:grep-path",
        "coding:find_files",
        "coding:find_files-path",
    ],
)
def test_worker_path_tool_does_not_list_or_search_a_workspace_replaced_by_link(
    tmp_path: Path,
    workspace: Path,
    outside: Path,
    operation: Callable[[FileTools, CodingTools], str],
) -> None:
    """Listing and searching never reveal the target of a workspace that worker code replaced with a link."""
    (outside / "openai.json").write_text('{"api_key": "sk-SECRET-VALUE"}\n')
    file_tool = file_tools()(base_dir=workspace)
    coding_tool = CodingTools(base_dir=str(workspace))
    workspace.rename(tmp_path / "moved-workspace")
    workspace.symlink_to(outside, target_is_directory=True)

    result = operation(file_tool, coding_tool)

    assert "openai.json" not in result
    assert "SECRET" not in result
    assert "unrestricted" not in result


@pytest.mark.parametrize("swap", ["workspace-link", "ancestor-swapped-while-resolving"])
@pytest.mark.parametrize(
    "build",
    [lambda base_dir: file_tools()(base_dir=base_dir), lambda base_dir: CodingTools(base_dir=str(base_dir))],
    ids=["file", "coding"],
)
def test_worker_path_tool_refuses_a_base_dir_swapped_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    outside: Path,
    build: Callable[[Path], object],
    swap: str,
) -> None:
    """A workspace swapped after runtime resolution but before the toolkit pins it is refused, never adopted."""
    if swap == "workspace-link":
        workspace.rename(tmp_path / "moved-workspace")
        workspace.symlink_to(outside, target_is_directory=True)
        base_dir = workspace
    else:
        (tmp_path / "current").symlink_to(tmp_path, target_is_directory=True)
        (outside / "workspace").mkdir()
        base_dir = tmp_path / "current" / "workspace"
        open_directory = path_safety_module.open_directory_within_root

        def swap_then_open(root: Path, *args: object, **kwargs: object) -> object:
            (tmp_path / "current").unlink()
            (tmp_path / "current").symlink_to(outside, target_is_directory=True)
            return open_directory(root, *args, **kwargs)

        monkeypatch.setattr(path_safety_module, "open_directory_within_root", swap_then_open)

    with pytest.raises(ValueError, match="base_dir"):
        build(base_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", _PROBES[-2:], ids=["file:read_file", "coding:read_file"])
async def test_worker_path_tool_refuses_a_file_above_the_read_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    probe: _ToolProbe,
) -> None:
    """A huge or sparse workspace file is refused instead of being read whole into the primary."""
    with (workspace / probe.filename).open("r+b") as doc:
        doc.truncate(65 << 20)

    assert not await probe.read(tmp_path, monkeypatch, workspace, "workspace", probe.filename)


@pytest.mark.parametrize("interrupted", [False, True], ids=["complete", "interrupted"])
def test_worker_path_tool_writes_replace_the_entry_instead_of_the_linked_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    interrupted: bool,
) -> None:
    """A workspace file hard-linked to a primary file is replaced, never written through, and never left partial."""
    outside = tmp_path / "primary-owned.db"
    outside.write_text("primary-only state", encoding="utf-8")
    target = workspace / "notes.md"
    os.link(outside, target)
    if interrupted:

        def fail_rename(*_args: object, **_kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "replace", fail_rename)
        with pytest.raises(OSError, match="No space"):
            path_safety_module.write_resolved_file(workspace, target.resolve(), b"new notes")
        assert target.read_text(encoding="utf-8") == "primary-only state"
    else:
        path_safety_module.write_resolved_file(workspace, target.resolve(), b"new notes")
        assert target.read_text(encoding="utf-8") == "new notes"
    assert outside.read_text(encoding="utf-8") == "primary-only state"


@pytest.mark.parametrize(
    "refusal",
    [None, PermissionError(1, "Operation not permitted"), OSError(22, "Invalid argument")],
    ids=["owner-kept", "owner-refused", "owner-unmapped"],
)
def test_worker_path_tool_writes_keep_the_replaced_files_owner_where_permitted(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    refusal: OSError | None,
) -> None:
    """A replacement keeps the worker's owner, or at least its group, and is still published when both are refused.

    An owner the primary cannot map (user namespaces, NFSv4 idmap) is refused with EINVAL rather than EPERM.
    """
    permitted = refusal is None
    target = workspace / "notes.md"
    target.write_text("old notes", encoding="utf-8")
    target.chmod(0o640)
    original = target.stat()
    chowned: list[tuple[int, int, int]] = []

    def record_fchown(fd: int, uid: int, gid: int) -> None:
        chowned.append((os.fstat(fd).st_ino, uid, gid))
        if refusal is not None:
            raise refusal

    monkeypatch.setattr(os, "fchown", record_fchown)
    path_safety_module.write_resolved_file(workspace, target.resolve(), b"new notes")

    replaced = target.stat()
    assert target.read_text(encoding="utf-8") == "new notes"
    assert replaced.st_mode & 0o777 == 0o640
    kept_owner = (replaced.st_ino, original.st_uid, original.st_gid)
    assert chowned == ([kept_owner] if permitted else [kept_owner, (replaced.st_ino, -1, original.st_gid)])
