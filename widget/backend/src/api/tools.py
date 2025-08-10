"""API endpoints for tools information."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolInfo(BaseModel):
    """Information about a registered tool."""

    name: str
    description: str
    requires_config: bool


class ToolsResponse(BaseModel):
    """Response containing all registered tools."""

    tools: list[ToolInfo]


@router.get("", response_model=ToolsResponse)
async def get_registered_tools() -> ToolsResponse:
    """Get all registered tools from mindroom."""
    try:
        from mindroom.tools import TOOL_REGISTRY
    except ImportError:
        # Return empty list if mindroom is not available
        return ToolsResponse(tools=[])

    # Tools that require configuration to work
    TOOLS_REQUIRING_CONFIG = {
        "github",
        "telegram",
        "email",
        "googlesearch",
        "tavily",
        "slack",
        "reddit",
        "twitter",
    }

    tools = []
    for tool_name in sorted(TOOL_REGISTRY.keys()):
        # Get the tool's docstring as description
        try:
            tool_factory = TOOL_REGISTRY[tool_name]
            description = tool_factory.__doc__ or f"{tool_name.title()} tool"
            # Clean up the description
            description = description.strip().split("\n")[0]  # First line only
        except Exception:
            description = f"{tool_name.title()} tool"

        tools.append(
            ToolInfo(
                name=tool_name,
                description=description,
                requires_config=tool_name in TOOLS_REQUIRING_CONFIG,
            )
        )

    return ToolsResponse(tools=tools)


@router.get("/check-frontend-coverage")
async def check_frontend_coverage() -> dict:
    """Check which tools are in backend but missing from frontend."""
    try:
        from mindroom.tools import TOOL_REGISTRY
    except ImportError:
        return {"error": "Could not import mindroom.tools"}

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
