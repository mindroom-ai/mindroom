"""The file tools of one curation pass.

They edit only the pass's staged copy, never the live workspace, and every result reports progress against the
pass's band so the model can stop on target.
An edit that would cut a file or the total past the pass's limits is refused before it is staged, because a
model that over-cuts once rarely restores what it removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.prompt_curation.policy import file_shrink_violation, floor_violation, max_content_loss_tokens
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from mindroom.config.prompt_curation import PromptCurationConfig
    from mindroom.prompt_curation.policy import PassBounds, PassMeasurement
    from mindroom.prompt_curation.staging import StagedWorkspace


class PromptCurationTools(Toolkit):
    """Read, edit, and move text within one staged curation pass."""

    def __init__(
        self,
        staged: StagedWorkspace,
        bounds: PassBounds,
        settings: PromptCurationConfig,
        before: PassMeasurement,
    ) -> None:
        self._staged = staged
        self._bounds = bounds
        self._settings = settings
        self._before = before
        super().__init__(name="prompt_curation", tools=[self.read_file, self.edit_file, self.append_file])

    def read_file(self, path: str) -> str:
        """Read a curatable file or a memory/ Markdown file as this pass currently sees it.

        Args:
            path: Workspace-relative path, such as MEMORY.md or memory/projects.md.

        """
        try:
            return self._staged.read(path)
        except ValueError as exc:
            return f"Error: {exc}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace one exact, unique excerpt of a curatable file.

        Args:
            path: The curatable file, such as MEMORY.md.
            old_text: The exact text to replace; it must occur exactly once.
            new_text: The replacement, such as a condensed line, a one-line pointer to a memory/ file, or "".

        """
        try:
            self._staged.edit(path, old_text, new_text, refuse=self._refusal)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Edited {path}. {self._progress()}"

    def append_file(self, path: str, content: str) -> str:
        """Append text to a Markdown file under memory/, creating it if needed; use it to move detail out.

        Args:
            path: A topic file such as memory/projects.md.
            content: The text to add, copied verbatim from the curatable file it moves out of.

        """
        try:
            self._staged.append(path, content)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Appended to {path}. {self._progress()}"

    def _refusal(self, path: str, edited: str) -> str | None:
        after_tokens = estimate_text_tokens(edited)
        if violation := file_shrink_violation(path, self._before.curated[path], after_tokens, self._settings):
            return f"Refused: {violation}. Cut less from {path} in this pass."
        curated = {**self._staged.measurement().curated, path: after_tokens}
        if violation := floor_violation(sum(curated.values()), self._bounds):
            return f"Refused: {violation}. Cut less in this pass."
        return None

    def _progress(self) -> str:
        current = self._staged.measurement()
        removed_tokens = max(0, self._before.total_memory_tokens - current.total_memory_tokens)
        return (
            f"Curated files now {current.curated_tokens} tokens "
            f"(target at most {self._bounds.upper_tokens}, not below {self._bounds.floor_tokens}); "
            f"net memory content removed {removed_tokens} tokens "
            f"(at most {max_content_loss_tokens(self._before, self._settings)})."
        )
