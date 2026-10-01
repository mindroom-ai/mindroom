"""Save File Generation output inside the agent workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from agno.tools.file_generation import FileGenerationTools
from agno.utils.log import log_warning

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import open_directory_within_root


class WorkspaceFileGenerationTools(FileGenerationTools):
    """File Generation toolkit whose saved files land only in the agent workspace."""

    def __init__(
        self,
        *,
        tool_output_workspace_root: Path | None = None,
        output_directory: str | None = None,
        save_files: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        super().__init__(**kwargs)
        self._workspace_root = tool_output_workspace_root
        self._relative_output_directory = Path(output_directory or "")
        if not save_files and output_directory is None:
            return
        if tool_output_workspace_root is None:
            log_warning("File generation saves are disabled because this agent has no workspace")
            return
        if self._relative_output_directory.is_absolute() or ".." in self._relative_output_directory.parts:
            msg = "file_generation output_directory must be a relative path inside the agent workspace"
            raise ValueError(msg)
        self.save_files = True
        self.output_directory = tool_output_workspace_root / self._relative_output_directory

    # AGNO_COMPAT: FileGenerationTools saves model-named files by path into any directory.
    # Reason: Agno 3.0.9 writes into output_directory, or the process working directory when
    # only save_files is set, and follows links, so a model-chosen name such as config.yaml
    # replaced MindRoom's own files and a link planted in a shared workspace redirected the write.
    # Upstream issue: Tracking gap; no matching issue identified on October 1, 2026.
    # Upstream PR: None identified for a caller-supplied file writer.
    # Remove when: FileGenerationTools accepts a writer or directory descriptor for saved files;
    # retain workspace-only saves through no-follow descriptors and atomic replacement.
    # Coverage: tests/test_file_generation_tool.py::test_file_generation_saves_only_inside_the_workspace.
    def _save_file_to_disk(self, content: str | bytes, filename: str) -> tuple[str | None, str | None]:
        """Publish one sanitized file name in the workspace output directory without following links."""
        try:
            with (
                open_directory_within_root(
                    cast("Path", self._workspace_root),
                    self._relative_output_directory,
                    create=True,
                ) as directory_fd,
                atomic_write_file_at(directory_fd, filename) as file,
            ):
                file.write(content.encode() if isinstance(content, str) else content)
        except OSError as error:
            log_warning(f"Failed to save generated file: {error}")
            return None, str(error)
        return str(cast("Path", self.output_directory) / filename), None
