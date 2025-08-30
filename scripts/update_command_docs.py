#!/usr/bin/env python3
"""Auto-generate and update command documentation across the codebase."""
# ruff: noqa: RUF001

from __future__ import annotations

import re

# Add the src directory to the path so we can import mindroom modules
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mindroom.commands import COMMAND_DOCS, CommandType


def generate_command_list_markdown() -> str:
    """Generate markdown formatted list of commands."""
    lines = []
    for cmd_type in CommandType:
        if cmd_type in COMMAND_DOCS and cmd_type != CommandType.UNKNOWN:
            syntax, description = COMMAND_DOCS[cmd_type]
            lines.append(f"- `{syntax}` - {description}")
    return "\n".join(lines)


def update_readme_commands(readme_path: Path) -> None:
    """Update the Available Commands section in README.md."""
    content = readme_path.read_text()

    # Find the Available Commands section
    pattern = r"(### Available Commands\n)(.*?)(\n\n## )"

    # Generate the new command list
    command_list = generate_command_list_markdown()
    new_section = f"\\1{command_list}\\3"

    # Replace the section
    updated_content = re.sub(pattern, new_section, content, flags=re.DOTALL)

    if updated_content != content:
        readme_path.write_text(updated_content)
        print(f"✅ Updated {readme_path}")
    else:
        print(f"ℹ No changes needed in {readme_path}")


def get_command_list_for_help() -> str:
    """Generate the command list for the help text."""
    lines = []
    for cmd_type in CommandType:
        if cmd_type in COMMAND_DOCS and cmd_type != CommandType.UNKNOWN:
            syntax, description = COMMAND_DOCS[cmd_type]
            lines.append(f"- `{syntax}` - {description}")
    return "\n".join(lines)


def update_commands_help_text(commands_path: Path) -> None:
    """Update the general help text in commands.py to match COMMAND_DOCS."""
    content = commands_path.read_text()

    # Find the general help return statement
    pattern = r'(# General help\s+return """)\*\*Available Commands\*\*\n\n(.*?)(""")'

    # Get the command list
    command_list = get_command_list_for_help()

    # Build the new help text
    help_sections = []
    help_sections.append("**Available Commands**")
    help_sections.append("")
    help_sections.append(command_list)
    help_sections.append("")
    help_sections.append("**Scheduling Features:**")
    help_sections.append("- Time-based and event-driven workflows")
    help_sections.append("- Recurring tasks with cron-style scheduling (daily, weekly, hourly)")
    help_sections.append("- Agent workflows - mention agents to have them collaborate on scheduled tasks")
    help_sections.append('- Natural language time parsing - "tomorrow", "in 5 minutes", "every Monday"')
    help_sections.append("")
    help_sections.append("Note: All commands only work within threads, not in main room messages")
    help_sections.append("(except !widget which works in the main room).")
    help_sections.append("")
    help_sections.append("For detailed help on a command, use: `!help <command>`")

    replacement = f'\\1**Available Commands**\n\n{command_list}\n\n**Scheduling Features:**\n- Time-based and event-driven workflows\n- Recurring tasks with cron-style scheduling (daily, weekly, hourly)\n- Agent workflows - mention agents to have them collaborate on scheduled tasks\n- Natural language time parsing - "tomorrow", "in 5 minutes", "every Monday"\n\nNote: All commands only work within threads, not in main room messages\n(except !widget which works in the main room).\n\nFor detailed help on a command, use: `!help <command>`\\3'

    # Replace the help text
    updated_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

    if updated_content != content:
        commands_path.write_text(updated_content)
        print(f"✅ Updated {commands_path}")
    else:
        print(f"ℹ No changes needed in {commands_path}")


def check_undocumented_commands(commands_path: Path) -> None:
    """Check for any commands that might not be documented."""
    content = commands_path.read_text()

    # Find all CommandType enum values - only within the CommandType class
    # First, extract the CommandType enum class content
    class_match = re.search(r"class CommandType\(Enum\):.*?\n\n", content, re.DOTALL)
    if not class_match:
        print("⚠️ Could not find CommandType enum class")
        return

    class_content = class_match.group(0)

    # Find all enum values within the class
    enum_pattern = r'^\s+(\w+)\s*=\s*"(\w+)"'
    enum_matches = re.findall(enum_pattern, class_content, re.MULTILINE)

    # Check which ones are in COMMAND_DOCS
    undocumented = []
    for enum_name, enum_value in enum_matches:
        cmd_type = f"CommandType.{enum_name}"
        if cmd_type not in content.split("COMMAND_DOCS = {")[1].split("}")[0] and enum_name != "UNKNOWN":
            undocumented.append((enum_name, enum_value))

    if undocumented:
        print("\n⚠️ Warning: The following commands are not documented in COMMAND_DOCS:")
        for name, value in undocumented:
            print(f"  - CommandType.{name} ('{value}')")
        print("\nPlease add them to COMMAND_DOCS in commands.py")
    else:
        print("✅ All commands are documented")


def main() -> None:
    """Main function to update all command documentation."""
    # Get the project root
    project_root = Path(__file__).parent.parent

    # Paths to files that need updating
    readme_path = project_root / "README.md"
    commands_path = project_root / "src" / "mindroom" / "commands.py"

    print("🔄 Updating command documentation...")
    print()

    # Check for undocumented commands first
    check_undocumented_commands(commands_path)
    print()

    # Update README.md
    update_readme_commands(readme_path)

    # Update commands.py help text
    update_commands_help_text(commands_path)

    print()
    print("✨ Documentation update complete!")
    print()
    print("📝 Summary of available commands:")
    print(generate_command_list_markdown())


if __name__ == "__main__":
    main()
