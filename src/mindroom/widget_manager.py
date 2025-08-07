"""Matrix widget management for MindRoom configuration UI."""

import logging
from typing import Any

from nio import AsyncClient, RoomPutStateError

logger = logging.getLogger(__name__)


class WidgetManager:
    """Manages MindRoom configuration widget in Matrix rooms."""

    def __init__(self, client: AsyncClient, widget_url: str = "http://localhost:3001/matrix-widget.html"):
        """
        Initialize the widget manager.

        Args:
            client: The Matrix client instance
            widget_url: URL of the MindRoom configuration widget
        """
        self.client = client
        self.widget_url = widget_url
        self.widget_state_key = "mindroom_config"

    async def add_widget_to_room(
        self, room_id: str, widget_name: str = "MindRoom Configuration", widget_url: str | None = None
    ) -> bool:
        """
        Add the MindRoom configuration widget to a Matrix room.

        Args:
            room_id: The Matrix room ID
            widget_name: Display name for the widget
            widget_url: Optional custom widget URL (uses default if not provided)

        Returns:
            True if successful, False otherwise
        """
        url = widget_url or self.widget_url

        # Create the widget state event content
        widget_content = {
            "type": "custom",
            "url": url,
            "name": widget_name,
            "data": {"title": widget_name, "curl": url.replace("/matrix-widget.html", "")},
            "creatorUserId": self.client.user_id,
            "id": self.widget_state_key,
        }

        try:
            # Send the state event to add the widget
            response = await self.client.room_put_state(
                room_id=room_id,
                event_type="im.vector.modular.widgets",
                state_key=self.widget_state_key,
                content=widget_content,
            )

            if isinstance(response, RoomPutStateError):
                logger.error(f"Failed to add widget to room {room_id}: {response.message}")
                return False

            logger.info(f"Successfully added widget to room {room_id}")

            # Send a notification message
            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": (
                        "✅ MindRoom Configuration widget has been added to this room!\n"
                        "• Pin the widget to keep it visible\n"
                        "• All room members can access the configuration\n"
                        "• Changes are synchronized in real-time"
                    ),
                    "format": "org.matrix.custom.html",
                    "formatted_body": (
                        "<p>✅ <strong>MindRoom Configuration widget has been added to this room!</strong></p>"
                        "<ul>"
                        "<li>Pin the widget to keep it visible</li>"
                        "<li>All room members can access the configuration</li>"
                        "<li>Changes are synchronized in real-time</li>"
                        "</ul>"
                    ),
                },
            )

            return True

        except Exception as e:
            logger.error(f"Error adding widget to room {room_id}: {e}")
            return False

    async def remove_widget_from_room(self, room_id: str) -> bool:
        """
        Remove the MindRoom configuration widget from a Matrix room.

        Args:
            room_id: The Matrix room ID

        Returns:
            True if successful, False otherwise
        """
        try:
            # Send empty content to remove the widget
            response = await self.client.room_put_state(
                room_id=room_id, event_type="im.vector.modular.widgets", state_key=self.widget_state_key, content={}
            )

            if isinstance(response, RoomPutStateError):
                logger.error(f"Failed to remove widget from room {room_id}: {response.message}")
                return False

            logger.info(f"Successfully removed widget from room {room_id}")

            # Send a notification message
            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": "MindRoom Configuration widget has been removed from this room."},
            )

            return True

        except Exception as e:
            logger.error(f"Error removing widget from room {room_id}: {e}")
            return False

    async def update_widget_url(self, room_id: str, new_url: str) -> bool:
        """
        Update the widget URL in a room.

        Args:
            room_id: The Matrix room ID
            new_url: The new widget URL

        Returns:
            True if successful, False otherwise
        """
        # Get current widget state
        try:
            state_response = await self.client.room_get_state_event(
                room_id=room_id, event_type="im.vector.modular.widgets", state_key=self.widget_state_key
            )

            if not state_response:
                logger.error(f"No widget found in room {room_id}")
                return False

            # Update the URL
            widget_content = state_response.content
            widget_content["url"] = new_url
            widget_content["data"]["curl"] = new_url.replace("/matrix-widget.html", "")

            # Send updated state
            response = await self.client.room_put_state(
                room_id=room_id,
                event_type="im.vector.modular.widgets",
                state_key=self.widget_state_key,
                content=widget_content,
            )

            if isinstance(response, RoomPutStateError):
                logger.error(f"Failed to update widget URL in room {room_id}: {response.message}")
                return False

            logger.info(f"Successfully updated widget URL in room {room_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating widget URL in room {room_id}: {e}")
            return False

    async def get_widget_info(self, room_id: str) -> dict[str, Any] | None:
        """
        Get information about the widget in a room.

        Args:
            room_id: The Matrix room ID

        Returns:
            Widget information dict or None if no widget exists
        """
        try:
            state_response = await self.client.room_get_state_event(
                room_id=room_id, event_type="im.vector.modular.widgets", state_key=self.widget_state_key
            )

            if state_response and state_response.content:
                content: dict[str, Any] = state_response.content
                return content

            return None

        except Exception as e:
            logger.error(f"Error getting widget info from room {room_id}: {e}")
            return None

    async def list_room_widgets(self, room_id: str) -> dict[str, Any]:
        """
        List all widgets in a room.

        Args:
            room_id: The Matrix room ID

        Returns:
            Dictionary of widget state_key -> widget content
        """
        widgets = {}

        try:
            # Get all state events of widget type
            state_response = await self.client.room_get_state(room_id)

            for event in state_response.events:
                if event["type"] == "im.vector.modular.widgets":
                    state_key = event.get("state_key", "")
                    widgets[state_key] = event.get("content", {})

            return widgets

        except Exception as e:
            logger.error(f"Error listing widgets in room {room_id}: {e}")
            return {}


# Command handler integration
async def handle_widget_commands(
    room_id: str, sender: str, command: str, args: list, widget_manager: WidgetManager
) -> str:
    """
    Handle widget-related commands in chat.

    Commands:
        /mindroom widget add - Add the configuration widget
        /mindroom widget remove - Remove the configuration widget
        /mindroom widget update <url> - Update widget URL
        /mindroom widget info - Show widget information
    """
    if not args:
        return "Usage: /mindroom widget [add|remove|update|info]"

    action = args[0].lower()

    if action == "add":
        success = await widget_manager.add_widget_to_room(room_id)
        if success:
            return "Widget added successfully! Pin it to keep it visible."
        else:
            return "Failed to add widget. Check permissions and try again."

    elif action == "remove":
        success = await widget_manager.remove_widget_from_room(room_id)
        if success:
            return "Widget removed successfully."
        else:
            return "Failed to remove widget. Check permissions and try again."

    elif action == "update":
        if len(args) < 2:
            return "Usage: /mindroom widget update <url>"
        new_url = args[1]
        success = await widget_manager.update_widget_url(room_id, new_url)
        if success:
            return f"Widget URL updated to: {new_url}"
        else:
            return "Failed to update widget URL."

    elif action == "info":
        info = await widget_manager.get_widget_info(room_id)
        if info:
            return f"Widget: {info.get('name', 'Unknown')}\nURL: {info.get('url', 'Unknown')}"
        else:
            return "No MindRoom widget found in this room."

    else:
        return f"Unknown widget action: {action}"
