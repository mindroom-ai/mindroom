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

_MAX_DESCRIPTION_LEN = 350


def _require_apify() -> type[ApifyClient]:
    """Import and return ``apify_client.ApifyClient``, raising a helpful ImportError when absent."""
    try:
        from apify_client import ApifyClient
    except ImportError as exc:
        msg = "`apify-client` not installed. Please install using `pip install apify-client`"
        raise ImportError(msg) from exc
    return ApifyClient


def _truncate_description(description: str) -> str:
    if len(description) > _MAX_DESCRIPTION_LEN:
        return description[:_MAX_DESCRIPTION_LEN] + "..."
    return description


_PYTHON_TYPE_TO_JSON_TYPE: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (dict, "object"),
    (list, "array"),
)


def _python_value_to_json_type(value: Any) -> str:  # noqa: ANN401
    """Map a Python runtime value to a valid JSON Schema type name."""
    if value is None:
        return "null"
    for py_type, json_type in _PYTHON_TYPE_TO_JSON_TYPE:
        if isinstance(value, py_type):
            return json_type
    return "string"


def _infer_schema_from_value(value: Any) -> dict[str, Any]:  # noqa: ANN401
    """Infer a valid JSON Schema fragment from a Python default or prefill sample value."""
    json_type = _python_value_to_json_type(value)
    if json_type == "object":
        schema: dict[str, Any] = {"type": "object"}
        if value:
            schema["properties"] = {key: _infer_schema_from_value(val) for key, val in value.items()}
        return schema
    if json_type == "array":
        item_schema = _infer_schema_from_value(value[0]) if value else {"type": "string"}
        return {"type": "array", "items": item_schema}
    return {"type": json_type}


def _object_property_schema(raw: dict[str, Any]) -> dict[str, Any]:
    declared_properties = raw.get("properties")
    if isinstance(declared_properties, dict):
        result: dict[str, Any] = {
            "type": "object",
            "properties": {
                prop_key: _property_to_json_schema(prop_val)
                for prop_key, prop_val in declared_properties.items()
                if isinstance(prop_val, dict)
            },
        }
        if isinstance(raw.get("required"), list):
            result["required"] = [field for field in raw["required"] if isinstance(field, str)]
    elif raw.get("editor") == "proxy":
        result = {
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
    else:
        result = {"type": "object"}
        sample = raw.get("default") if isinstance(raw.get("default"), dict) else raw.get("prefill")
        if isinstance(sample, dict) and sample:
            result["properties"] = {
                prop_key: _infer_schema_from_value(prop_val) for prop_key, prop_val in sample.items()
            }

    if "additionalProperties" in raw:
        additional = raw["additionalProperties"]
        if isinstance(additional, bool):
            result["additionalProperties"] = additional
        elif isinstance(additional, dict):
            result["additionalProperties"] = _property_to_json_schema(additional)
    return result


def _array_items_schema(raw: dict[str, Any]) -> dict[str, Any]:
    declared_items = raw.get("items")
    if isinstance(declared_items, dict) and declared_items:
        return _property_to_json_schema(declared_items)

    editor = raw.get("editor")
    if editor == "requestListSources":
        return {
            "type": "object",
            "title": "Request list source",
            "description": "Request list source",
            "properties": {
                "url": {"title": "URL", "type": "string", "description": "URL of the request list source"},
            },
            "required": ["url"],
        }
    if editor == "keyValue":
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        }

    for key in ("prefill", "default"):
        sample = raw.get(key)
        if isinstance(sample, list) and sample:
            return _infer_schema_from_value(sample[0])

    return {"type": "string"}


def _property_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    prop_schema: dict[str, Any] = {}
    if isinstance(raw.get("title"), str) and raw["title"]:
        prop_schema["title"] = raw["title"]
    if isinstance(raw.get("description"), str) and raw["description"]:
        prop_schema["description"] = _truncate_description(raw["description"])
    if "enum" in raw and raw["enum"] is not None:
        prop_schema["enum"] = raw["enum"]
    if "default" in raw:
        prop_schema["default"] = raw["default"]
    elif "prefill" in raw:
        prop_schema["default"] = raw["prefill"]
    return prop_schema


def _property_to_json_schema(raw: dict[str, Any]) -> dict[str, Any]:
    prop_schema = _property_metadata(raw)
    prop_type = raw.get("type")
    if prop_type == "object":
        prop_schema.update(_object_property_schema(raw))
    elif prop_type == "array":
        prop_schema["type"] = "array"
        prop_schema["items"] = _array_items_schema(raw)
    elif prop_type is not None:
        prop_schema["type"] = prop_type
    elif "default" in prop_schema:
        prop_schema.update(_infer_schema_from_value(prop_schema["default"]))
    return prop_schema


def _actor_input_to_json_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Translate an Apify Actor input schema into a pruned JSON Schema object."""
    raw_properties = input_schema.get("properties")
    properties = (
        {key: _property_to_json_schema(meta) for key, meta in raw_properties.items() if isinstance(meta, dict)}
        if isinstance(raw_properties, dict)
        else {}
    )
    raw_required = input_schema.get("required")
    required = [field for field in raw_required if isinstance(field, str)] if isinstance(raw_required, list) else []
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _create_apify_client(token: str) -> ApifyClient:
    """Create an Apify client instance with a MindRoom user-agent suffix."""
    if not token:
        msg = "API token is required to create an Apify client."
        raise ValueError(msg)

    apify_client_cls = _require_apify()
    client = apify_client_cls(token)
    if http_client := getattr(client.http_client, "httpx_client", None):
        http_client.headers["user-agent"] += "; Origin/mindroom"
    return client


def _response_to_dict(response: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return an Apify client response as a dict across apify-client 2.x and 3.x."""
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(by_alias=True)
    msg = f"Unexpected Apify client response type: {type(response).__name__}"
    raise TypeError(msg)


def _actor_id_to_tool_name(actor_id: str) -> str:
    """Turn an Apify Actor ID into a valid tool function name."""
    valid_chars = string.ascii_letters + string.digits + "_-"
    return "apify_actor_" + "".join(char if char in valid_chars else "_" for char in actor_id)


def _get_actor_latest_build(apify_client: ApifyClient, actor_id: str) -> dict[str, Any]:
    """Fetch the default build metadata for an Apify Actor through the authenticated client."""
    actor_client = apify_client.actor(actor_id)
    if not (actor := actor_client.get()):
        msg = f"Actor {actor_id} not found."
        raise ValueError(msg)

    actor_dict = _response_to_dict(actor)
    default_tag = (actor_dict.get("defaultRunOptions") or {}).get("build")
    tagged_builds = actor_dict.get("taggedBuilds") or {}
    tagged_build = (
        tagged_builds.get(default_tag) if isinstance(default_tag, str) and isinstance(tagged_builds, dict) else None
    )
    build_id = tagged_build.get("buildId") if isinstance(tagged_build, dict) else None

    if build_id:
        build = apify_client.build(build_id).get()
    elif hasattr(actor_client, "default_build"):
        build = actor_client.default_build().get()
    else:
        msg = f"Failed to determine the default build for Actor {actor_id}."
        raise ValueError(msg)

    if not build:
        msg = f"Failed to get the latest build data of the Actor {actor_id}."
        raise ValueError(msg)
    return _response_to_dict(build)


class _ApifyTools(Toolkit):
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

        self.client = _create_apify_client(self.apify_api_token)
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
            build = _get_actor_latest_build(self.client, actor_id)
            tool_name = _actor_id_to_tool_name(actor_id)

            actor_definition = build.get("actorDefinition") or {}
            actor_description = actor_definition.get("description") or ""
            if len(actor_description) > _MAX_DESCRIPTION_LEN:
                actor_description = actor_description[:_MAX_DESCRIPTION_LEN] + "...(TRUNCATED, TOO LONG)"

            actor_input = actor_definition.get("input")
            if not actor_input:
                logger.error("apify_actor_input_schema_missing", actor_id=actor_id)
                return

            parameters_schema = _actor_input_to_json_schema(actor_input)
            properties = parameters_schema["properties"]
            required = parameters_schema["required"]

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
        run_id = _response_to_dict(details).get("id")
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
def apify_tools() -> type[Toolkit]:
    """Return Apify tools for web scraping and automation."""
    _require_apify()
    return _ApifyTools
