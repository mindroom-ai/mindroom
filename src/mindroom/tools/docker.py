"""Docker tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
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
    icon="SiDocker",
    icon_color="text-blue-500",
    config_fields=[],
    dependencies=["docker"],
    docs_url="https://docs.agno.com/tools/toolkits/local/docker",
)
def docker_tools() -> type[DockerTools]:
    """Return Docker tools for container management."""
    from agno.tools.docker import DockerTools

    return DockerTools
