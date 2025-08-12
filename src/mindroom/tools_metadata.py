"""Tool metadata and enhanced registration system."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


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
    INTEGRATIONS = "integrations"
    SMART_HOME = "smart_home"


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
class ConfigField:
    """Definition of a configuration field."""

    name: str  # Environment variable name (e.g., "SMTP_HOST")
    label: str  # Display label (e.g., "SMTP Host")
    type: str = "text"  # Field type: text, password, number, boolean, select, url
    required: bool = True
    default: Any = None
    placeholder: str | None = None
    description: str | None = None
    options: list[dict[str, str]] | None = None  # For select type
    validation: dict[str, Any] | None = None  # min, max, pattern, etc.


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
    icon_color: str | None = None  # Tailwind color class like "text-blue-500"
    config_fields: list[ConfigField] | None = None  # Detailed field definitions
    dependencies: list[str] | None = None  # Required pip packages
    docs_url: str | None = None  # Documentation URL
    helper_text: str | None = None  # Additional helper text (markdown) for configuration
    factory: Callable[[], type] | None = None  # Tool factory function


# Global registry for tool metadata
TOOL_METADATA: dict[str, ToolMetadata] = {}


def register_tool_with_metadata(
    name: str,
    display_name: str,
    description: str,
    category: ToolCategory,
    status: ToolStatus = ToolStatus.AVAILABLE,
    setup_type: SetupType = SetupType.NONE,
    icon: str | None = None,
    icon_color: str | None = None,
    config_fields: list[ConfigField] | None = None,
    dependencies: list[str] | None = None,
    docs_url: str | None = None,
    helper_text: str | None = None,
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
        icon_color: Tailwind color class for icon
        config_fields: Configuration field definitions
        dependencies: Required pip packages
        docs_url: Documentation URL
        helper_text: Additional helper text (markdown) for configuration

    Returns:
        Decorator function

    """

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
            icon_color=icon_color,
            config_fields=config_fields,
            dependencies=dependencies,
            docs_url=docs_url,
            helper_text=helper_text,
            factory=func,
        )

        # Store in metadata registry
        TOOL_METADATA[name] = metadata

        # Also register in TOOL_REGISTRY for actual tool loading
        # Import here to avoid circular dependency
        from mindroom.tools import TOOL_REGISTRY  # noqa: PLC0415

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
