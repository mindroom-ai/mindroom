"""File tool configuration."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.tools.file import FileTools as AgnoFileTools
from agno.tools.file.file import TEXT_EXTENSIONS, _extract_snippet, _format_size
from agno.utils.log import log_debug, log_error

from mindroom.path_confinement import is_git_metadata_path, read_regular_file_within_root
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
    read_resolved_file,
    remove_resolved_path,
    resolve_base_dir_path,
    resolve_tool_base_dir,
    split_search_pattern,
    write_resolved_file,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.config.models import FileAccess

# Agno searches only text files up to this size.
_SEARCH_CONTENT_MAX_BYTES = 500 * 1024


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
        # Agno's plain resolve would adopt the target of a link swapped in after runtime resolution.
        self.base_dir = resolve_tool_base_dir(base_dir)

    def _check_path(self, file_name: str, base_dir: Path, restrict_to_base_dir: bool = True) -> tuple[bool, Path]:
        """Resolve a path against base_dir, honoring this toolkit's restriction setting.

        Replaces Agno's Toolkit helper so every method here shares one rule.
        """
        del restrict_to_base_dir
        try:
            return True, resolve_base_dir_path(base_dir, file_name, self.restrict_to_base_dir)
        except ValueError as exc:
            log_error(f"Refused path {file_name}: {exc}")
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
            if file_path.exists() and not overwrite:
                return f"File {file_name} already exists"
            write_resolved_file(self.base_dir, file_path, contents.encode(encoding))
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
            contents = read_resolved_file(self.base_dir, file_path).decode(encoding)
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
            contents = read_resolved_file(self.base_dir, file_path).decode(encoding)
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
            contents = read_resolved_file(self.base_dir, file_path).decode(encoding)
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
                remove_resolved_path(self.base_dir, path)
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
        safe, search_dir = self._check_path(directory or ".", self.base_dir)
        if not safe:
            return blocked_file_action_message("searching content", directory or ".", self.base_dir)
        if self.restrict_to_base_dir:
            return self._search_workspace_content(query, directory, search_dir, limit)
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

    def _search_workspace_content(self, query: str, directory: str | None, search_dir: Path, limit: int) -> str:
        """Search like Agno, reading each walked file through the same checks and descriptors as ``read_file``."""
        # AGNO_COMPAT: FileTools.search_content checks one path and then opens another.
        # Reason: Agno 3.0.9 checks each walked file with safe_join_relative_path, which
        # NFKC-normalizes the path and strips trailing dots and spaces from every segment, then
        # opens the unsanitized path by name and follows its links, so worker code that writes the
        # workspace can plant `d /x.txt` linked to any file beside a real `d/x.txt` and make a
        # primary-process search return its lines.
        # This copy keeps Agno's walk, exclusions, text extensions, size limit, snippets, and
        # result format through its private helpers, but resolves each file with MindRoom's
        # resolver and reads that canonical path through no-follow descriptors.
        # Upstream issue: Tracking gap; no issue for search_content opening a different path than
        # it validated has been identified.
        # Upstream PR: None identified.
        # Remove when: Agno's search_content opens exactly the path it validated without following
        # links out of base_dir, or accepts a caller-supplied file reader; keep MindRoom's
        # descriptor reads for workspace mode.
        # Coverage: tests/test_file_access_contract.py::test_file_tool_content_search_never_reads_a_planted_link_target.
        if not query or not query.strip():
            return "Error: Query cannot be empty"
        if not search_dir.is_dir():
            return f"Error: '{directory}' is not a directory"
        lower_query = query.lower()
        matches: list[dict[str, str]] = []
        for file_path in self._searchable_files(search_dir):
            if len(matches) >= limit:
                break
            try:
                resolved = resolve_base_dir_path(self.base_dir, str(file_path.relative_to(self.base_dir)))
                payload = read_regular_file_within_root(
                    self.base_dir,
                    resolved.relative_to(self.base_dir),
                    max_bytes=_SEARCH_CONTENT_MAX_BYTES,
                )
            except (OSError, ValueError):
                continue
            content = payload.decode("utf-8", errors="ignore")
            if lower_query in content.lower():
                matches.append(
                    {
                        "file": file_path.relative_to(self.base_dir).as_posix(),
                        "size": _format_size(len(payload)),
                        "snippet": _extract_snippet(content, query),
                    },
                )
        return json.dumps({"query": query, "matches_found": len(matches), "files": matches}, indent=2)

    def _searchable_files(self, search_dir: Path) -> Iterator[Path]:
        """Walk without following directory links, yielding the text files Agno's content search would read."""
        for dirpath, dirnames, filenames in os.walk(search_dir):
            dirnames[:] = [name for name in dirnames if not self._is_excluded(Path(dirpath) / name)]
            for name in filenames:
                file_path = Path(dirpath) / name
                if not self._is_excluded(file_path) and file_path.suffix.lower() in TEXT_EXTENSIONS:
                    yield file_path


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
