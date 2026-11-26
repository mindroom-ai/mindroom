"""Apify Actor execution and dynamic tool registration toolkit."""

from __future__ import annotations

import json
import string
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, passthrough_tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from apify_client import ApifyClient

logger = get_logger(__name__)

MAX_DESCRIPTION_LEN = 350
REQUESTS_TIMEOUT_SECS = 300
APIFY_API_ENDPOINT_GET_DEFAULT_BUILD = "https://api.apify.com/v2/acts/{actor_id}/builds/default"


def _require_apify() -> type[ApifyClient]:
    """Import and return ``apify_client.ApifyClient``, raising a helpful ImportError when absent."""
    try:
        from apify_client import ApifyClient
    except ImportError as exc:
        msg = "`apify-client` not installed. Please install using `pip install apify-client`"
        raise ImportError(msg) from exc
    return ApifyClient


def _infer_array_item_type(prop: dict[str, Any]) -> str:
    type_map = {
        "string": "string",
        "int": "number",
        "float": "number",
        "dict": "object",
        "list": "array",
        "bool": "boolean",
        "none": "null",
    }
    if prop.get("items", {}).get("type"):
        return str(prop["items"]["type"])
    for key in ("prefill", "default"):
        sample = prop.get(key)
        if sample:
            return type_map.get(type(sample[0]).__name__.lower(), "string")
    return "string"


def _object_property_schema(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("editor") == "proxy":
        result: dict[str, Any] = {
            "type": "object",
            "properties": {
                "useApifyProxy": {
                    "title": "Use Apify Proxy",
                    "type": "boolean",
                    "description": "Whether to use Apify Proxy - ALWAYS SET TO TRUE.",
                    "default": True,
                    "examples": [True],
                },
            },
            "required": ["useApifyProxy"],
        }
        if "default" in value:
            result["default"] = value["default"]
        return result

    result = {"type": "object"}
    if "default" in value:
        result["default"] = value["default"]
        nested: dict[str, Any] = {}
        for prop_key, prop_val in value.get("properties", value["default"]).items():
            nested_type = prop_val["type"] if isinstance(prop_val, dict) else type(prop_val).__name__.lower()
            nested[prop_key] = {"type": "boolean" if nested_type == "bool" else nested_type}
        result["properties"] = nested
    return result


def _property_to_json_schema(value: dict[str, Any]) -> dict[str, Any]:
    prop_schema: dict[str, Any] = {}
    if "enum" in value:
        prop_schema["enum"] = value["enum"]
    if "default" in value:
        prop_schema["default"] = value["default"]
    elif "prefill" in value:
        prop_schema["default"] = value["prefill"]
    if "description" in value:
        prop_schema["description"] = value["description"]

    prop_type = value.get("type")
    if prop_type == "object":
        prop_schema.update(_object_property_schema(value))
    elif prop_type == "array" and value.get("editor") == "requestListSources":
        prop_schema["type"] = "array"
        prop_schema["items"] = {
            "type": "object",
            "title": "Request list source",
            "description": "Request list source",
            "properties": {
                "url": {"title": "URL", "type": "string", "description": "URL of the request list source"},
            },
        }
    elif prop_type == "array":
        prop_schema["type"] = "array"
        prop_schema["items"] = {
            "type": _infer_array_item_type(value),
            "title": value.get("title", "Item"),
            "description": "Item",
        }
    else:
        prop_schema["type"] = prop_type
    return prop_schema


def props_to_json_schema(input_dict: dict[str, Any], required_fields: list[str] | None = None) -> dict[str, Any]:
    """Convert pruned Apify Actor input properties into a JSON Schema object."""
    return {
        "type": "object",
        "properties": {key: _property_to_json_schema(value) for key, value in input_dict.items()},
        "required": required_fields or [],
    }


def create_apify_client(token: str) -> ApifyClient:
    """Create an Apify client instance with a MindRoom user-agent suffix."""
    if not token:
        msg = "API token is required to create an Apify client."
        raise ValueError(msg)

    apify_client_cls = _require_apify()
    client = apify_client_cls(token)
    if http_client := getattr(client.http_client, "httpx_client", None):
        http_client.headers["user-agent"] += "; Origin/mindroom"
    return client


def response_to_dict(response: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return an Apify client response as a dict across apify-client 2.x and 3.x."""
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(by_alias=True)
    msg = f"Unexpected Apify client response type: {type(response).__name__}"
    raise TypeError(msg)


def actor_id_to_tool_name(actor_id: str) -> str:
    """Turn an Apify Actor ID into a valid tool function name."""
    valid_chars = string.ascii_letters + string.digits + "_-"
    return "apify_actor_" + "".join(char if char in valid_chars else "_" for char in actor_id)


def get_actor_latest_build(apify_client: ApifyClient, actor_id: str) -> dict[str, Any]:
    """Fetch the default build metadata for an Apify Actor."""
    import requests

    if not (actor := apify_client.actor(actor_id).get()):
        msg = f"Actor {actor_id} not found."
        raise ValueError(msg)

    if not (actor_obj_id := response_to_dict(actor).get("id")):
        msg = f"Failed to get the Actor object ID for {actor_id}."
        raise ValueError(msg)

    url = APIFY_API_ENDPOINT_GET_DEFAULT_BUILD.format(actor_id=actor_obj_id)
    response = requests.request("GET", url, timeout=REQUESTS_TIMEOUT_SECS)
    build = response.json()
    if not isinstance(build, dict):
        msg = f"Failed to get the latest build of the Actor {actor_id}."
        raise TypeError(msg)

    if (data := build.get("data")) is None:
        msg = f"Failed to get the latest build data of the Actor {actor_id}."
        raise ValueError(msg)
    return data


def prune_actor_input_schema(input_schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Extract and trim input properties and required fields from an Actor build schema."""
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])

    properties_out: dict[str, Any] = {}
    for item, meta in properties.items():
        properties_out[item] = {}
        if desc := meta.get("description"):
            properties_out[item]["description"] = (
                desc[:MAX_DESCRIPTION_LEN] + "..." if len(desc) > MAX_DESCRIPTION_LEN else desc
            )
        for key_name in ("type", "default", "prefill", "enum"):
            if value := meta.get(key_name):
                properties_out[item][key_name] = value

    return properties_out, required


class ApifyTools(Toolkit):
    """Run Apify Actors registered dynamically from their build input schemas."""

    def __init__(
        self,
        actors: str | list[str] | None = None,
        apify_api_token: str | None = None,
    ) -> None:
        _require_apify()
        self.apify_api_token = apify_api_token
        if not self.apify_api_token:
            msg = "APIFY_API_TOKEN environment variable or apify_api_token parameter must be set"
            raise ValueError(msg)

        self.client = create_apify_client(self.apify_api_token)
        super().__init__(name="ApifyTools", tools=[])

        if actors:
            actor_list = (
                [part.strip() for part in actors.split(",") if part.strip()] if isinstance(actors, str) else actors
            )
            for actor_id in actor_list:
                self.register_actor(actor_id)

    def register_actor(self, actor_id: str) -> None:
        """Register an Apify Actor as a function in the toolkit.

        Args:
            actor_id: ID of the Apify Actor to register (e.g., 'apify/web-scraper').

        """
        try:
            build = get_actor_latest_build(self.client, actor_id)
            tool_name = actor_id_to_tool_name(actor_id)

            actor_description = build.get("actorDefinition", {}).get("description", "")
            if len(actor_description) > MAX_DESCRIPTION_LEN:
                actor_description = actor_description[:MAX_DESCRIPTION_LEN] + "...(TRUNCATED, TOO LONG)"

            actor_input = build.get("actorDefinition", {}).get("input")
            if not actor_input:
                logger.error("apify_actor_input_schema_missing", actor_id=actor_id)
                return

            properties, required = prune_actor_input_schema(actor_input)
            parameters_schema = props_to_json_schema(properties, required)

            docstring = f"{actor_description}\n\nArgs:\n"
            for param_name, param_info in properties.items():
                param_type = param_info.get("type", "any")
                param_desc = param_info.get("description", "No description available")
                required_marker = "(required)" if param_name in required else "(optional)"
                docstring += f"    {param_name} ({param_type}): {required_marker} {param_desc}\n"
            docstring += "\nReturns:\n    str: JSON string containing the Actor's output dataset\n"

            @passthrough_tool(name=tool_name, description=docstring, parameters=parameters_schema)
            def actor_function(**kwargs: Any) -> str:  # noqa: ANN401
                try:
                    return self._run_actor(actor_id, kwargs)
                except Exception as exc:
                    error_msg = f"Error running Apify Actor {actor_id}: {exc}"
                    logger.exception("apify_actor_failed", actor_id=actor_id)
                    return json.dumps([{"error": error_msg}])

            actor_function.__name__ = tool_name
            actor_function.__doc__ = docstring
            self.functions[tool_name] = actor_function
            logger.info("apify_actor_registered", actor_id=actor_id, tool_name=tool_name)
        except Exception:
            logger.exception("apify_actor_registration_failed", actor_id=actor_id)

    def _run_actor(self, actor_id: str, kwargs: dict[str, Any]) -> str:
        run_input = kwargs["kwargs"] if len(kwargs) == 1 and "kwargs" in kwargs else kwargs
        logger.info("apify_running_actor", actor_id=actor_id)
        details = self.client.actor(actor_id=actor_id).call(run_input=run_input)
        if details is None:
            msg = f"Actor: {actor_id} was not started properly and details about the run were not returned"
            raise ValueError(msg)
        run_id = response_to_dict(details).get("id")
        if run_id is None:
            msg = f"Run ID not found in the run details for Actor: {actor_id}"
            raise ValueError(msg)
        run = self.client.run(run_id=run_id)
        results = run.dataset().list_items(clean=True).items
        return json.dumps(results)


@register_tool_with_metadata(
    name="apify",
    file_access=ToolFileAccess.NONE,
    display_name="Apify",
    description="Web scraping, crawling, data extraction, and web automation platform with ready-to-use Actors",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API token
    setup_type=SetupType.API_KEY,  # Uses API token authentication
    icon="FaCode",  # Web scraping/automation icon
    icon_color="text-blue-600",  # Apify brand color
    config_fields=[
        # Authentication
        ConfigField(
            name="apify_api_token",
            env=("APIFY_API_TOKEN",),
            label="API Token",
            type="password",
            required=False,
            placeholder="apify_api_...",
            description="Apify API token for authentication",
        ),
        # Configuration
        ConfigField(
            name="actors",
            label="Actors",
            type="text",
            required=False,
            placeholder="apify/rag-web-browser,compass/crawler-google-places",
            description="Single Actor ID as string or comma-separated list of Actor IDs to register as individual tools (e.g., 'apify/rag-web-browser' for web content extraction)",
        ),
    ],
    dependencies=["apify-client"],  # Required dependency for Apify integration
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#apify",
    function_names=("register_actor",),
)
def apify_tools() -> type[ApifyTools]:
    """Return Apify tools for web scraping and automation."""
    _require_apify()
    return ApifyTools
