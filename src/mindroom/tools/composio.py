"""Native Composio toolkit wrapping selected `composio-core` actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.tool_system.authoring import Toolkit, passthrough_tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import json_dumps, require_non_empty
from mindroom.vendor_telemetry import disable_vendor_telemetry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_MAX_ACTION_NAME_LENGTH = 64


def _published_action_name(action_name: str) -> str:
    return action_name.lower()[:_MAX_ACTION_NAME_LENGTH]


def _wrap_composio_action(
    toolset: Any,  # noqa: ANN401
    action_enum: Any,  # noqa: ANN401
    schema: dict[str, Any],
    *,
    published_name: str,
    entity_id: str,
) -> Callable[..., str]:
    action_name = str(schema["name"])
    description = str(schema.get("description") or f"Execute Composio action {action_name}.")
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}

    @passthrough_tool(name=published_name, description=description, parameters=parameters)
    def _run_action(**kwargs: Any) -> str:  # noqa: ANN401
        result = toolset.execute_action(
            action=action_enum(value=action_name),
            params=kwargs,
            entity_id=entity_id,
            _check_requested_actions=True,
        )
        return json_dumps(result)

    return _run_action


@register_tool_with_metadata(
    name="composio",
    file_access=ToolFileAccess.UNCONFINED,
    display_name="Composio",
    description="Access 1000+ integrations including Gmail, Salesforce, GitHub, and more",
    category=ToolCategory.INTEGRATIONS,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    requires_primary_runtime=True,
    icon="FaConnectdevelop",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="actions",
            label="Actions",
            type="string[]",
            required=True,
            placeholder="GITHUB_GET_THE_AUTHENTICATED_USER",
            description="Nonempty list of Composio action IDs to expose to the agent",
        ),
        ConfigField(
            name="api_key",
            env=("COMPOSIO_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            placeholder="comp_...",
            description="Composio API key",
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            description="Base URL for Composio API (leave empty for default)",
        ),
        ConfigField(
            name="entity_id",
            label="Entity ID",
            type="text",
            required=False,
            default="default",
            placeholder="default",
            description="Entity identifier for Composio workspace",
        ),
        ConfigField(
            name="workspace_id",
            label="Workspace ID",
            type="text",
            required=False,
            placeholder="workspace_123",
            description="Workspace identifier for organizing tools and data",
        ),
        ConfigField(
            name="output_dir",
            label="Output Directory",
            type="text",
            required=False,
            placeholder="/path/to/output",
            description="Directory path for output files",
        ),
        ConfigField(
            name="lockfile",
            label="Lock File Path",
            type="text",
            required=False,
            placeholder="/path/to/lockfile",
            description="Path to lock file for concurrency control",
        ),
        ConfigField(
            name="max_retries",
            label="Max Retries",
            type="number",
            required=False,
            default=3,
            description="Maximum number of retries for failed operations",
        ),
        ConfigField(
            name="verbosity_level",
            label="Verbosity Level",
            type="number",
            required=False,
            placeholder="1",
            description="Logging verbosity level (0-3, higher = more verbose)",
        ),
        ConfigField(
            name="output_in_file",
            label="Output in File",
            type="boolean",
            required=False,
            default=False,
            description="Enable file-based output for operations",
        ),
        ConfigField(
            name="allow_tracing",
            label="Allow Tracing",
            type="boolean",
            required=False,
            default=False,
            description="Enable operation tracing for debugging",
        ),
        ConfigField(
            name="lock",
            label="Enable Locking",
            type="boolean",
            required=False,
            default=True,
            description="Enable file locking for concurrent operations",
        ),
    ],
    dependencies=["composio-core"],
    docs_url="https://docs.mindroom.chat/tools/automation-and-platforms/#composio",
    supports_toolkit_filters=False,
)
def composio_tools() -> type[Toolkit]:
    """Return selected Composio actions as a native MindRoom toolkit."""
    from composio import Action, ComposioToolSet
    from composio.constants import DEFAULT_BASE_URL

    disable_vendor_telemetry()

    class MindRoomComposioTools(Toolkit):
        """Expose selected `composio-core` action schemas as MindRoom passthrough tools."""

        def __init__(
            self,
            actions: list[str] | None = None,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            entity_id: str = "default",
            workspace_id: str | None = None,
            output_dir: Path | None = None,
            output_in_file: bool = False,
            verbosity_level: int | None = None,
            allow_tracing: bool = False,
            max_retries: int = 3,
            lockfile: Path | None = None,
            lock: bool = True,
        ) -> None:
            if not actions:
                msg = "Composio requires a nonempty actions list."
                raise ValueError(msg)
            resolved_key = require_non_empty(api_key, "Composio API key")
            resolved_base_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else DEFAULT_BASE_URL
            toolset = ComposioToolSet(
                api_key=resolved_key,
                base_url=resolved_base_url,
                entity_id=entity_id,
                workspace_id=workspace_id,
                output_dir=output_dir,
                output_in_file=output_in_file,
                verbosity_level=verbosity_level,
                allow_tracing=allow_tracing,
                max_retries=max_retries,
                lockfile=lockfile,
                lock=lock,
            )
            toolset.validate_tools(actions=actions)
            schemas = toolset.get_action_schemas(
                actions=actions,
                check_connected_accounts=True,
                _populate_requested=True,
            )
            seen_names: dict[str, str] = {}
            wrapped: list[Callable[..., str]] = []
            for schema in schemas:
                dumped = schema.model_dump(exclude_none=True)
                action_name = str(dumped["name"])
                published_name = _published_action_name(action_name)
                previous = seen_names.get(published_name)
                if previous is not None and previous != action_name:
                    msg = (
                        f"Composio actions {previous!r} and {action_name!r} collide after "
                        f"truncating to the {_MAX_ACTION_NAME_LENGTH}-character tool name {published_name!r}."
                    )
                    raise ValueError(msg)
                seen_names[published_name] = action_name
                wrapped.append(
                    _wrap_composio_action(
                        toolset,
                        Action,
                        dumped,
                        published_name=published_name,
                        entity_id=toolset.entity_id,
                    ),
                )
            super().__init__(name="composio", tools=wrapped)

    return MindRoomComposioTools
