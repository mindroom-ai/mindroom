"""A plugin tool reads and writes the agent's files under the agent's file_access, in the primary and in a worker."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

import mindroom.api.sandbox_runner as sandbox_runner_module
from mindroom.agents import build_agent_toolkit
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.plugin_api import write_agent_file
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.agent_tool_helpers import call_entrypoint, toolkit_offers
from tests.api.sandbox_runner_support import execute_call
from tests.conftest import publish_test_plugins

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction

# The example tool of docs/plugins.md, which imports only the supported plugin surface.
_NOTES_TOOLS = '''
from pathlib import Path
from typing import Literal

from mindroom.plugin_api import (
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    Toolkit,
    register_tool_with_metadata,
    resolve_agent_file,
    tool,
    write_agent_file,
)


class NotesTools(Toolkit):
    name = "notes"

    def __init__(
        self,
        *,
        tool_output_workspace_root: Path | None,
        file_access: Literal["workspace", "unrestricted"],
    ) -> None:
        super().__init__()
        self.workspace_root = tool_output_workspace_root
        self.file_access = file_access

    @tool
    def save_note(self, path: str, text: str) -> str:
        """Save a note.

        Args:
            path: Where to save the note, relative to the workspace.
            text: The note.
        """
        written = write_agent_file(path, text.encode(), workspace_root=self.workspace_root, file_access=self.file_access)
        return f"Saved {written}."

    @tool
    def read_note(self, path: str) -> str:
        """Read a note.

        Args:
            path: The note's path, relative to the workspace.
        """
        note = resolve_agent_file(path, workspace_root=self.workspace_root, file_access=self.file_access, field_name="path")
        with note.open() as file:
            return file.read().decode()


@register_tool_with_metadata(
    name="notes",
    file_access=ToolFileAccess.AGENT,
    display_name="Notes",
    description="Save and read notes in the agent workspace",
    category=ToolCategory.PRODUCTIVITY,
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
)
def notes_tools() -> type[NotesTools]:
    return NotesTools
'''

_OUTSIDE_WRITE_CASES = [("workspace", False), ("unrestricted", True)]


def _notes_runtime(tmp_path: Path, file_access: FileAccess) -> tuple[Config, RuntimePaths]:
    plugin_root = tmp_path / "plugins" / "notes"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(json.dumps({"name": "notes", "tools_module": "tools.py"}))
    (plugin_root / "tools.py").write_text(_NOTES_TOOLS)
    config = Config.model_validate(
        {
            "agents": {"general": {"display_name": "General", "tools": ["notes"], "file_access": file_access}},
            "plugins": ["./plugins/notes"],
        },
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    return config, runtime_paths


def _primary_notes_tools(tmp_path: Path, file_access: FileAccess) -> tuple[ToolFunction, ToolFunction, Path]:
    """Return the save and read functions the agent's own build offers, and the agent's workspace."""
    config, runtime_paths = _notes_runtime(tmp_path, file_access)
    tools = publish_test_plugins(config, runtime_paths).tools
    toolkit = build_agent_toolkit(
        "notes",
        agent_tools=resolve_agent_tools(config, "general", tools),
        placement=tool_placement(runtime_paths, tools),
        runtime_paths=runtime_paths,
        execution_identity=None,
    )
    assert toolkit is not None
    workspace = agent_workspace_root_path(runtime_paths.storage_root, "general").resolve()
    return toolkit_offers(toolkit, "save_note"), toolkit_offers(toolkit, "read_note"), workspace


@pytest.mark.parametrize(("file_access", "outside_written"), _OUTSIDE_WRITE_CASES)
@pytest.mark.asyncio
async def test_plugin_tool_writes_and_reads_agent_files_in_the_primary(
    tmp_path: Path,
    file_access: FileAccess,
    outside_written: bool,
) -> None:
    """The agent's own build gives the tool its workspace and file_access; a write outside follows file_access."""
    save_note, read_note, workspace = _primary_notes_tools(tmp_path, file_access)
    note = workspace / "notes" / "today.md"

    assert await call_entrypoint(save_note, path="notes/today.md", text="Buy milk") == f"Saved {note}."

    assert note.read_text() == "Buy milk"
    assert await call_entrypoint(read_note, path="notes/today.md") == "Buy milk"
    outside = tmp_path / "outside.md"
    if outside_written:
        await call_entrypoint(save_note, path=str(outside), text="Outside")
        assert outside.read_text() == "Outside"
    else:
        with pytest.raises(ValueError, match="outside base_dir"):
            await call_entrypoint(save_note, path=str(outside), text="Outside")
        assert not outside.exists()


@pytest.mark.parametrize(("file_access", "outside_written"), _OUTSIDE_WRITE_CASES)
@pytest.mark.asyncio
async def test_plugin_tool_reads_back_a_home_path_it_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_access: FileAccess,
    outside_written: bool,
) -> None:
    """Writing and reading resolve `~` alike, so a note saved under the home directory reads back from there."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    save_note, read_note, workspace = _primary_notes_tools(tmp_path, file_access)

    if outside_written:
        await call_entrypoint(save_note, path="~/note.md", text="From home")
        assert (home / "note.md").read_text() == "From home"
        assert await call_entrypoint(read_note, path="~/note.md") == "From home"
    else:
        with pytest.raises(ValueError, match="outside base_dir"):
            await call_entrypoint(save_note, path="~/note.md", text="From home")
        with pytest.raises(ValueError, match="inside the agent workspace"):
            await call_entrypoint(read_note, path="~/note.md")
        assert not (home / "note.md").exists()
    assert not (workspace / "~").exists()


def test_write_agent_file_confines_an_unknown_file_access_to_the_workspace(tmp_path: Path) -> None:
    """Only `unrestricted` lets a write leave the workspace, as for reads."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"

    with pytest.raises(ValueError, match="outside base_dir"):
        write_agent_file(
            str(outside),
            b"Outside",
            workspace_root=workspace,
            file_access=cast("FileAccess", "Unrestricted"),
        )

    assert not outside.exists()


@pytest.mark.parametrize("execution", ["inprocess", "subprocess"])
@pytest.mark.parametrize(("file_access", "outside_written"), _OUTSIDE_WRITE_CASES)
@pytest.mark.asyncio
async def test_plugin_tool_writes_and_reads_agent_files_in_a_worker(
    tmp_path: Path,
    file_access: FileAccess,
    outside_written: bool,
    execution: str,
) -> None:
    """A worker gives the tool the agent's workspace there and the agent's file_access from the request's config."""
    config, runtime_paths = _notes_runtime(tmp_path, file_access)

    async def call(function_name: str, **kwargs: str) -> sandbox_runner_module.ExecuteResponse:
        request = execute_call(
            runtime_paths,
            config,
            tool_name="notes",
            function_name=function_name,
            kwargs=kwargs,
            routing_agent_name="general",
        )
        if execution == "subprocess":
            return sandbox_runner_module._execute_request_subprocess_sync(request)
        return await sandbox_runner_module._execute_request_inprocess(request)

    note = agent_workspace_root_path(runtime_paths.storage_root, "general").resolve() / "notes" / "today.md"

    written = await call("save_note", path="notes/today.md", text="Buy milk")

    assert (written.ok, written.result) == (True, f"Saved {note}."), written.error
    assert note.read_text() == "Buy milk"
    read = await call("read_note", path="notes/today.md")
    assert (read.ok, read.result) == (True, "Buy milk")
    outside = tmp_path / "outside.md"
    outside_write = await call("save_note", path=str(outside), text="Outside")
    if outside_written:
        assert outside_write.ok, outside_write.error
        assert outside.read_text() == "Outside"
    else:
        assert outside_write.ok is False
        assert "outside base_dir" in str(outside_write.error)
        assert not outside.exists()
