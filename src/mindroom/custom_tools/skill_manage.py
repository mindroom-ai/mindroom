"""Chat-time ``skill_manage``, like Hermes' foreground ``skill_manage``, for agents that list it or learn skills.

The skill review forks the agent's final request with its tools unchanged, so the review can only call tools the
agent's request offered; this is the one tool it writes skills with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.skill_learning.tools import SkillAction, SkillChange, manage_skill_in_chat

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


class SkillManageTools(Toolkit):
    """Create and change the agent's workspace skills."""

    def __init__(self, agent_name: str, config: Config, runtime_paths: RuntimePaths, skills_root: Path) -> None:
        self._agent_name = agent_name
        self._config = config
        self._runtime_paths = runtime_paths
        self._skills_root = skills_root
        super().__init__(name="skill_manage", tools=[self.skill_manage])

    async def skill_manage(
        self,
        action: SkillAction,
        name: str,
        content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        replace_all: bool = False,
    ) -> str:
        """Create or change a skill in your workspace skill library.

        Skills are folders under skills/ in your workspace, and configured skills are read-only. A new SKILL.md
        starts with YAML frontmatter whose name is exactly the directory name and whose description is one
        trigger-first sentence of at most 60 characters. Files that contain a literal credential are refused.

        Args:
            action: "create" a new skill from content, "patch" old_string to new_string in SKILL.md or file_path,
                "edit" replaces SKILL.md with content, "write_file" writes file_content to file_path, and
                "remove_file" deletes file_path.
            name: Skill directory name, lowercase and hyphenated.
            content: Complete SKILL.md for create or edit.
            old_string: Exact text to replace for patch; it must occur once unless replace_all is true.
            new_string: Replacement text for patch; an empty string deletes the match.
            file_path: Support file such as "references/topic.md" or "scripts/check.sh"; omit it to patch SKILL.md.
            file_content: Complete support file content for write_file.
            replace_all: Replace every occurrence of old_string instead of exactly one.

        """
        return await manage_skill_in_chat(
            self._config,
            self._runtime_paths,
            self._agent_name,
            self._skills_root,
            SkillChange(action, name, content, old_string, new_string, file_path, file_content, replace_all),
        )
