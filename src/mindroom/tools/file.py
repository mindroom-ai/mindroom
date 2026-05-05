"""File tool configuration."""

from __future__ import annotations

import json
from glob import has_magic
from pathlib import Path
from typing import Any, cast

from agno.tools.file import FileTools as AgnoFileTools
from agno.tools.file import log_debug, log_error

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolStatus,
    register_tool_with_metadata,
)


def _blocked_path_message(action: str, requested_path: str, base_dir: Path) -> str:
    """Explain why a file-tool path was blocked."""
    return (
        f"Error {action}: path '{requested_path}' is outside base_dir '{base_dir}'. "
        "Set restrict_to_base_dir=false to allow access outside base_dir."
    )


def _format_path_for_output(path: Path, base_dir: Path) -> str:
    """Prefer base-dir-relative output, falling back to absolute paths outside the base dir."""
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def _is_within_base_dir(path: Path, base_dir: Path) -> bool:
    """Check whether a resolved path stays within base_dir."""
    try:
        path.resolve().relative_to(base_dir.resolve())
    except (OSError, ValueError):
        return False
    return True


def _split_search_pattern(base_dir: Path, pattern: str) -> tuple[Path, str]:
    """Resolve the concrete search root ahead of the first glob component."""
    pattern_path = Path(pattern)
    if pattern_path.is_absolute():
        search_root = Path(pattern_path.anchor)
        parts = list(pattern_path.relative_to(search_root).parts)
    else:
        search_root = base_dir
        parts = list(pattern_path.parts)

    first_glob_index = next((index for index, part in enumerate(parts) if has_magic(part)), len(parts))
    static_parts = parts[:first_glob_index]
    glob_parts = parts[first_glob_index:]
    if not glob_parts and static_parts:
        glob_parts = [static_parts.pop()]

    resolved_root = search_root.joinpath(*static_parts).resolve()
    resolved_pattern = str(Path(*glob_parts)) if glob_parts else "."
    return resolved_root, resolved_pattern


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
        expose_base_directory: bool = False,
        max_file_length: int = 10000000,
        max_file_lines: int = 100000,
        line_separator: str = "\n",
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
            expose_base_directory=expose_base_directory,
            max_file_length=max_file_length,
            max_file_lines=max_file_lines,
            line_separator=line_separator,
            all=all,
            **cast("dict[str, Any]", kwargs),
        )

    def check_escape(self, relative_path: str) -> tuple[bool, Path]:
        """Check whether a path stays within base_dir when restriction is enabled."""
        return self._check_path(
            relative_path,
            self.base_dir,
            restrict_to_base_dir=self.restrict_to_base_dir,
        )

    def save_file(self, contents: str, file_name: str, overwrite: bool = True, encoding: str = "utf-8") -> str:
        """Save content to a file, with clear blocked-path errors."""
        try:
            safe, file_path = self.check_escape(file_name)
            if not safe:
                log_error(f"Attempted to save file: {file_name}")
                return _blocked_path_message("saving file", file_name, self.base_dir)
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
            safe, file_path = self.check_escape(file_name)
            if not safe:
                log_error(f"Attempted to read file: {file_name}")
                return _blocked_path_message("reading file", file_name, self.base_dir)
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
            safe, file_path = self.check_escape(file_name)
            if not safe:
                log_error(f"Attempted to replace file chunk: {file_name}")
                return _blocked_path_message("replacing file chunk", file_name, self.base_dir)
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
            safe, file_path = self.check_escape(file_name)
            if not safe:
                log_error(f"Attempted to read file: {file_name}")
                return _blocked_path_message("reading file", file_name, self.base_dir)
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
        safe, path = self.check_escape(file_name)
        try:
            if safe:
                if path.is_dir():
                    path.rmdir()
                    return ""
                path.unlink()
                return ""
            log_error(f"Attempt to delete file outside {self.base_dir}: {file_name}")
            return _blocked_path_message("removing file", file_name, self.base_dir)
        except Exception as e:
            log_error(f"Error removing {file_name}: {e}")
            return f"Error removing file: {e}"

    def list_files(self, **kwargs: object) -> str:
        """List files in a directory, falling back to absolute paths outside base_dir."""
        directory = kwargs.get("directory", ".")
        try:
            log_debug(f"Reading files in : {self.base_dir}/{directory}")
            safe, resolved_directory = self.check_escape(str(directory))
            if not safe:
                return _blocked_path_message("listing files", str(directory), self.base_dir)
            return json.dumps(
                [_format_path_for_output(file_path, self.base_dir) for file_path in resolved_directory.iterdir()],
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

            search_root, glob_pattern = _split_search_pattern(self.base_dir, pattern)
            if self.restrict_to_base_dir and not _is_within_base_dir(search_root, self.base_dir):
                return _blocked_path_message("searching files", pattern, self.base_dir)

            log_debug(f"Searching files in {search_root} with pattern {glob_pattern}")
            matching_files = []
            for file_path in search_root.glob(glob_pattern):
                if self.restrict_to_base_dir and not _is_within_base_dir(file_path, self.base_dir):
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
                file_paths = [_format_path_for_output(file_path, self.base_dir) for file_path in matching_files]
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


@register_tool_with_metadata(
    name="file",
    display_name="File Tools",
    description="Local file operations including read, write, list, and search",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
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
        "check_escape",
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
