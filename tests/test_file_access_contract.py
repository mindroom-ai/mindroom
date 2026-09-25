"""Contract every tool that follows the agent ``file_access`` setting must satisfy.

Each probe drives one tool's real path entry point and reports whether it read the requested file.
A tool declared ``ToolFileAccess.AGENT`` must appear here, so a new path tool cannot skip the contract.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import mindroom.custom_tools.attachments as attachments_module
import mindroom.custom_tools.browser as browser_module
import mindroom.custom_tools.e2b as e2b_module
import mindroom.custom_tools.gmail as gmail_module
import mindroom.custom_tools.google_drive as google_drive_module
import mindroom.custom_tools.microsoft_365 as microsoft_365_module
import mindroom.media_delivery as media_delivery_module
from mindroom.attachments import load_attachment
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.attachments import AttachmentTools, resolve_send_attachments
from mindroom.custom_tools.coding import CodingTools
from mindroom.custom_tools.e2b import MindRoomE2BTools
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.custom_tools.microsoft_365 import Microsoft365Tools
from mindroom.oauth.microsoft import microsoft_365_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.runtime_context import tool_runtime_context
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
from tests.microsoft_graph_test_support import ALICE_TOKEN, FakeGraph, publish_grant, save_client_config, worker_target
from tests.microsoft_graph_test_support import runtime_paths as microsoft_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

    from mindroom.config.models import FileAccess

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=",
)
_TEXT = "contract text\n"
_XLSX = b"PK\x03\x04contract workbook"


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


async def _save_to_microsoft_365(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    file_access: FileAccess,
    raw_path: str,
) -> bool:
    paths = microsoft_runtime_paths(tmp_path / "microsoft")
    manager = save_client_config(paths)
    publish_grant(microsoft_365_oauth_provider(), manager, ALICE_TOKEN)
    graph = FakeGraph.with_forecast().install(monkeypatch)
    tool = Microsoft365Tools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(),
        tool_output_workspace_root=workspace,
        file_access=file_access,
    )
    result = json.loads(await tool.save_office_document(raw_path))
    if result["status"] != "ok":
        return False
    return [request.content for request in graph.requests if request.method == "PUT"] == [_XLSX]


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
    _ToolProbe(
        "microsoft_365",
        "save_office_document",
        _save_to_microsoft_365,
        microsoft_365_module,
        "doc.xlsx",
    ),
    _ToolProbe("browser", "upload", _upload_in_browser, browser_module),
    _ToolProbe("e2b", "upload_file", _upload_to_e2b, e2b_module),
    # `file` and `coding` run in a worker by default, where worker code shares their trust,
    # so they are held to the confinement contract but not to the link-swap contract.
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
    (root / "doc.xlsx").write_bytes(_XLSX)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Return a directory outside the workspace holding the same contract files."""
    root = tmp_path / "outside"
    root.mkdir()
    (root / "doc.png").write_bytes(_PNG)
    (root / "doc.txt").write_text(_TEXT)
    (root / "doc.xlsx").write_bytes(_XLSX)
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
