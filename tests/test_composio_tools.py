"""Composio action registration through the native `composio-core` wrapper, without external calls."""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

import pytest
from composio import ComposioToolSet
from composio.client.collections import ActionModel

from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.composio import composio_tools
from tests.agent_tool_helpers import toolkit_offers

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def offline_composio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep SDK tool creation real and replace only remote/setup boundaries."""
    monkeypatch.setattr("composio.tools.toolset.LOCAL_CACHE_DIRECTORY", tmp_path / "composio")

    def reject_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Composio registration attempted a live network call")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(ComposioToolSet, "_validate_connection_ids", lambda _self, **_kwargs: {})
    monkeypatch.setattr(ComposioToolSet, "validate_tools", lambda _self, **_kwargs: None)

    def action_schemas(
        self: ComposioToolSet,
        *,
        actions: list[str] | None = None,
        **kwargs: object,
    ) -> list[ActionModel]:
        schemas = [
            ActionModel(
                name=name,
                description="Read a GitHub resource.",
                parameters={
                    "title": "GitHubResourceParameters",
                    "type": "object",
                    "properties": {"owner": {"type": "string", "description": "Resource owner."}},
                    "required": ["owner"],
                },
                response={"title": "GitHubResourceResponse", "type": "object", "properties": {}},
                appName="github",
                appId="github",
                version="1",
                available_versions=["1"],
                tags=["read"],
            )
            for name in actions or []
        ]
        if kwargs.get("_populate_requested"):
            self._requested_actions.extend(schema.name for schema in schemas)
        return schemas

    def execute_action(
        _self: ComposioToolSet,
        *,
        action: object,
        params: dict[str, object],
        entity_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {"action": str(action), "params": params, "entity_id": entity_id}

    monkeypatch.setattr(ComposioToolSet, "get_action_schemas", action_schemas)
    monkeypatch.setattr(ComposioToolSet, "execute_action", execute_action)


def _assert_registered_actions(tool: Toolkit) -> None:
    """Exercise the same native Toolkit protocol agent assembly consumes."""
    assert isinstance(tool, Toolkit)
    assert set(tool.functions) == {"github_get_a_repository", "github_get_the_authenticated_user"}
    function = toolkit_offers(tool, "github_get_a_repository")
    assert function.parameters["properties"]["owner"]["type"] == "string"
    assert "owner" in function.parameters["required"]
    assert json.loads(function.entrypoint(owner="example")) == {
        "action": "GITHUB_GET_A_REPOSITORY",
        "params": {"owner": "example"},
        "entity_id": "test-user",
    }


@pytest.mark.usefixtures("offline_composio")
def test_composio_factory_exposes_selected_sdk_actions() -> None:
    """Native Composio toolkit wraps selected composio-core action schemas cleanly."""
    tool = composio_tools()(
        api_key="test-key",
        entity_id="test-user",
        actions=["GITHUB_GET_A_REPOSITORY", "GITHUB_GET_THE_AUTHENTICATED_USER"],
        lock=False,
    )

    _assert_registered_actions(tool)


@pytest.mark.usefixtures("offline_composio")
@pytest.mark.parametrize("with_workspace", [False, True])
def test_composio_registry_loads_selected_actions(tmp_path: Path, with_workspace: bool) -> None:
    """Authored action selection must survive loading and workspace wrapping."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    tool = get_tool_by_name(
        "composio",
        runtime_paths,
        credential_overrides={"api_key": "test-key"},
        tool_config_overrides={
            "actions": ["GITHUB_GET_A_REPOSITORY", "GITHUB_GET_THE_AUTHENTICATED_USER"],
            "entity_id": "test-user",
            "lock": False,
        },
        tool_output_workspace_root=tmp_path / "workspace" if with_workspace else None,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    _assert_registered_actions(tool)


@pytest.mark.usefixtures("offline_composio")
@pytest.mark.parametrize("selection", [{}, {"actions": []}])
def test_composio_requires_explicit_action_selection(selection: dict[str, list[str]]) -> None:
    """Missing selection should explain configuration instead of returning an unusable tool."""
    with pytest.raises(ValueError, match="actions"):
        composio_tools()(api_key="test-key", lock=False, **selection)


@pytest.mark.usefixtures("offline_composio")
def test_composio_requires_scoped_api_key_and_ignores_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing API key fails closed instead of falling back to host COMPOSIO_API_KEY."""
    monkeypatch.setenv("COMPOSIO_API_KEY", "host-secret")
    with pytest.raises(ValueError, match="Composio API key"):
        composio_tools()(actions=["GITHUB_GET_A_REPOSITORY"], lock=False)


@pytest.mark.usefixtures("offline_composio")
def test_composio_pins_default_base_url_against_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitted base_url pins to Composio vendor default instead of ambient COMPOSIO_BASE_URL."""
    monkeypatch.setenv("COMPOSIO_BASE_URL", "https://evil.example/api")
    seen_base_urls: list[str] = []

    def capture_validate(self: ComposioToolSet, **_kwargs: object) -> None:
        seen_base_urls.append(self._base_url)

    monkeypatch.setattr(ComposioToolSet, "validate_tools", capture_validate)

    composio_tools()(api_key="test-key", actions=["GITHUB_GET_A_REPOSITORY"], lock=False)
    composio_tools()(
        api_key="test-key",
        base_url="https://custom.composio.example/api",
        actions=["GITHUB_GET_A_REPOSITORY"],
        lock=False,
    )

    assert seen_base_urls == [
        "https://backend.composio.dev/api",
        "https://custom.composio.example/api",
    ]


@pytest.mark.usefixtures("offline_composio")
def test_composio_truncates_long_action_names_and_rejects_collisions() -> None:
    """Action names >64 chars publish <=64-char tool names, dispatch full action IDs, and reject collisions."""
    long_action = "GITHUB_CREATE_REPOSITORY_DISPATCH_EVENT_FOR_LONG_WORKFLOW_AUTOMATION_V1"
    assert len(long_action) > 64

    tool = composio_tools()(api_key="test-key", entity_id="test-user", actions=[long_action], lock=False)
    (published_name,) = tuple(tool.functions)
    assert len(published_name) <= 64
    assert published_name == long_action.lower()[:64]

    function = toolkit_offers(tool, published_name)
    assert json.loads(function.entrypoint(owner="example")) == {
        "action": long_action,
        "params": {"owner": "example"},
        "entity_id": "test-user",
    }

    colliding_action = "GITHUB_CREATE_REPOSITORY_DISPATCH_EVENT_FOR_LONG_WORKFLOW_AUTOMATION_V2"
    with pytest.raises(ValueError, match="collide"):
        composio_tools()(
            api_key="test-key",
            actions=[long_action, colliding_action],
            lock=False,
        )
