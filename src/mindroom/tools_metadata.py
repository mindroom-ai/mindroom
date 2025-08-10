"""Tool metadata and enhanced registration system."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class ToolCategory(str, Enum):
    """Tool categories for organization."""

    EMAIL = "email"
    SHOPPING = "shopping"
    ENTERTAINMENT = "entertainment"
    SOCIAL = "social"
    DEVELOPMENT = "development"
    RESEARCH = "research"
    INFORMATION = "information"
    PRODUCTIVITY = "productivity"
    COMMUNICATION = "communication"


class ToolStatus(str, Enum):
    """Tool availability status."""

    AVAILABLE = "available"
    COMING_SOON = "coming_soon"
    REQUIRES_CONFIG = "requires_config"


class SetupType(str, Enum):
    """Tool setup type."""

    NONE = "none"  # No setup required
    API_KEY = "api_key"  # Requires API key
    OAUTH = "oauth"  # OAuth flow
    SPECIAL = "special"  # Special setup (e.g., for Google)
    COMING_SOON = "coming_soon"  # Not yet available


@dataclass
class ToolMetadata:
    """Complete metadata for a tool."""

    name: str  # Internal tool name (e.g., "gmail")
    display_name: str  # Display name (e.g., "Gmail")
    description: str  # Description for UI
    category: ToolCategory
    status: ToolStatus = ToolStatus.AVAILABLE
    setup_type: SetupType = SetupType.NONE
    icon: str | None = None  # Icon identifier for frontend
    requires_config: list[str] | None = None  # Required env vars or config
    dependencies: list[str] | None = None  # Required pip packages
    factory: Callable[[], type] | None = None  # Tool factory function


# Global registry for tool metadata
TOOL_METADATA: dict[str, ToolMetadata] = {}


def register_tool_with_metadata(
    name: str,
    display_name: str,
    description: str,
    category: ToolCategory | str,
    status: ToolStatus | str = ToolStatus.AVAILABLE,
    setup_type: SetupType | str = SetupType.NONE,
    icon: str | None = None,
    requires_config: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> Callable[[Callable[[], type]], Callable[[], type]]:
    """Enhanced decorator to register a tool with full metadata.

    Args:
        name: Internal tool name
        display_name: Display name for UI
        description: Tool description
        category: Tool category
        status: Availability status
        setup_type: Setup requirements
        icon: Icon identifier
        requires_config: Required configuration
        dependencies: Required pip packages

    Returns:
        Decorator function
    """
    # Convert strings to enums if needed
    if isinstance(category, str):
        category = ToolCategory(category)
    if isinstance(status, str):
        status = ToolStatus(status)
    if isinstance(setup_type, str):
        setup_type = SetupType(setup_type)

    def decorator(func: Callable[[], type]) -> Callable[[], type]:
        # Create metadata
        metadata = ToolMetadata(
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            status=status,
            setup_type=setup_type,
            icon=icon,
            requires_config=requires_config,
            dependencies=dependencies,
            factory=func,
        )

        # Store in metadata registry
        TOOL_METADATA[name] = metadata

        # Also register in TOOL_REGISTRY for actual tool loading
        from mindroom.tools import TOOL_REGISTRY

        TOOL_REGISTRY[name] = func

        return func

    return decorator


def get_tool_metadata(name: str) -> ToolMetadata | None:
    """Get metadata for a tool by name."""
    return TOOL_METADATA.get(name)


def get_all_tool_metadata() -> dict[str, ToolMetadata]:
    """Get all tool metadata."""
    return TOOL_METADATA.copy()


def get_tools_by_category(category: ToolCategory) -> dict[str, ToolMetadata]:
    """Get all tools in a specific category."""
    return {name: meta for name, meta in TOOL_METADATA.items() if meta.category == category}


def get_available_tools() -> dict[str, ToolMetadata]:
    """Get all available tools."""
    return {name: meta for name, meta in TOOL_METADATA.items() if meta.status == ToolStatus.AVAILABLE}
