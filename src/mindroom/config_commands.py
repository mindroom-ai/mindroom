"""Configuration command handling for user-driven config changes."""

from __future__ import annotations

import shlex
from pathlib import Path  # noqa: TC003
from typing import Any

import yaml
from pydantic import ValidationError

from .config import Config
from .constants import DEFAULT_AGENTS_CONFIG
from .logging_config import get_logger

logger = get_logger(__name__)


def parse_config_args(args_text: str) -> tuple[str, list[str]]:
    """Parse config command arguments.

    Args:
        args_text: Raw argument text from command

    Returns:
        Tuple of (operation, arguments)

    """
    if not args_text:
        return "show", []

    # Use shlex to handle quoted strings properly
    try:
        parts = shlex.split(args_text)
    except ValueError as e:
        # Handle parsing errors (e.g., unmatched quotes)
        # Return a special operation that will trigger an error message
        return "parse_error", [str(e)]

    if not parts:
        return "show", []

    operation = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    return operation, args


def get_nested_value(data: Any, path: str) -> Any:  # noqa: ANN401
    """Get a value from nested dict using dot notation.

    Args:
        data: The dictionary to search
        path: Dot-separated path (e.g., "agents.analyst.display_name")

    Returns:
        The value at the path

    Raises:
        KeyError: If path doesn't exist

    """
    keys = path.split(".")
    current = data

    for key in keys:
        # Handle array indexing
        if key.isdigit():  # noqa: SIM108
            current = current[int(key)]
        else:
            current = current[key]

    return current


def set_nested_value(data: Any, path: str, value: Any) -> None:  # noqa: ANN401
    """Set a value in nested dict using dot notation.

    Args:
        data: The dictionary to modify
        path: Dot-separated path (e.g., "agents.analyst.display_name")
        value: Value to set

    Raises:
        KeyError: If parent path doesn't exist

    """
    keys = path.split(".")
    current = data

    # Navigate to the parent of the target
    for key in keys[:-1]:
        if key.isdigit():
            current = current[int(key)]
        elif key not in current:
            # Auto-create missing intermediate dicts
            current[key] = {}
            current = current[key]
        else:
            current = current[key]

    # Set the final value
    final_key = keys[-1]
    if final_key.isdigit():
        current[int(final_key)] = value
    else:
        current[final_key] = value


def parse_value(value_str: str) -> Any:  # noqa: ANN401
    """Parse a string value into appropriate Python type.

    Args:
        value_str: String representation of value

    Returns:
        Parsed value (str, int, float, bool, list, or dict)

    """
    # Try to parse as YAML first (handles unquoted strings in arrays/dicts)
    # YAML is a superset of JSON, so this handles both formats
    # Examples that work:
    #   [item1, item2]          -> ['item1', 'item2']
    #   ["item1", "item2"]      -> ['item1', 'item2']
    #   {key: value}            -> {'key': 'value'}
    #   {"key": "value"}        -> {'key': 'value'}
    try:
        return yaml.safe_load(value_str)
    except yaml.YAMLError:
        pass

    # If YAML parsing fails, return as string
    # This handles cases where the string itself contains special YAML characters
    return value_str


def format_value(value: Any) -> str:  # noqa: ANN401
    """Format a value for display as YAML.

    Args:
        value: Value to format

    Returns:
        YAML formatted string representation

    """
    # Use yaml.dump for consistent formatting
    yaml_str = yaml.dump(value, default_flow_style=False, sort_keys=False, allow_unicode=True)
    # Remove trailing newline and document end marker that yaml.dump adds
    yaml_str = yaml_str.rstrip()
    if yaml_str.endswith("..."):
        yaml_str = yaml_str[:-3].rstrip()
    return yaml_str


async def handle_config_command(args_text: str, config_path: Path | None = None) -> tuple[str, dict[str, Any] | None]:  # noqa: C901, PLR0911, PLR0912
    """Handle config command execution.

    Args:
        args_text: The command arguments
        config_path: Optional path to config file

    Returns:
        Tuple of (response message, config change dict or None)
        The config change dict contains info needed for confirmation

    """
    operation, args = parse_config_args(args_text)
    path = config_path or DEFAULT_AGENTS_CONFIG

    # Load current config
    config = Config.from_yaml(path)
    config_dict = config.model_dump(exclude_none=True)

    if operation == "show":
        # Show entire config
        yaml_str = yaml.dump(config_dict, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return f"**Current Configuration:**\n```yaml\n{yaml_str}```", None

    if operation == "get":
        if not args:
            return (
                "❌ Please specify a configuration path to get\nExample: `!config get agents.analyst.display_name`",
                None,
            )

        config_path_str = args[0]
        try:
            value = get_nested_value(config_dict, config_path_str)
        except (KeyError, IndexError) as e:
            return f"❌ Configuration path not found: `{config_path_str}`\nError: {e}", None
        else:
            formatted = format_value(value)
            return f"**Configuration value for `{config_path_str}`:**\n```yaml\n{formatted}\n```", None

    elif operation == "set":
        if len(args) < 2:
            return (
                '❌ Please specify a path and value\nExample: `!config set agents.analyst.display_name "New Name"`',
                None,
            )

        config_path_str = args[0]
        # Join remaining args as the value (handles unquoted strings with spaces)
        value_str = " ".join(args[1:])

        # Parse the value - YAML parsing handles both quoted and unquoted formats
        value = parse_value(value_str)

        # Get the current value for comparison
        try:
            old_value = get_nested_value(config_dict, config_path_str)
        except (KeyError, IndexError):
            old_value = None  # Path doesn't exist yet

        # Create a copy to test the change
        test_config_dict = config_dict.copy()

        try:
            # Verify the path exists or can be created
            set_nested_value(test_config_dict, config_path_str, value)

            # Validate the modified config
            Config(**test_config_dict)  # This will raise ValidationError if invalid
        except (KeyError, IndexError) as e:
            return f"❌ Configuration path error: `{config_path_str}`\nError: {e}", None
        except ValidationError as e:
            # Validation failed - explain why
            errors = []
            for error in e.errors():
                location = " → ".join(str(loc) for loc in error["loc"])
                errors.append(f"• {location}: {error['msg']}")
            error_msg = "\n".join(errors)
            return f"❌ Invalid configuration:\n{error_msg}\n\nChanges were NOT applied.", None
        else:
            # Format the preview message
            formatted_old = format_value(old_value) if old_value is not None else "Not set"
            formatted_new = format_value(value)

            preview_msg = (
                f"**Configuration Change Preview**\n\n"
                f"📝 **Path:** `{config_path_str}`\n\n"
                f"**Current value:**\n```yaml\n{formatted_old}\n```\n"
                f"**New value:**\n```yaml\n{formatted_new}\n```\n\n"
                f"React with ✅ to confirm or ❌ to cancel this change."
            )

            # Return the preview and the change info for confirmation
            change_info = {
                "config_path": config_path_str,
                "old_value": old_value,
                "new_value": value,
                "path": str(path),
            }

            return preview_msg, change_info

    elif operation == "parse_error":
        # Handle parsing errors (e.g., unmatched quotes)
        error_msg = args[0] if args else "Unknown parsing error"
        return (
            f"❌ **Command parsing error:**\n{error_msg}\n\n"
            "**Common issues:**\n"
            "• Unmatched quotes: Make sure quotes are properly paired\n"
            '• For JSON arrays/objects, use matching quotes: `["item1", "item2"]`\n'
            "• Or use single quotes consistently: `['item1', 'item2']`\n\n"
            "**Example:**\n"
            '`!config set agents.analyst.tools ["tool1", "tool2"]`'
        ), None

    else:
        available_ops = ["show", "get", "set"]
        return (
            f"❌ Unknown operation: '{operation}'\n"
            f"Available operations: {', '.join(available_ops)}\n\n"
            "Try `!help config` for usage examples."
        ), None


async def apply_config_change(
    config_path_str: str,
    new_value: Any,  # noqa: ANN401
    config_file_path: Path | None = None,
) -> str:
    """Apply a confirmed configuration change.

    Args:
        config_path_str: The configuration path (e.g., "agents.analyst.role")
        new_value: The new value to set
        config_file_path: Optional path to config file

    Returns:
        Success or error message

    """
    path = config_file_path or DEFAULT_AGENTS_CONFIG

    try:
        # Load the current configuration
        config = Config.from_yaml(path)
        config_dict = config.model_dump()

        # Apply the specific change
        set_nested_value(config_dict, config_path_str, new_value)

        # Validate the modified config
        try:
            new_config = Config(**config_dict)
        except ValidationError as ve:
            errors = ["❌ Configuration validation failed:"]
            for error in ve.errors():
                location = " → ".join(str(loc) for loc in error["loc"])
                errors.append(f"• {location}: {error['msg']}")
            error_msg = "\n".join(errors)
            return f"{error_msg}\n\nChanges were NOT applied."

        # Save to file
        new_config.save_to_yaml(path)
        return (  # noqa: TRY300
            f"✅ **Configuration updated successfully!**\n\n"
            f"Changes saved to {path} and will affect new agent interactions."
        )
    except Exception as e:
        logger.exception("Failed to apply config change")
        return f"❌ Failed to apply configuration change: {e}"
