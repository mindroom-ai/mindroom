"""Tests for the native Apify toolkit schema translation and authenticated build loading."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import jsonschema
import requests

from mindroom.tool_system.authoring import authored_description, passthrough_parameters
from mindroom.tools import apify as apify_module

if TYPE_CHECKING:
    import pytest


class _FakeBuildClient:
    def __init__(self, build_data: dict[str, Any]) -> None:
        self._build_data = build_data
        self.get_calls = 0

    def get(self) -> dict[str, Any]:
        self.get_calls += 1
        return self._build_data


class _FakeActorClient:
    def __init__(
        self,
        actor_data: dict[str, Any],
        default_build_client: _FakeBuildClient | None = None,
    ) -> None:
        self._actor_data = actor_data
        self._default_build_client = default_build_client
        self.default_build_calls = 0

    def get(self) -> dict[str, Any]:
        return self._actor_data

    def default_build(self) -> _FakeBuildClient:
        self.default_build_calls += 1
        assert self._default_build_client is not None
        return self._default_build_client


class _FakeApifyClient:
    def __init__(
        self,
        token: str,
        *,
        actors: dict[str, _FakeActorClient],
        builds: dict[str, _FakeBuildClient],
    ) -> None:
        self.token = token
        self.http_client = SimpleNamespace()
        self._actors = actors
        self._builds = builds
        self.requested_build_ids: list[str] = []

    def actor(self, actor_id: str) -> _FakeActorClient:
        return self._actors[actor_id]

    def build(self, build_id: str) -> _FakeBuildClient:
        self.requested_build_ids.append(build_id)
        return self._builds[build_id]


def test_apify_schema_preserves_start_urls_editors_and_declared_nested_schemas() -> None:
    """Apify editor schemas and explicit items/properties survive conversion before pruning."""
    input_schema = {
        "properties": {
            "startUrls": {
                "title": "Start URLs",
                "type": "array",
                "description": "URLs to start scraping from",
                "editor": "requestListSources",
                "prefill": [{"url": "https://apify.com"}],
            },
            "explicitRequests": {
                "type": "array",
                "description": "Explicit request item schema",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target URL"},
                        "method": {"type": "string", "enum": ["GET", "POST"]},
                    },
                    "required": ["url"],
                },
            },
            "proxyConfiguration": {
                "type": "object",
                "description": "Proxy settings",
                "editor": "proxy",
                "default": {"useApifyProxy": True},
            },
            "filters": {
                "type": "object",
                "description": "Explicitly typed nested object",
                "properties": {
                    "maxDepth": {"type": "integer", "default": 0},
                    "includeSubdomains": {"type": "boolean", "default": False},
                },
                "required": ["maxDepth"],
            },
            "maxPages": {
                "type": "integer",
                "description": "Zero default must not be dropped",
                "default": 0,
            },
            "followRedirects": {
                "type": "boolean",
                "description": "False default must not be dropped",
                "default": False,
            },
        },
        "required": ["startUrls"],
    }

    schema = apify_module._actor_input_to_json_schema(input_schema)
    props = schema["properties"]

    assert schema["required"] == ["startUrls"]
    assert props["startUrls"]["type"] == "array"
    assert props["startUrls"]["items"]["type"] == "object"
    assert props["startUrls"]["items"]["properties"]["url"]["type"] == "string"
    assert props["startUrls"]["items"]["properties"]["requestsFromUrl"]["type"] == "string"
    assert "required" not in props["startUrls"]["items"]
    assert props["startUrls"]["default"] == [{"url": "https://apify.com"}]
    assert "editor" not in props["startUrls"]
    assert "prefill" not in props["startUrls"]

    assert props["explicitRequests"]["items"] == {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL"},
            "method": {"type": "string", "enum": ["GET", "POST"]},
        },
        "required": ["url"],
    }

    assert props["proxyConfiguration"]["type"] == "object"
    assert props["proxyConfiguration"]["properties"]["useApifyProxy"]["type"] == "boolean"
    assert props["proxyConfiguration"]["required"] == ["useApifyProxy"]
    assert props["proxyConfiguration"]["default"] == {"useApifyProxy": True}

    assert props["filters"]["properties"]["maxDepth"] == {"type": "integer", "default": 0}
    assert props["filters"]["properties"]["includeSubdomains"] == {"type": "boolean", "default": False}
    assert props["filters"]["required"] == ["maxDepth"]
    assert props["maxPages"]["default"] == 0
    assert props["followRedirects"]["default"] is False


def test_apify_default_type_conversion_emits_valid_json_schema_types() -> None:
    """Inferred schemas from Python default values never emit Python type names like str or int."""
    input_schema = {
        "properties": {
            "settings": {
                "type": "object",
                "description": "Object whose properties are inferred from default",
                "default": {
                    "label": "demo",
                    "count": 5,
                    "ratio": 0.75,
                    "enabled": True,
                    "nullable": None,
                    "nested": {"key": "value", "retries": 2},
                    "tags": ["a", "b"],
                },
            },
        },
    }

    schema = apify_module._actor_input_to_json_schema(input_schema)
    nested_props = schema["properties"]["settings"]["properties"]

    assert nested_props == {
        "label": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "enabled": {"type": "boolean"},
        "nullable": {"type": "null"},
        "nested": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "retries": {"type": "integer"},
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    }


def test_apify_fetches_private_actor_build_through_authenticated_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default build metadata must be loaded through the authenticated Apify client, never anonymous requests."""
    build_data = {
        "id": "build-private-123",
        "actorDefinition": {
            "description": "Private scraper",
            "input": {
                "properties": {
                    "startUrls": {
                        "type": "array",
                        "description": "URLs to crawl",
                        "editor": "requestListSources",
                    },
                },
                "required": ["startUrls"],
            },
        },
    }
    build_client = _FakeBuildClient(build_data)
    actor_client = _FakeActorClient(
        {
            "id": "act-private-1",
            "defaultRunOptions": {"build": "latest"},
            "taggedBuilds": {"latest": {"buildId": "build-private-123"}},
        },
    )
    created_clients: list[_FakeApifyClient] = []

    def fake_client_cls(token: str) -> _FakeApifyClient:
        client = _FakeApifyClient(
            token,
            actors={"private/scraper": actor_client},
            builds={"build-private-123": build_client},
        )
        created_clients.append(client)
        return client

    def fail_anonymous_request(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        msg = "Anonymous requests.request must not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(apify_module, "_require_apify", lambda: fake_client_cls)
    monkeypatch.setattr(requests, "request", fail_anonymous_request)
    apify_key = "apify-auth-value"

    toolkit = apify_module.apify_tools()(actors="private/scraper", apify_api_token=apify_key)

    assert len(created_clients) == 1
    assert created_clients[0].token == apify_key
    assert created_clients[0].requested_build_ids == ["build-private-123"]
    assert build_client.get_calls == 1

    fn = toolkit.functions["apify_actor_private_scraper"]
    params = passthrough_parameters(fn)
    assert params is not None
    assert params["properties"]["startUrls"]["items"]["properties"]["url"]["type"] == "string"


def test_apify_falls_back_to_authenticated_default_build_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When taggedBuilds has no matching tag, use actor(actor_id).default_build().get() on the authenticated client."""
    default_build_client = _FakeBuildClient(
        {
            "id": "build-default-456",
            "actorDefinition": {
                "description": "Numeric build actor",
                "input": {
                    "properties": {"query": {"type": "string", "description": "Search query"}},
                    "required": ["query"],
                },
            },
        },
    )
    actor_client = _FakeActorClient(
        {
            "id": "act-2",
            "defaultRunOptions": {"build": "0.1.2"},
            "taggedBuilds": {},
        },
        default_build_client=default_build_client,
    )

    def fake_client_cls(token: str) -> _FakeApifyClient:
        return _FakeApifyClient(token, actors={"org/actor": actor_client}, builds={})

    monkeypatch.setattr(apify_module, "_require_apify", lambda: fake_client_cls)
    apify_key = "apify-auth-value"

    toolkit = apify_module.apify_tools()(actors=["org/actor"], apify_api_token=apify_key)

    assert actor_client.default_build_calls == 1
    assert default_build_client.get_calls == 1
    assert "apify_actor_org_actor" in toolkit.functions


def test_apify_request_list_sources_schema_accepts_url_and_requests_from_url() -> None:
    """Apify requestListSources items must accept both single-URL and Crawlee bulk-import requestsFromUrl forms."""
    schema = apify_module._actor_input_to_json_schema(
        {
            "properties": {
                "startUrls": {
                    "type": "array",
                    "description": "URLs to crawl",
                    "editor": "requestListSources",
                },
            },
            "required": ["startUrls"],
        },
    )

    jsonschema.validate({"startUrls": [{"url": "https://example.org"}]}, schema)
    jsonschema.validate({"startUrls": [{"requestsFromUrl": "https://example.org/urls.txt"}]}, schema)


def test_apify_tool_description_does_not_duplicate_parameter_descriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registered Apify tools must pass actor_description directly without duplicating parameter docs."""
    param_description = "Unique parameter guidance that belongs only in parameters_schema"
    build_data = {
        "id": "build-desc-1",
        "actorDefinition": {
            "description": "Concise Actor summary.",
            "input": {
                "properties": {
                    "query": {
                        "type": "string",
                        "description": param_description,
                    },
                },
                "required": ["query"],
            },
        },
    }
    build_client = _FakeBuildClient(build_data)
    actor_client = _FakeActorClient(
        {
            "id": "act-desc-1",
            "defaultRunOptions": {"build": "latest"},
            "taggedBuilds": {"latest": {"buildId": "build-desc-1"}},
        },
    )

    def fake_client_cls(token: str) -> _FakeApifyClient:
        return _FakeApifyClient(
            token,
            actors={"apify/rag-web-browser": actor_client},
            builds={"build-desc-1": build_client},
        )

    monkeypatch.setattr(apify_module, "_require_apify", lambda: fake_client_cls)
    apify_key = "apify-auth-value"

    toolkit = apify_module.apify_tools()(actors="apify/rag-web-browser", apify_api_token=apify_key)
    fn = toolkit.functions["apify_actor_apify_rag-web-browser"]
    description = authored_description(fn)
    params = passthrough_parameters(fn)

    assert description == "Concise Actor summary."
    assert param_description not in description
    assert "Args:" not in description
    assert params is not None
    assert params["properties"]["query"]["description"] == param_description
