"""File tool configuration."""

from __future__ import annotations

import json
import os
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.tools.file import TEXT_EXTENSIONS, path_matches_exclude
from agno.tools.file import FileTools as AgnoFileTools
from agno.utils.log import log_debug, log_error

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.path_safety import (
    blocked_file_action_message,
    format_path_for_output,
    is_within_base_dir,
    resolve_base_dir_path,
    split_search_pattern,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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
        restrict_to_base_dir: bool = True,
        **kwargs: object,
    ) -> None:
        self.restrict_to_base_dir = restrict_to_base_dir
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
        """Search file contents for a case-insensitive query, honoring the base-dir restriction.

        Agno's version relativizes every hit against ``base_dir`` and so fails
        on any directory outside it even when the restriction is off.
        """
        try:
            if not query or not query.strip():
                return "Error: Query cannot be empty"
            search_dir = self.base_dir
            if directory:
                safe, search_dir = self._check_path(directory, self.base_dir)
                if not safe:
                    return blocked_file_action_message("searching content", directory, self.base_dir)
            if not search_dir.is_dir():
                return f"Error: '{directory}' is not a directory"

            log_debug(f"Searching file contents in {search_dir} for '{query}'")
            matches = [
                {
                    "file": format_path_for_output(file_path, self.base_dir),
                    "size": f"{file_path.stat().st_size} bytes",
                    "snippet": _content_snippet(content, query.lower()),
                }
                for file_path, content in islice(self._content_matches(search_dir, query.lower()), limit)
            ]
            log_debug(f"Found {len(matches)} files containing '{query}'")
            return json.dumps({"query": query, "matches_found": len(matches), "files": matches}, indent=2)
        except Exception as e:
            error_msg = f"Error searching content for '{query}': {e}"
            log_error(error_msg)
            return error_msg

    def _content_matches(self, search_dir: Path, lower_query: str) -> Iterator[tuple[Path, str]]:
        """Yield (path, content) for searchable text files under ``search_dir`` containing the query.

        Exclusions are matched against ``base_dir`` as agno does while the
        search stays inside it, and against the searched directory's parent
        otherwise: agno's matcher excludes nothing once the search root lies
        outside ``base_dir``, and anchoring at the searched directory itself
        would drop its own name (searching ``.git`` must still find nothing).
        """
        anchor = self.base_dir if is_within_base_dir(search_dir, self.base_dir) else search_dir.parent
        for dirpath, dirnames, filenames in os.walk(search_dir):
            dirnames[:] = [
                name
                for name in dirnames
                if not path_matches_exclude(Path(dirpath) / name, anchor, self.exclude_patterns)
            ]
            for filename in filenames:
                file_path = Path(dirpath) / filename
                if path_matches_exclude(file_path, anchor, self.exclude_patterns):
                    continue
                if file_path.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                if self.restrict_to_base_dir and not is_within_base_dir(file_path, self.base_dir):
                    continue
                try:
                    if file_path.stat().st_size > _MAX_CONTENT_SEARCH_BYTES:
                        continue
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if lower_query in content.lower():
                    yield file_path, content


_MAX_CONTENT_SEARCH_BYTES = 500 * 1024


def _content_snippet(content: str, lower_query: str, context_chars: int = 200) -> str:
    """Return the text around the first match, trimmed to ``context_chars`` on each side."""
    index = content.lower().find(lower_query)
    start = max(0, index - context_chars)
    end = min(len(content), index + len(lower_query) + context_chars)
    return content[start:end].strip()


@register_tool_with_metadata(
    name="file",
    display_name="File Tools",
    description="Local file operations including read, write, list, and search",
    category=ToolCategory.DEVELOPMENT,
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
            name="restrict_to_base_dir",
            label="Restrict To Base Dir",
            type="boolean",
            required=False,
            default=True,
            description="Whether file access must stay under base_dir. Relative paths still resolve from base_dir.",
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
