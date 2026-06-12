"""Dynamic Workflow tool metadata registration."""

from mindroom.tool_system.metadata import (
    ConfigField,
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
        config_fields=[
            ConfigField(
                name="allowed_tools",
                label="Pre-approved participant tools",
                type="string[]",
                required=False,
                default=None,
                description=(
                    "Tool names workflow participants may call without per-call user approval. "
                    'Use "*" to pre-approve every granted tool. '
                    "System-mutating tools (claude_agent, config_manager, scheduler, subagents) "
                    "always require per-call approval and cannot be pre-approved."
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
