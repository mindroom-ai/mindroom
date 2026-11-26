"""Home Assistant tools for MindRoom agents."""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import quote

import httpx

from mindroom.credentials.manager import CredentialsManager, load_scoped_credentials, save_scoped_credentials
from mindroom.homeassistant_requests import send_homeassistant_request
from mindroom.homeassistant_url_validation import homeassistant_url_error_detail, validate_homeassistant_instance_url
from mindroom.server_fetch_url import ServerFetchAsyncHTTPTransport, ServerFetchUrlError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    unsupported_shared_only_integration_message,
    worker_scope_allows_shared_only_integrations,
)

_ReadAction = Literal["list_entities", "get_state"]
_ControlAction = Literal[
    "turn_on",
    "turn_off",
    "toggle",
    "set_brightness",
    "set_color",
    "set_temperature",
    "activate_scene",
    "trigger_automation",
    "call_service",
]
_POWER_ACTIONS = frozenset({"turn_on", "turn_off", "toggle"})


def _require_non_empty(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return value.strip()


def _error_json(message: str) -> str:
    return json.dumps({"error": message})


class HomeAssistantTools(Toolkit):
    """Tools for monitoring and controlling Home Assistant."""

    def __init__(
        self,
        *,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        """Initialize Home Assistant tools."""
        worker_scope = worker_target.worker_scope if worker_target is not None else None
        routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
        if not worker_scope_allows_shared_only_integrations(worker_scope):
            msg = unsupported_shared_only_integration_message(
                "homeassistant",
                worker_scope,
                agent_name=routing_agent_name,
                subject="Tool",
            )
            raise ValueError(msg)

        if credentials_manager is None:
            msg = "HomeAssistantTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._creds_manager = credentials_manager
        self._worker_target = worker_target

        super().__init__(
            name="homeassistant",
            tools=[self.homeassistant_read, self.homeassistant_control],
        )

    def _load_config(self) -> dict[str, Any] | None:
        """Load Home Assistant configuration from unified location."""
        return load_scoped_credentials(
            "homeassistant",
            credentials_manager=self._creds_manager,
            worker_target=self._worker_target,
        )

    def _save_config(self, config: dict[str, Any]) -> None:
        """Save Home Assistant configuration with a renewed OAuth access token."""
        save_scoped_credentials(
            "homeassistant",
            config,
            credentials_manager=self._creds_manager,
            worker_target=self._worker_target,
        )

    async def _api_request(  # noqa: PLR0911
        self,
        method: str,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401
        """Make an API request to Home Assistant."""
        config = self._load_config()
        if not config:
            return {"error": "Home Assistant is not configured. Please connect through the dashboard."}

        instance_url = config.get("instance_url")
        token = config.get("access_token") or config.get("long_lived_token")

        if not instance_url or not token:
            return {"error": "Missing Home Assistant credentials"}

        allow_private_url = config.get("allow_private_url") is True
        try:
            fetch_url = validate_homeassistant_instance_url(
                instance_url,
                allow_private_url=allow_private_url,
            )
            async with httpx.AsyncClient(
                transport=ServerFetchAsyncHTTPTransport(
                    allow_private_networks=allow_private_url,
                ),
            ) as client:
                response = await send_homeassistant_request(
                    client,
                    fetch_url,
                    config,
                    method,
                    endpoint,
                    json_data=json_data,
                    save_config=self._save_config,
                )

                if response.status_code == 401:
                    return {"error": "Invalid authentication token. Please reconnect Home Assistant."}
                if response.status_code not in (200, 201):
                    return {"error": f"Home Assistant API returned status {response.status_code}"}

                return response.json() if response.text else {"success": True}

        except ServerFetchUrlError as e:
            return {
                "error": homeassistant_url_error_detail(e, allow_private_url=allow_private_url),
            }
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            return {"error": "Connection timeout - check if Home Assistant is accessible"}
        except httpx.RequestError as e:
            return {"error": f"Connection error: {e!s}"}
        except Exception as e:
            return {"error": f"Unexpected error: {e!s}"}

    async def homeassistant_read(
        self,
        action: _ReadAction,
        entity_id: str | None = None,
        domain: str = "",
    ) -> str:
        """Read Home Assistant entity states.

        - `list_entities`: list up to 50 entities (optional `domain` prefix, e.g. `light`, `sensor`)
        - `get_state`: full state and attributes for `entity_id`
        """
        if action == "get_state":
            target = _require_non_empty(entity_id)
            if target is None:
                return _error_json("entity_id is required for get_state")
            result = await self._api_request("GET", f"/api/states/{quote(target, safe='')}")
            if isinstance(result, dict) and "error" in result:
                return json.dumps(result)
            return json.dumps(
                {
                    "entity_id": result.get("entity_id"),
                    "state": result.get("state"),
                    "attributes": result.get("attributes", {}),
                    "last_changed": result.get("last_changed"),
                },
            )

        if action == "list_entities":
            result = await self._api_request("GET", "/api/states")
            if isinstance(result, dict) and "error" in result:
                return json.dumps(result)
            entities = result
            if domain and isinstance(entities, list):
                entities = [e for e in entities if e["entity_id"].startswith(f"{domain}.")]
            entity_list: list[Any] = entities[:50] if isinstance(entities, list) else []
            simplified: list[dict[str, Any]] = [
                {
                    "entity_id": e["entity_id"],
                    "state": e["state"],
                    "friendly_name": e.get("attributes", {}).get("friendly_name", e["entity_id"]),
                }
                for e in entity_list
            ]
            return json.dumps(simplified)

        return _error_json(f"Unsupported Home Assistant read action: {action}")

    async def _call_service(
        self,
        domain: str | None,
        service: str | None,
        entity_id: str | None,
        data: dict[str, Any] | str | None,
    ) -> str:
        clean_domain = _require_non_empty(domain)
        clean_service = _require_non_empty(service)
        if clean_domain is None or clean_service is None:
            return _error_json("domain and service are required for call_service")

        service_data: dict[str, Any] = {}
        if entity_id and entity_id.strip():
            service_data["entity_id"] = entity_id.strip()

        if isinstance(data, dict):
            service_data.update(data)
        elif isinstance(data, str) and data:
            try:
                additional_data = json.loads(data)
            except json.JSONDecodeError:
                return _error_json("Invalid JSON in data parameter")
            if not isinstance(additional_data, dict):
                return _error_json("data parameter must decode to a JSON object")
            service_data.update(additional_data)

        result = await self._api_request(
            "POST",
            f"/api/services/{quote(clean_domain, safe='')}/{quote(clean_service, safe='')}",
            service_data,
        )
        return json.dumps(result)

    @staticmethod
    def _build_entity_service_call(  # noqa: PLR0911
        action: _ControlAction,
        target: str,
        *,
        brightness: int | None,
        rgb_color: list[int] | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, Any]] | str:
        if action in _POWER_ACTIONS:
            entity_domain = target.split(".", maxsplit=1)[0]
            return f"/api/services/{quote(entity_domain, safe='')}/{action}", {"entity_id": target}
        if action == "set_brightness":
            if brightness is None or isinstance(brightness, bool) or not 0 <= brightness <= 255:
                return _error_json("Brightness must be between 0 and 255")
            return "/api/services/light/turn_on", {"entity_id": target, "brightness": brightness}
        if action == "set_color":
            valid_rgb = (
                rgb_color is not None
                and len(rgb_color) == 3
                and all(not isinstance(v, bool) and isinstance(v, int) and 0 <= v <= 255 for v in rgb_color)
            )
            if not valid_rgb or rgb_color is None:
                return _error_json("RGB values must be three integers between 0 and 255")
            return "/api/services/light/turn_on", {"entity_id": target, "rgb_color": list(rgb_color)}
        if action == "set_temperature":
            if temperature is None or isinstance(temperature, bool):
                return _error_json("temperature is required for set_temperature")
            return "/api/services/climate/set_temperature", {"entity_id": target, "temperature": temperature}
        if action == "activate_scene":
            return "/api/services/scene/turn_on", {"entity_id": target}
        if action == "trigger_automation":
            return "/api/services/automation/trigger", {"entity_id": target}
        return _error_json(f"Unsupported Home Assistant control action: {action}")

    async def homeassistant_control(
        self,
        action: _ControlAction,
        entity_id: str | None = None,
        brightness: int | None = None,
        rgb_color: list[int] | None = None,
        temperature: float | None = None,
        domain: str | None = None,
        service: str | None = None,
        data: dict[str, Any] | str | None = None,
    ) -> str:
        """Control Home Assistant devices, scenes, automations, and services.

        - `turn_on`: turn on `entity_id`
        - `turn_off`: turn off `entity_id`
        - `toggle`: toggle `entity_id`
        - `set_brightness`: set light `entity_id` to `brightness` (0-255)
        - `set_color`: set light `entity_id` to `rgb_color` (`[r, g, b]`, each 0-255)
        - `set_temperature`: set climate `entity_id` to `temperature`
        - `activate_scene`: activate scene `entity_id`
        - `trigger_automation`: trigger automation `entity_id`
        - `call_service`: call `domain`/`service` with optional `entity_id` and `data` (dict or JSON string)
        """
        if action == "call_service":
            return await self._call_service(domain, service, entity_id, data)
        target = _require_non_empty(entity_id)
        if target is None:
            return _error_json(f"entity_id is required for {action}")
        built = self._build_entity_service_call(
            action,
            target,
            brightness=brightness,
            rgb_color=rgb_color,
            temperature=temperature,
        )
        if isinstance(built, str):
            return built
        endpoint, payload = built
        return json.dumps(await self._api_request("POST", endpoint, payload))
