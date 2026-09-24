"""Dynamic Workflow tool metadata registration."""

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="dynamic_workflow",
        file_access=ToolFileAccess.NONE,
        display_name="Dynamic Workflows",
        description="Create, update, run, and inspect reusable multi-agent Dynamic Workflows",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        requires_room_context=True,
        icon="Workflow",
        icon_color="text-violet-500",
        config_fields=[
            ConfigField(
                name="allowed_tools",
                label="Pre-approved participant tools",
                type="string[]",
                required=False,
                default=None,
                description=(
                    "Toolkit names receiving automatic approval grants after operator rules. "
                    'Use "*" for all eligible granted toolkits. '
                    "claude_agent, config_manager, and scheduler receive no generated grant; "
                    "explicit operator rules can authorize otherwise eligible functions. "
                    "Functions requiring approval or native confirmation remain unavailable."
                ),
            ),
        ],
        dependencies=[],
        function_names=(
            "create_workflow",
            "validate_workflow",
            "update_workflow",
            "run_workflow",
            "get_workflow_run",
            "list_workflows",
            "list_workflow_revisions",
        ),
    ),
)
