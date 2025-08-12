"""Home Assistant tools for MindRoom agents.

This module provides tools for interacting with Home Assistant,
allowing agents to control devices, query states, and execute automations.
"""

import json
from typing import Any
from urllib.parse import urljoin

import httpx
from agno.tools import Toolkit

from mindroom.credentials import get_credentials_manager


class HomeAssistantTools(Toolkit):
    """Tools for interacting with Home Assistant."""

    def __init__(self) -> None:
        """Initialize Home Assistant tools."""
        # Use the credentials manager
        self._creds_manager = get_credentials_manager()
        self._config: dict[str, Any] | None = None

        # Initialize the toolkit with all available methods
        super().__init__(
            name="homeassistant",
            tools=[
                self.get_entity_state,
                self.list_entities,
                self.turn_on,
                self.turn_off,
                self.toggle,
                self.set_brightness,
                self.set_color,
                self.set_temperature,
                self.activate_scene,
                self.trigger_automation,
                self.call_service,
            ],
        )

    def _load_config(self) -> dict[str, Any] | None:
        """Load Home Assistant configuration from unified location."""
        if self._config:
            return self._config

        # Load from credentials manager
        self._config = self._creds_manager.load_credentials("homeassistant")
        return self._config

    async def _api_request(  # noqa: PLR0911
        self,
        method: str,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request to Home Assistant."""
        config = self._load_config()
        if not config:
            return {"error": "Home Assistant is not configured. Please connect through the widget."}

        instance_url = config.get("instance_url")
        token = config.get("access_token") or config.get("long_lived_token")

        if not instance_url or not token:
            return {"error": "Missing Home Assistant credentials"}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=urljoin(instance_url, endpoint),
                    headers={"Authorization": f"Bearer {token}"},
                    json=json_data,
                    timeout=10.0,
                )

                if response.status_code == 401:
                    return {"error": "Invalid authentication token. Please reconnect Home Assistant."}
                if response.status_code not in (200, 201):
                    return {"error": f"API error: {response.text}"}

                return response.json() if response.text else {"success": True}

        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            return {"error": "Connection timeout - check if Home Assistant is accessible"}
        except httpx.RequestError as e:
            return {"error": f"Connection error: {e!s}"}
        except Exception as e:
            return {"error": f"Unexpected error: {e!s}"}

    async def get_entity_state(self, entity_id: str) -> str:
        """Get the current state of a Home Assistant entity.

        Args:
            entity_id: The entity ID (e.g., 'light.living_room', 'switch.bedroom_fan')

        Returns:
            JSON string with entity state information

        """
        result = await self._api_request("GET", f"/api/states/{entity_id}")

        if "error" in result:
            return json.dumps(result)

        return json.dumps(
            {
                "entity_id": result.get("entity_id"),
                "state": result.get("state"),
                "attributes": result.get("attributes", {}),
                "last_changed": result.get("last_changed"),
            },
        )

    async def list_entities(self, domain: str = "") -> str:
        """List all entities in Home Assistant, optionally filtered by domain.

        Args:
            domain: Optional domain to filter by (e.g., 'light', 'switch', 'sensor')

        Returns:
            JSON string with list of entities

        """
        result = await self._api_request("GET", "/api/states")

        if isinstance(result, dict) and "error" in result:
            return json.dumps(result)

        entities = result

        # Filter by domain if specified
        if domain and isinstance(entities, list):
            entities = [e for e in entities if e["entity_id"].startswith(f"{domain}.")]

        # Simplify the response
        entity_list: list[Any] = entities[:50] if isinstance(entities, list) else []
        simplified: list[dict[str, Any]] = [
            {
                "entity_id": e["entity_id"],
                "state": e["state"],
                "friendly_name": e.get("attributes", {}).get("friendly_name", e["entity_id"]),
            }
            for e in entity_list  # Limit to 50 entities to avoid huge responses
        ]

        return json.dumps(simplified)

    async def turn_on(self, entity_id: str) -> str:
        """Turn on a device (light, switch, etc.).

        Args:
            entity_id: The entity ID to turn on (e.g., 'light.living_room')

        Returns:
            JSON string with result

        """
        domain = entity_id.split(".")[0]
        result = await self._api_request(
            "POST",
            f"/api/services/{domain}/turn_on",
            {"entity_id": entity_id},
        )
        return json.dumps(result)

    async def turn_off(self, entity_id: str) -> str:
        """Turn off a device (light, switch, etc.).

        Args:
            entity_id: The entity ID to turn off (e.g., 'light.living_room')

        Returns:
            JSON string with result

        """
        domain = entity_id.split(".")[0]
        result = await self._api_request(
            "POST",
            f"/api/services/{domain}/turn_off",
            {"entity_id": entity_id},
        )
        return json.dumps(result)

    async def toggle(self, entity_id: str) -> str:
        """Toggle a device (if on, turn off; if off, turn on).

        Args:
            entity_id: The entity ID to toggle (e.g., 'switch.bedroom_fan')

        Returns:
            JSON string with result

        """
        domain = entity_id.split(".")[0]
        result = await self._api_request(
            "POST",
            f"/api/services/{domain}/toggle",
            {"entity_id": entity_id},
        )
        return json.dumps(result)

    async def set_brightness(self, entity_id: str, brightness: int) -> str:
        """Set the brightness of a light.

        Args:
            entity_id: The light entity ID (e.g., 'light.living_room')
            brightness: Brightness level (0-255, where 255 is 100%)

        Returns:
            JSON string with result

        """
        if not 0 <= brightness <= 255:
            return json.dumps({"error": "Brightness must be between 0 and 255"})

        result = await self._api_request(
            "POST",
            "/api/services/light/turn_on",
            {
                "entity_id": entity_id,
                "brightness": brightness,
            },
        )
        return json.dumps(result)

    async def set_color(self, entity_id: str, red: int, green: int, blue: int) -> str:
        """Set the color of a light using RGB values.

        Args:
            entity_id: The light entity ID (e.g., 'light.living_room')
            red: Red value (0-255)
            green: Green value (0-255)
            blue: Blue value (0-255)

        Returns:
            JSON string with result

        """
        if not all(0 <= v <= 255 for v in [red, green, blue]):
            return json.dumps({"error": "RGB values must be between 0 and 255"})

        result = await self._api_request(
            "POST",
            "/api/services/light/turn_on",
            {
                "entity_id": entity_id,
                "rgb_color": [red, green, blue],
            },
        )
        return json.dumps(result)

    async def set_temperature(self, entity_id: str, temperature: float) -> str:
        """Set the temperature of a climate device.

        Args:
            entity_id: The climate entity ID (e.g., 'climate.thermostat')
            temperature: Target temperature in the unit configured in Home Assistant

        Returns:
            JSON string with result

        """
        result = await self._api_request(
            "POST",
            "/api/services/climate/set_temperature",
            {
                "entity_id": entity_id,
                "temperature": temperature,
            },
        )
        return json.dumps(result)

    async def activate_scene(self, scene_id: str) -> str:
        """Activate a Home Assistant scene.

        Args:
            scene_id: The scene entity ID (e.g., 'scene.movie_time')

        Returns:
            JSON string with result

        """
        result = await self._api_request(
            "POST",
            "/api/services/scene/turn_on",
            {"entity_id": scene_id},
        )
        return json.dumps(result)

    async def trigger_automation(self, automation_id: str) -> str:
        """Trigger a Home Assistant automation.

        Args:
            automation_id: The automation entity ID (e.g., 'automation.morning_routine')

        Returns:
            JSON string with result

        """
        result = await self._api_request(
            "POST",
            "/api/services/automation/trigger",
            {"entity_id": automation_id},
        )
        return json.dumps(result)

    async def call_service(self, domain: str, service: str, entity_id: str = "", data: str = "") -> str:
        """Call a generic Home Assistant service.

        Args:
            domain: The service domain (e.g., 'light', 'switch', 'notify')
            service: The service name (e.g., 'turn_on', 'toggle', 'send_message')
            entity_id: The entity ID(s) to apply the service to (optional)
            data: Additional service data as JSON string (optional)

        Returns:
            JSON string with result

        """
        service_data = {}

        if entity_id:
            service_data["entity_id"] = entity_id

        if data:
            try:
                additional_data = json.loads(data)
                service_data.update(additional_data)
            except json.JSONDecodeError:
                return json.dumps({"error": "Invalid JSON in data parameter"})

        result = await self._api_request(
            "POST",
            f"/api/services/{domain}/{service}",
            service_data,
        )
        return json.dumps(result)
