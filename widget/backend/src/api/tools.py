"""API endpoints for tools information."""

from fastapi import APIRouter
from pydantic import BaseModel

from mindroom.tools import TOOL_REGISTRY
from mindroom.tools_metadata import TOOL_METADATA

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolInfo(BaseModel):
    """Information about a registered tool."""

    name: str
    display_name: str
    description: str
    category: str
    status: str
    setup_type: str
    icon: str | None
    requires_config: list[str] | None
    dependencies: list[str] | None


class ToolsResponse(BaseModel):
    """Response containing all registered tools."""

    tools: list[ToolInfo]


@router.get("")
async def get_registered_tools() -> ToolsResponse:
    """Get all registered tools from mindroom with full metadata."""
    tools = []

    # Only return tools that have proper metadata
    for tool_name, metadata in TOOL_METADATA.items():
        tools.append(
            ToolInfo(
                name=tool_name,
                display_name=metadata.display_name,
                description=metadata.description,
                category=metadata.category.value,
                status=metadata.status.value,
                setup_type=metadata.setup_type.value,
                icon=metadata.icon,
                requires_config=metadata.requires_config,
                dependencies=metadata.dependencies,
            ),
        )

    # Sort by category, then by name
    tools.sort(key=lambda t: (t.category, t.name))

    return ToolsResponse(tools=tools)


@router.get("/check-frontend-coverage")
async def check_frontend_coverage() -> dict:
    """Check which tools are in backend but missing from frontend."""
    # These are the tools currently shown in the frontend
    # We'll get this list from the frontend in a real implementation
    frontend_tools = {
        "google",  # Gmail
        "outlook",
        "yahoo",
        "calendar",
        "amazon",
        "walmart",
        "ebay",
        "target",
        "imdb",
        "spotify",
        "netflix",
        "youtube",
        "apple_music",
        "hbo",
        "twitter",
        "facebook",
        "instagram",
        "reddit",
        "linkedin",
        "slack",
    }

    # Map some tool names to their frontend equivalents
    tool_name_mapping = {
        "gmail": "google",  # Gmail is shown as "Google Services"
        "x": "twitter",  # X is shown as Twitter
    }

    backend_tools = set(TOOL_REGISTRY.keys())

    # Apply name mappings
    mapped_backend = set()
    for tool in backend_tools:
        mapped_name = tool_name_mapping.get(tool, tool)
        mapped_backend.add(mapped_name)

    # Find tools that are in backend but not in frontend
    missing_in_frontend = backend_tools - frontend_tools
    # Exclude tools that have mappings
    missing_in_frontend = {t for t in missing_in_frontend if tool_name_mapping.get(t, t) not in frontend_tools}

    # Find tools that are in frontend but not in backend
    missing_in_backend = frontend_tools - mapped_backend

    return {
        "backend_tools": sorted(backend_tools),
        "frontend_tools": sorted(frontend_tools),
        "missing_in_frontend": sorted(missing_in_frontend),
        "missing_in_backend": sorted(missing_in_backend),
        "total_backend": len(backend_tools),
        "total_frontend": len(frontend_tools),
    }
