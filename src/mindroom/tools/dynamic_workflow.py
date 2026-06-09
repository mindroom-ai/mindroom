"""Dynamic Workflow tool metadata registration."""

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    register_builtin_tool_metadata,
)

register_builtin_tool_metadata(
    ToolMetadata(
        name="dynamic_workflow",
        display_name="Dynamic Workflows",
        description="Create, update, run, and inspect reusable multi-agent Dynamic Workflows",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Workflow",
        icon_color="text-violet-500",
        config_fields=[],
        dependencies=[],
        function_names=(
            "create_workflow",
            "validate_workflow",
            "update_workflow",
            "run_workflow",
            "get_workflow_run",
            "publish_workflow_report",
            "revoke_public_report",
            "list_workflows",
            "list_workflow_revisions",
        ),
    ),
)
