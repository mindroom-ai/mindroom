"""Skill-manage tool metadata registration.

Registers the ``skill_manage`` tool in the metadata registry.
The actual toolkit (``mindroom.custom_tools.skill_manage.SkillManageTools``)
requires the agent's workspace and is built in ``build_agent_toolkit()`` for
agents that list it or learn skills, so its metadata has no generic factory.
"""

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="skill_manage",
        file_access=ToolFileAccess.NONE,
        display_name="Skill Manage",
        description="Create and change the agent's workspace skills",
        docs_url="https://docs.mindroom.chat/skills/#the-skill_manage-tool",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Brain",
        icon_color="text-emerald-500",
        config_fields=[],
        dependencies=[],
        function_names=("skill_manage",),
    ),
)
