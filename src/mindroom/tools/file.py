"""File tool configuration."""

from __future__ import annotations

import copy
import json
from pathlib import Path  # noqa: TC003 - toolkit introspection evaluates constructor annotations.
from typing import TYPE_CHECKING, Any, cast

from agno.tools.file import FileTools as AgnoFileTools
from agno.utils.log import log_debug, log_error

from mindroom.path_confinement import is_git_metadata_path
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.path_safety import (
    blocked_file_action_message,
    blocked_git_metadata_message,
    format_path_for_output,
    is_within_base_dir,
    resolve_base_dir_path,
    split_search_pattern,
)

if TYPE_CHECKING:
    from mindroom.config.models import FileAccess


class _MindRoomFileTools(AgnoFileTools):
    """MindRoom wrapper around Agno's file tools."""

    def __init__(
        self,
        base_dir: Path | None = None,
        enable_save_file: bool = True,
        enable_read_file: bool = True,
        enable_delete_file: bool = False,
        enable_list_files: bool = True,
        enable_search_files: bool = True,
        enable_read_file_chunk: bool = True,
        enable_replace_file_chunk: bool = True,
        enable_search_content: bool = True,
        expose_base_directory: bool = False,
        max_file_length: int = 10000000,
        max_file_lines: int = 100000,
        line_separator: str = "\n",
        exclude_patterns: list[str] | None = None,
        all: bool = False,  # noqa: A002
        file_access: FileAccess = "workspace",
        **kwargs: object,
    ) -> None:
        self.restrict_to_base_dir = file_access == "workspace"
        super().__init__(
            base_dir=base_dir,
            enable_save_file=enable_save_file,
            enable_read_file=enable_read_file,
            enable_delete_file=enable_delete_file,
            enable_list_files=enable_list_files,
            enable_search_files=enable_search_files,
            enable_read_file_chunk=enable_read_file_chunk,
            enable_replace_file_chunk=enable_replace_file_chunk,
            enable_search_content=enable_search_content,
            expose_base_directory=expose_base_directory,
            max_file_length=max_file_length,
            max_file_lines=max_file_lines,
            line_separator=line_separator,
            exclude_patterns=exclude_patterns,
            all=all,
            **cast("dict[str, Any]", kwargs),
        )

    def _check_path(self, file_name: str, base_dir: Path, restrict_to_base_dir: bool = True) -> tuple[bool, Path]:
        """Resolve a path against base_dir, honoring this toolkit's restriction setting.

        Replaces Agno's Toolkit helper so every method here shares one rule.
        """
        del restrict_to_base_dir
        try:
            return True, resolve_base_dir_path(base_dir, file_name, self.restrict_to_base_dir)
        except ValueError:
            log_error(f"Path escapes base directory: {file_name}")
            return False, base_dir

    def save_file(self, contents: str, file_name: str, overwrite: bool = True, encoding: str = "utf-8") -> str:
        """Save content to a file, with clear blocked-path errors."""
        try:
            safe, file_path = self._check_path(file_name, self.base_dir)
            if not safe:
                log_error(f"Attempted to save file: {file_name}")
                return blocked_file_action_message("saving file", file_name, self.base_dir)
            if is_git_metadata_path(file_path):
                log_error(f"Attempted to save Git metadata: {file_name}")
                return blocked_git_metadata_message("saving file", file_name)
            log_debug(f"Saving contents to {file_path}")
            if not file_path.parent.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
            if file_path.exists() and not overwrite:
                return f"File {file_name} already exists"
            file_path.write_text(contents, encoding=encoding)
            log_debug(f"Saved: {file_path}")
            return str(file_name)
        except Exception as e:
            log_error(f"Error saving to file: {e}")
            return f"Error saving to file: {e}"

    def read_file_chunk(self, file_name: str, start_line: int, end_line: int, encoding: str = "utf-8") -> str:
        """Read a range of lines from a file."""
        try:
            log_debug(f"Reading file: {file_name}")
            safe, file_path = self._check_path(file_name, self.base_dir)
            if not safe:
                log_error(f"Attempted to read file: {file_name}")
                return blocked_file_action_message("reading file", file_name, self.base_dir)
            contents = file_path.read_text(encoding=encoding)
            lines = contents.split(self.line_separator)
            return self.line_separator.join(lines[start_line : end_line + 1])
        except Exception as e:
            log_error(f"Error reading file: {e}")
            return f"Error reading file: {e}"

    def replace_file_chunk(
        self,
        file_name: str,
        start_line: int,
        end_line: int,
        chunk: str,
        encoding: str = "utf-8",
    ) -> str:
        """Replace a range of lines in a file."""
        try:
            log_debug(f"Patching file: {file_name}")
            safe, file_path = self._check_path(file_name, self.base_dir)
            if not safe:
                log_error(f"Attempted to replace file chunk: {file_name}")
                return blocked_file_action_message("replacing file chunk", file_name, self.base_dir)
            contents = file_path.read_text(encoding=encoding)
            lines = contents.split(self.line_separator)
            start = lines[0:start_line]
            end = lines[end_line + 1 :]
            return self.save_file(
                file_name=file_name,
                contents=self.line_separator.join([*start, chunk, *end]),
                encoding=encoding,
            )
        except Exception as e:
            log_error(f"Error patching file: {e}")
            return f"Error patching file: {e}"

    def read_file(self, file_name: str, encoding: str = "utf-8") -> str:
        """Read a file with clear blocked-path errors."""
        try:
            log_debug(f"Reading file: {file_name}")
            safe, file_path = self._check_path(file_name, self.base_dir)
            if not safe:
                log_error(f"Attempted to read file: {file_name}")
                return blocked_file_action_message("reading file", file_name, self.base_dir)
            contents = file_path.read_text(encoding=encoding)
            if len(contents) > self.max_file_length:
                return "Error reading file: file too long. Use read_file_chunk instead"
            if len(contents.split(self.line_separator)) > self.max_file_lines:
                return "Error reading file: file too long. Use read_file_chunk instead"
            return str(contents)
        except Exception as e:
            log_error(f"Error reading file: {e}")
            return f"Error reading file: {e}"

    def delete_file(self, file_name: str) -> str:
        """Delete a file or empty directory with clear blocked-path errors."""
        safe, path = self._check_path(file_name, self.base_dir)
        try:
            if safe and is_git_metadata_path(path):
                log_error(f"Attempted to remove Git metadata: {file_name}")
                return blocked_git_metadata_message("removing file", file_name)
            if safe:
                if path.is_dir():
                    path.rmdir()
                    return ""
                path.unlink()
                return ""
            log_error(f"Attempt to delete file outside {self.base_dir}: {file_name}")
            return blocked_file_action_message("removing file", file_name, self.base_dir)
        except Exception as e:
            log_error(f"Error removing {file_name}: {e}")
            return f"Error removing file: {e}"

    def list_files(self, directory: str = ".") -> str:
        """List files in a directory, falling back to absolute paths outside base_dir."""
        try:
            log_debug(f"Reading files in : {self.base_dir}/{directory}")
            safe, resolved_directory = self._check_path(str(directory), self.base_dir)
            if not safe:
                return blocked_file_action_message("listing files", str(directory), self.base_dir)
            return json.dumps(
                [format_path_for_output(file_path, self.base_dir) for file_path in resolved_directory.iterdir()],
                indent=4,
            )
        except Exception as e:
            log_error(f"Error reading files: {e}")
            return f"Error reading files: {e}"

    def search_files(self, pattern: str) -> str:
        """Search for files, allowing absolute patterns only when restriction is disabled."""
        try:
            if not pattern or not pattern.strip():
                return "Error: Pattern cannot be empty"

            search_root, glob_pattern = split_search_pattern(self.base_dir, pattern)
            if self.restrict_to_base_dir and not is_within_base_dir(search_root, self.base_dir):
                return blocked_file_action_message("searching files", pattern, self.base_dir)

            log_debug(f"Searching files in {search_root} with pattern {glob_pattern}")
            matching_files = []
            for file_path in search_root.glob(glob_pattern):
                if self.restrict_to_base_dir and not is_within_base_dir(file_path, self.base_dir):
                    continue
                matching_files.append(file_path)
            if self.expose_base_directory:
                file_paths = [str(file_path) for file_path in matching_files]
                result = {
                    "pattern": pattern,
                    "matches_found": len(file_paths),
                    "base_directory": str(search_root),
                    "files": file_paths,
                }
            else:
                file_paths = [format_path_for_output(file_path, self.base_dir) for file_path in matching_files]
                result = {
                    "pattern": pattern,
                    "matches_found": len(file_paths),
                    "files": file_paths,
                }
            log_debug(f"Found {len(file_paths)} files matching pattern {pattern}")
            return json.dumps(result, indent=2)
        except Exception as e:
            error_msg = f"Error searching files with pattern '{pattern}': {e}"
            log_error(error_msg)
            return error_msg

    def search_content(self, query: str, directory: str | None = None, limit: int = 10) -> str:
        """Search file contents, reaching outside ``base_dir`` only with unrestricted file access.

        Agno's implementation relativizes every hit and every exclusion check
        against ``base_dir``, so an outside directory is searched by a copy
        rooted there and its hits are reported as absolute paths.
        """
        if not directory:
            return super().search_content(query, directory, limit)
        safe, search_dir = self._check_path(directory, self.base_dir)
        if not safe:
            return blocked_file_action_message("searching content", directory, self.base_dir)
        if is_within_base_dir(search_dir, self.base_dir):
            return super().search_content(query, directory, limit)
        if not search_dir.is_dir():
            return f"Error: '{directory}' is not a directory"
        # AGNO_COMPAT: FileTools.search_content cannot search outside base_dir.
        # Reason: Agno 3.0.9 relativizes every hit and exclusion check against
        # self.base_dir, so unrestricted agents cannot search other directories.
        # Searching a shallow copy rooted at the target relies on that method
        # deriving all state from self.base_dir.
        # Upstream issue: Tracking gap; no matching issue for a search root
        # independent of base_dir has been identified.
        # Upstream PR: None identified.
        # Remove when: Agno accepts an absolute search directory outside base_dir
        # and reports absolute hit paths; keep the workspace-mode refusal and
        # exclusion matching relative to the searched directory.
        # Coverage: tests/test_coding_tools.py::TestFileToolFileAccess::test_file_tool_search_content_searches_outside_directories_when_unrestricted.
        rooted = copy.copy(self)
        rooted.base_dir = search_dir
        result = AgnoFileTools.search_content(rooted, query, None, limit)
        if result.startswith("Error"):
            return result
        payload = json.loads(result)
        for match in payload["files"]:
            match["file"] = str(search_dir / match["file"])
        return json.dumps(payload, indent=2)


@register_tool_with_metadata(
    name="file",
    display_name="File Tools",
    description="Read, write, list, and search files in the agent workspace",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.AGENT,
    managed_init_args=(ToolManagedInitArg.FILE_ACCESS,),
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="FaFolder",
    icon_color="text-yellow-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
            authored_override=False,
        ),
        ConfigField(
            name="enable_save_file",
            label="Enable Save File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_file",
            label="Enable Read File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_delete_file",
            label="Enable Delete File",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_list_files",
            label="Enable List Files",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_files",
            label="Enable Search Files",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_file_chunk",
            label="Enable Read File Chunk",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_replace_file_chunk",
            label="Enable Replace File Chunk",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_content",
            label="Enable Search Content",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="expose_base_directory",
            label="Expose Base Directory",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="max_file_length",
            label="Max File Length",
            type="number",
            required=False,
            default=10000000,
        ),
        ConfigField(
            name="max_file_lines",
            label="Max File Lines",
            type="number",
            required=False,
            default=100000,
        ),
        ConfigField(
            name="line_separator",
            label="Line Separator",
            type="text",
            required=False,
            default="\n",
        ),
        ConfigField(
            name="exclude_patterns",
            label="Search Content Exclude Patterns",
            type="string[]",
            required=False,
            default=None,
            description=(
                "Fnmatch-style path component patterns excluded from content search. "
                "Leave unset to use Agno defaults; set an empty list to disable exclusions."
            ),
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["agno"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/local/file",
    function_names=(
        "delete_file",
        "list_files",
        "read_file",
        "read_file_chunk",
        "replace_file_chunk",
        "save_file",
        "search_content",
        "search_files",
    ),
)
def file_tools() -> type[AgnoFileTools]:
    """Return file tools for local file operations."""
    return _MindRoomFileTools
