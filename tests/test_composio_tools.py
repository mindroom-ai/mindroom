"""Composio action registration through the installed SDK, without external calls."""

import json
import socket
from pathlib import Path

import pytest
from agno.tools import Toolkit
from composio.client.collections import ActionModel
from composio_agno import ComposioToolSet

from mindroom.agents import _reject_matrix_room_runtime_tool_function_collisions
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tools.composio import composio_tools


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
    """Exercise the same Toolkit protocol agent assembly consumes."""
    assert isinstance(tool, Toolkit)
    _reject_matrix_room_runtime_tool_function_collisions("composio", tool)
    assert set(tool.get_functions()) == {"github_get_a_repository", "github_get_the_authenticated_user"}
    function = tool.get_functions()["github_get_a_repository"]
    assert function.entrypoint is not None
    function.process_entrypoint()
    assert function.parameters["properties"]["owner"]["type"] == "string"
    assert "owner" in function.parameters["required"]
    assert json.loads(function.entrypoint(owner="example")) == {
        "action": "GITHUB_GET_A_REPOSITORY",
        "params": {"owner": "example"},
        "entity_id": "test-user",
    }


@pytest.mark.usefixtures("offline_composio")
def test_composio_factory_exposes_selected_sdk_actions() -> None:
    """Returning the raw SDK toolset loses the callable Agno tool surface."""
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
        disable_sandbox_proxy=True,
        worker_target=None,
    )

    _assert_registered_actions(tool)


@pytest.mark.usefixtures("offline_composio")
@pytest.mark.parametrize("selection", [{}, {"actions": []}])
def test_composio_requires_explicit_action_selection(selection: dict[str, list[str]]) -> None:
    """Missing selection should explain configuration instead of returning an unusable tool."""
    with pytest.raises(ValueError, match="actions"):
        composio_tools()(api_key="test-key", lock=False, **selection)
