"""API endpoints for tools information."""

from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel

from mindroom.tools import TOOL_REGISTRY
from mindroom.tools_metadata import TOOL_METADATA

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolsResponse(BaseModel):
    """Response containing all registered tools."""

    tools: list[dict]  # We'll convert ToolMetadata to dict for JSON serialization


@router.get("")
async def get_registered_tools() -> ToolsResponse:
    """Get all registered tools from mindroom with full metadata.

    This uses the ToolMetadata dataclass directly from the main library,
    converting it to a dict for JSON serialization.
    """
    tools = []

    # Use dataclasses.asdict to convert metadata to dict
    for metadata in TOOL_METADATA.values():
        # Convert dataclass to dict
        tool_dict = asdict(metadata)

        # Convert enums to their string values for JSON serialization
        tool_dict["category"] = metadata.category.value
        tool_dict["status"] = metadata.status.value
        tool_dict["setup_type"] = metadata.setup_type.value

        # Remove non-serializable fields
        tool_dict.pop("factory", None)  # Callable is not JSON serializable

        tools.append(tool_dict)

    # Sort by category, then by name
    tools.sort(key=lambda t: (t["category"], t["name"]))

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
