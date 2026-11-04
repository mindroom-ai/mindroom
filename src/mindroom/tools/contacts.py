"""Contacts tool metadata; the toolkit takes ``agent_name``, so it is injected for agents with contacts and has no generic factory."""

from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolMetadata
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="contacts",
        file_access=ToolFileAccess.NONE,
        display_name="Contacts",
        description="Ask other people's personal agents or people for contact, and manage the contacts you have",
        docs_url="https://docs.mindroom.chat/agent-contacts/#tools",
        category=ToolCategory.COMMUNICATION,
        requires_primary_runtime=True,
        requires_room_context=True,
        icon="Users",
        icon_color="text-cyan-500",
        config_fields=[],
        dependencies=[],
        function_names=(
            "list_contacts",
            "request_contact",
            "accept_contact",
            "decline_contact",
            "update_contact",
            "remove_contact",
        ),
    ),
)
