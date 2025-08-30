"""Configuration command handling for user-driven config changes."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

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
    except ValueError:
        # If shlex fails, fall back to simple split
        parts = args_text.split()

    if not parts:
        return "show", []

    operation = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    return operation, args


def get_nested_value(data: dict[str, Any], path: str) -> Any:  # noqa: ANN401
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

    for i, key in enumerate(keys):
        # Handle array indexing
        if key.isdigit():
            idx = int(key)
            if not isinstance(current, list):
                partial_path = ".".join(keys[:i])
                msg = f"Cannot index into non-list at '{partial_path}'"
                raise KeyError(msg)
            if idx >= len(current):
                partial_path = ".".join(keys[:i])
                msg = f"Index {idx} out of range for list at '{partial_path}' (length: {len(current)})"
                raise KeyError(msg)
            current = current[idx]
        elif isinstance(current, dict):
            if key not in current:
                partial_path = ".".join(keys[: i + 1])
                available = ", ".join(sorted(current.keys()))
                msg = f"Key '{partial_path}' not found. Available keys: {available}"
                raise KeyError(msg)
            current = current[key]
        else:
            partial_path = ".".join(keys[:i])
            msg = f"Cannot access '{key}' on non-dict value at '{partial_path}'"
            raise KeyError(msg)

    return current


def set_nested_value(data: dict[str, Any], path: str, value: Any) -> None:  # noqa: ANN401, C901
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
    for i, key in enumerate(keys[:-1]):
        # Handle array indexing
        if key.isdigit():
            idx = int(key)
            if not isinstance(current, list):
                partial_path = ".".join(keys[:i])
                msg = f"Cannot index into non-list at '{partial_path}'"
                raise KeyError(msg)
            if idx >= len(current):
                partial_path = ".".join(keys[:i])
                msg = f"Index {idx} out of range for list at '{partial_path}' (length: {len(current)})"
                raise KeyError(msg)
            current = current[idx]
        elif isinstance(current, dict):
            if key not in current:
                # Auto-create missing intermediate dicts
                current[key] = {}
            current = current[key]
        else:
            partial_path = ".".join(keys[:i])
            msg = f"Cannot access '{key}' on non-dict value at '{partial_path}'"
            raise KeyError(msg)

    # Set the final value
    final_key = keys[-1]
    if final_key.isdigit():
        idx = int(final_key)
        if not isinstance(current, list):
            partial_path = ".".join(keys[:-1])
            msg = f"Cannot index into non-list at '{partial_path}'"
            raise KeyError(msg)
        if idx >= len(current):
            partial_path = ".".join(keys[:-1])
            msg = f"Index {idx} out of range for list at '{partial_path}' (length: {len(current)})"
            raise KeyError(msg)
        current[idx] = value
    elif isinstance(current, dict):
        current[final_key] = value
    else:
        partial_path = ".".join(keys[:-1])
        msg = f"Cannot set '{final_key}' on non-dict value at '{partial_path}'"
        raise KeyError(msg)


def parse_value(value_str: str) -> Any:  # noqa: ANN401, PLR0911
    """Parse a string value into appropriate Python type.

    Args:
        value_str: String representation of value

    Returns:
        Parsed value (str, int, float, bool, list, or dict)

    """
    # Try to parse as JSON first (handles lists, dicts, bools, null)
    try:
        return json.loads(value_str)
    except json.JSONDecodeError:
        pass

    # Check for boolean strings
    if value_str.lower() == "true":
        return True
    if value_str.lower() == "false":
        return False

    # Check for None/null
    if value_str.lower() in {"none", "null"}:
        return None

    # Try to parse as number
    try:
        if "." in value_str:
            return float(value_str)
        return int(value_str)
    except ValueError:
        pass

    # Return as string
    return value_str


def format_value(value: Any, indent: int = 0) -> str:  # noqa: ANN401, C901, PLR0911
    """Format a value for display.

    Args:
        value: Value to format
        indent: Indentation level

    Returns:
        Formatted string representation

    """
    indent_str = "  " * indent

    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, val in value.items():
            formatted_val = format_value(val, indent + 1)
            lines.append(f"{indent_str}  {key}: {formatted_val}")
        lines.append(f"{indent_str}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(isinstance(item, (str, int, float, bool, type(None))) for item in value):
            # Simple list - format inline
            formatted_items = [json.dumps(item) if isinstance(item, str) else str(item) for item in value]
            return f"[{', '.join(formatted_items)}]"
        # Complex list - format with indentation
        lines = ["["]
        for item in value:
            formatted_item = format_value(item, indent + 1)
            lines.append(f"{indent_str}  - {formatted_item}")
        lines.append(f"{indent_str}]")
        return "\n".join(lines)
    if isinstance(value, str):
        return json.dumps(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def handle_config_command(args_text: str, config_path: Path | None = None) -> str:  # noqa: C901, PLR0911, PLR0912
    """Handle config command execution.

    Args:
        args_text: The command arguments
        config_path: Optional path to config file

    Returns:
        Response message for the user

    """
    operation, args = parse_config_args(args_text)
    path = config_path or DEFAULT_AGENTS_CONFIG

    try:
        # Load current config
        config = Config.from_yaml(path)
        config_dict = config.model_dump(exclude_none=True)

        if operation == "show":
            # Show entire config
            yaml_str = yaml.dump(config_dict, default_flow_style=False, sort_keys=False, allow_unicode=True)
            return f"**Current Configuration:**\n```yaml\n{yaml_str}```"

        if operation == "get":
            if not args:
                return (
                    "❌ Please specify a configuration path to get\nExample: `!config get agents.analyst.display_name`"
                )

            config_path_str = args[0]
            try:
                value = get_nested_value(config_dict, config_path_str)
            except KeyError as e:
                return f"❌ {e}"
            else:
                formatted = format_value(value)
                return f"**Configuration value for `{config_path_str}`:**\n```json\n{formatted}\n```"

        elif operation == "set":
            if len(args) < 2:
                return (
                    '❌ Please specify a path and value\nExample: `!config set agents.analyst.display_name "New Name"`'
                )

            config_path_str = args[0]
            # Join remaining args as the value (handles unquoted strings with spaces)
            value_str = " ".join(args[1:])
            value = parse_value(value_str)

            try:
                # Verify the path exists or can be created
                set_nested_value(config_dict, config_path_str, value)

                # Validate the modified config
                new_config = Config(**config_dict)

                # Save to file
                new_config.save_to_yaml(path)
            except KeyError as e:
                return f"❌ {e}"
            except ValidationError as e:
                # Validation failed - explain why
                errors = []
                for error in e.errors():
                    location = " → ".join(str(loc) for loc in error["loc"])
                    errors.append(f"• {location}: {error['msg']}")
                error_msg = "\n".join(errors)
                return f"❌ Invalid configuration:\n{error_msg}\n\nChanges were NOT saved."
            else:
                formatted_value = format_value(value)
                return (
                    f"✅ **Configuration updated successfully!**\n\n"
                    f"Set `{config_path_str}` to:\n```json\n{formatted_value}\n```\n\n"
                    f"Changes saved to {path} and will affect new agent interactions."
                )

        else:
            available_ops = ["show", "get", "set"]
            return (
                f"❌ Unknown operation: '{operation}'\n"
                f"Available operations: {', '.join(available_ops)}\n\n"
                "Try `!help config` for usage examples."
            )

    except FileNotFoundError:
        return f"❌ Configuration file not found: {path}"
    except Exception as e:
        logger.exception("Error handling config command")
        return f"❌ Error processing config command: {e!s}"
