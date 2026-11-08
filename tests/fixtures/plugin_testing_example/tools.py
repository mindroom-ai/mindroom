"""The example plugin's tool, which greets with the greeting its settings name and counts whom it greeted."""

from mindroom.plugin_api import (
    ToolCategory,
    ToolFileAccess,
    Toolkit,
    current_plugin_context,
    register_tool_with_metadata,
    tool,
)


class GreeterTools(Toolkit):
    """Greets people."""

    name = "greeter"

    @tool
    def greet(self, name: str) -> str:
        """Greet someone by name.

        Args:
            name: The person's name.

        """
        context = current_plugin_context("greeter")
        log = context.state_root / "greeted.txt"
        with log.open("a", encoding="utf-8") as greeted:
            greeted.write(f"{name}\n")
        count = len(log.read_text(encoding="utf-8").splitlines())
        return f"{context.settings['greeting']}, {name}! (greeting {count})"


@register_tool_with_metadata(
    name="greeter",
    display_name="Greeter",
    description="Greets people by name",
    file_access=ToolFileAccess.NONE,
    category=ToolCategory.DEVELOPMENT,
)
def greeter_tools() -> type[GreeterTools]:
    """Return the greeter toolkit."""
    return GreeterTools
