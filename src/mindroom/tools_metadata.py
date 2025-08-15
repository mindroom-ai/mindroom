"""Tool metadata and enhanced registration system."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit

from mindroom.credentials import get_credentials_manager

# Registry mapping tool names to their factory functions
TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}


def register_tool(name: str) -> Callable[[Callable[[], type[Toolkit]]], Callable[[], type[Toolkit]]]:
    """Decorator to register a tool factory function.

    Args:
        name: The name to register the tool under

    Returns:
        Decorator function

    """

    def decorator(func: Callable[[], type[Toolkit]]) -> Callable[[], type[Toolkit]]:
        TOOL_REGISTRY[name] = func
        return func

    return decorator


def get_tool_by_name(tool_name: str) -> Toolkit:
    """Get a tool instance by its registered name."""
    if tool_name not in TOOL_REGISTRY:
        available = ", ".join(sorted(TOOL_REGISTRY.keys()))
        msg = f"Unknown tool: {tool_name}. Available tools: {available}"
        raise ValueError(msg)

    try:
        tool_factory = TOOL_REGISTRY[tool_name]
        tool_class = tool_factory()

        creds_manager = get_credentials_manager()
        credentials = creds_manager.load_credentials(tool_name) or {}
        metadata = TOOL_METADATA[tool_name]

        init_kwargs = {}
        if metadata.config_fields:
            for field in metadata.config_fields:
                if field.name in credentials:
                    init_kwargs[field.name] = credentials[field.name]

        return tool_class(**init_kwargs)

    except ImportError as e:
        logger.warning(f"Could not import tool '{tool_name}': {e}")
        logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
        raise


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
    type: Literal["boolean", "number", "password", "text", "url", "select"] = "text"
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
    auth_provider: str | None = None  # Name of integration that provides auth (e.g., "google")
    docs_url: str | None = None  # Documentation URL
    helper_text: str | None = None  # Additional help text for setup
    factory: Callable | None = None  # Factory function to create tool instance


# Global registry for tool metadata
TOOL_METADATA: dict[str, ToolMetadata] = {}


def register_tool_with_metadata(
    *,
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
    auth_provider: str | None = None,
    docs_url: str | None = None,
    helper_text: str | None = None,
) -> Callable[[Callable[[], type]], Callable[[], type]]:
    """Decorator to register a tool with metadata.

    This decorator stores comprehensive metadata about tools that can be used
    by the frontend and other components.

    Args:
        name: Tool identifier used in registry
        display_name: Human-readable name for UI
        description: Brief description of what the tool does
        category: Tool category for organization
        status: Availability status of the tool
        setup_type: Type of setup required
        icon: Icon identifier for frontend
        icon_color: CSS color class for the icon
        config_fields: List of configuration fields
        dependencies: Required Python packages
        auth_provider: Name of integration that provides authentication
        docs_url: Link to documentation
        helper_text: Additional setup instructions

    Returns:
        Decorator function

    """

    def decorator(func: Callable) -> Callable:
        # Create metadata object
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
            auth_provider=auth_provider,
            docs_url=docs_url,
            helper_text=helper_text,
            factory=func,
        )

        # Store in metadata registry
        TOOL_METADATA[name] = metadata

        # Also register in TOOL_REGISTRY for actual tool loading
        TOOL_REGISTRY[name] = func

        return func

    return decorator


def get_tool_metadata(name: str) -> ToolMetadata | None:
    """Get metadata for a tool by name."""
    return TOOL_METADATA.get(name)


def get_all_tool_metadata() -> dict[str, ToolMetadata]:
    """Get all tool metadata."""
    return TOOL_METADATA.copy()
