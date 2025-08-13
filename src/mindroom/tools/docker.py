"""Docker tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.docker import DockerTools


@register_tool_with_metadata(
    name="docker",
    display_name="Docker",
    description="Container, image, volume, and network management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaDocker",
    icon_color="text-blue-500",
    config_fields=[
        # Container Management
        ConfigField(
            name="enable_container_management",
            label="Enable Container Management",
            type="boolean",
            required=False,
            default=True,
            description="Enable container operations (list, start, stop, remove, logs, inspect, run, exec)",
        ),
        # Image Management
        ConfigField(
            name="enable_image_management",
            label="Enable Image Management",
            type="boolean",
            required=False,
            default=True,
            description="Enable image operations (list, pull, remove, build, tag, inspect)",
        ),
        # Volume Management
        ConfigField(
            name="enable_volume_management",
            label="Enable Volume Management",
            type="boolean",
            required=False,
            default=False,
            description="Enable volume operations (list, create, remove, inspect)",
        ),
        # Network Management
        ConfigField(
            name="enable_network_management",
            label="Enable Network Management",
            type="boolean",
            required=False,
            default=False,
            description="Enable network operations (list, create, remove, inspect, connect, disconnect)",
        ),
    ],
    dependencies=["docker"],
    docs_url="https://docs.agno.com/tools/toolkits/local/docker",
)
def docker_tools() -> type[DockerTools]:
    """Return Docker tools for container management."""
    from agno.tools.docker import DockerTools

    return DockerTools
