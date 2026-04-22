"""Docker tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
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
    function_names=(
        "build_image",
        "connect_container_to_network",
        "create_network",
        "create_volume",
        "disconnect_container_from_network",
        "exec_in_container",
        "get_container_logs",
        "inspect_container",
        "inspect_image",
        "inspect_network",
        "inspect_volume",
        "list_containers",
        "list_images",
        "list_networks",
        "list_volumes",
        "pull_image",
        "remove_container",
        "remove_image",
        "remove_network",
        "remove_volume",
        "run_container",
        "start_container",
        "stop_container",
        "tag_image",
    ),
)
def docker_tools() -> type[DockerTools]:
    """Return Docker tools for container management."""
    from agno.tools.docker import DockerTools

    return DockerTools
