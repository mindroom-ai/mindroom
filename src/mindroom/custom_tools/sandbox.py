"""Persistent in-container sandbox workspace utilities."""

from __future__ import annotations

import shutil

from agno.tools import Toolkit

from mindroom.constants import MINDROOM_SANDBOX_WORKSPACE


class SandboxTools(Toolkit):
    """Manage the persistent workspace used by in-container sandbox tooling."""

    def __init__(self) -> None:
        self.workspace = MINDROOM_SANDBOX_WORKSPACE
        self.workspace.mkdir(parents=True, exist_ok=True)
        super().__init__(
            name="sandbox",
            tools=[self.workspace_info, self.list_workspace, self.reset_workspace],
        )

    def workspace_info(self) -> str:
        """Show the sandbox workspace path and basic state."""
        files = sum(1 for _ in self.workspace.rglob("*"))
        return f"Sandbox workspace: {self.workspace}\nEntries: {files}"

    def list_workspace(self, relative_path: str = ".") -> str:
        """List files in the sandbox workspace.

        Args:
            relative_path: Relative directory under the workspace.

        Returns:
            Newline-separated list of directory entries.

        """
        target = (self.workspace / relative_path).resolve()
        try:
            target.relative_to(self.workspace)
        except ValueError:
            return "Error: path must stay within sandbox workspace."

        if not target.exists():
            return f"Error: path does not exist: {relative_path}"
        if not target.is_dir():
            return f"Error: path is not a directory: {relative_path}"

        entries = sorted(path.name for path in target.iterdir())
        if not entries:
            return "(empty)"
        return "\n".join(entries)

    def reset_workspace(self, confirmation: str) -> str:
        """Delete all workspace state and recreate an empty workspace.

        Args:
            confirmation: Must be the exact string "RESET WORKSPACE".

        Returns:
            Result message.

        """
        if confirmation != "RESET WORKSPACE":
            return "Refusing reset. Call reset_workspace with confirmation='RESET WORKSPACE' to wipe all sandbox state."

        if self.workspace.exists():
            for entry in self.workspace.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        self.workspace.mkdir(parents=True, exist_ok=True)
        return f"Sandbox workspace reset: {self.workspace}"
