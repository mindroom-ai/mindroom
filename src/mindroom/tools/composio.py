"""Composio tool configuration."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.vendor_telemetry import disable_vendor_telemetry

if TYPE_CHECKING:
    from agno.tools import Toolkit


@register_tool_with_metadata(
    name="composio",
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
        # Authentication/Connection parameters first
        ConfigField(
            name="api_key",
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
        # Workspace Configuration
        ConfigField(
            name="workspace_id",
            label="Workspace ID",
            type="text",
            required=False,
            placeholder="workspace_123",
            description="Workspace identifier for organizing tools and data",
        ),
        ConfigField(
            name="workspace_config",
            label="Workspace Config",
            type="text",
            required=False,
            placeholder='{"type": "local"}',
            description="JSON configuration for workspace settings",
        ),
        # Connection Configuration
        ConfigField(
            name="connected_account_ids",
            label="Connected Account IDs",
            type="text",
            required=False,
            placeholder='{"github": "account_123"}',
            description="JSON mapping of app names to connected account IDs",
        ),
        # Advanced Configuration
        ConfigField(
            name="metadata",
            label="Metadata",
            type="text",
            required=False,
            placeholder='{"key": "value"}',
            description="JSON metadata for tools and actions configuration",
        ),
        ConfigField(
            name="processors",
            label="Processors",
            type="text",
            required=False,
            description="Custom processors configuration (JSON format)",
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
        # Numerical Configuration
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
        # Feature Flags
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
        # Logging Configuration
        ConfigField(
            name="logging_level",
            label="Logging Level",
            type="text",
            required=False,
            default="INFO",
            placeholder="INFO",
            description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        ),
    ],
    dependencies=["composio-agno"],
    docs_url="https://docs.agno.com/tools/toolkits/others/composio",
    supports_toolkit_filters=False,
)
def composio_tools() -> "type[Toolkit]":
    """Return selected Composio actions as an Agno toolkit."""
    from agno.tools import Toolkit
    from composio import AppType
    from composio.tools.env.base import WorkspaceConfigType
    from composio.tools.toolset import MetadataType, ProcessorsType
    from composio.utils.logging import LogLevel
    from composio_agno import ComposioToolSet

    disable_vendor_telemetry()

    class MindRoomComposioTools(Toolkit):
        """Expose the SDK's selected action functions to MindRoom consumers."""

        def __init__(
            self,
            actions: list[str] | None = None,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            entity_id: str = "default",
            workspace_id: str | None = None,
            workspace_config: WorkspaceConfigType | None = None,
            metadata: MetadataType | None = None,
            processors: ProcessorsType | None = None,
            logging_level: LogLevel = LogLevel.INFO,
            output_dir: Path | None = None,
            output_in_file: bool = False,
            verbosity_level: int | None = None,
            allow_tracing: bool = False,
            connected_account_ids: dict[AppType, str] | None = None,
            max_retries: int = 3,
            lockfile: Path | None = None,
            lock: bool = True,
            **kwargs: Any,  # noqa: ANN401 - forwards metadata-selected SDK constructor kwargs.
        ) -> None:
            if not actions:
                msg = "Composio requires a nonempty actions list."
                raise ValueError(msg)
            super().__init__(name="composio")
            toolset = ComposioToolSet(
                api_key=api_key,
                base_url=base_url,
                entity_id=entity_id,
                workspace_id=workspace_id,
                workspace_config=workspace_config,
                metadata=metadata,
                processors=processors,
                logging_level=logging_level,
                output_dir=output_dir,
                output_in_file=output_in_file,
                verbosity_level=verbosity_level,
                allow_tracing=allow_tracing,
                connected_account_ids=connected_account_ids,
                max_retries=max_retries,
                lockfile=lockfile,
                lock=lock,
                **kwargs,
            )
            for toolkit in cast("list[Toolkit]", toolset.get_tools(actions=actions)):
                # Preserve SDK entrypoints and their deferred schema processing.
                self.functions.update(toolkit.functions)
                self.async_functions.update(toolkit.async_functions)

    return MindRoomComposioTools
