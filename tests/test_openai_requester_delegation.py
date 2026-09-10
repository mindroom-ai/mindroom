"""Requester-bound API authentication and real delegation authorization checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle, openai_compat
from mindroom.api.main import initialize_api_app
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.tool_system.runtime_context import get_detached_requester_context, get_tool_runtime_context
from tests.identity_helpers import persist_entity_accounts

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mindroom.ai import ResponseTurnContext
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass
class _ApiHarness:
    client: TestClient
    config: Config
    runtime_paths: RuntimePaths


@pytest.fixture
def api(tmp_path: Path) -> Iterator[_ApiHarness]:
    """Build an authenticated API with one permitted and one forbidden specialist."""
    config = Config(
        agents={
            "leader": AgentConfig(
                display_name="Leader",
                delegate_to=["specialist", "forbidden"],
                access=ResponderAccessConfig(users=["@alice:example.org"]),
            ),
            "specialist": AgentConfig(
                display_name="Specialist",
                access=ResponderAccessConfig(users=["@alice:example.org"]),
            ),
            "forbidden": AgentConfig(
                display_name="Forbidden",
                access=ResponderAccessConfig(users=["@bob:example.org"]),
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )
    config.authorization.aliases = {"@alice:example.org": ["@bridge:example.org"]}
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={
            "OPENAI_COMPAT_API_KEYS": "alice-key,bob-key,legacy-key,bridge-key",
            "OPENAI_COMPAT_API_KEY_REQUESTERS": json.dumps(
                {
                    "alice-key": "@alice:example.org",
                    "bob-key": "@bob:example.org",
                    "bridge-key": "@bridge:example.org",
                    "revoked-key": "@alice:example.org",
                },
            ),
        },
    )
    persist_entity_accounts(config, runtime_paths)
    app = FastAPI()
    app.include_router(openai_compat.router)
    initialize_api_app(app, runtime_paths)
    config_lifecycle.require_api_state(app).snapshot.runtime_config = config
    with patch.object(openai_compat, "_load_config", return_value=(config, runtime_paths)), TestClient(app) as client:
        yield _ApiHarness(client, config, runtime_paths)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("key", ["alice-key", "bridge-key"])
def test_mapped_request_delegates_with_canonical_identity(api: _ApiHarness, stream: bool, key: str) -> None:
    """Run the real delegate tool from both API drivers, including after an SSE yield."""
    child_calls: list[ResponseTurnContext] = []

    async def child(ctx: ResponseTurnContext, **kwargs: object) -> str:
        child_calls.append(ctx)
        assert ctx.requester_id == "@alice:example.org"
        assert ctx.room_id is None
        assert kwargs["include_openai_compat_guidance"] is True
        assert get_tool_runtime_context() is None
        assert get_detached_requester_context() is not None
        return "specialist result"

    async def delegate(
        ctx: ResponseTurnContext,
        *,
        execution_identity: ToolExecutionIdentity,
        **_kwargs: object,
    ) -> str:
        assert ctx.requester_id == "@alice:example.org"
        assert execution_identity.requester_id == ctx.requester_id
        tool = DelegateTools("leader", ["specialist"], api.runtime_paths, api.config, execution_identity)
        return await tool.delegate_task("specialist", "help")

    async def streaming(
        ctx: ResponseTurnContext,
        *,
        execution_identity: ToolExecutionIdentity,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        yield "Working: "
        yield await delegate(ctx, execution_identity=execution_identity)

    with (
        patch.object(openai_compat, "ai_response", side_effect=delegate),
        patch.object(openai_compat, "stream_agent_response", side_effect=streaming),
        patch("mindroom.custom_tools.delegate.ai_response", side_effect=child),
    ):
        response = api.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "X-Requester-ID": "@bob:example.org"},
            json={
                "model": "leader",
                "messages": [{"role": "user", "content": "help"}],
                "stream": stream,
                "user": "@bob:example.org",
            },
        )
    assert response.status_code == 200
    assert "specialist result" in response.text
    assert len(child_calls) == 1
    assert get_detached_requester_context() is None


@pytest.mark.parametrize(("key", "target"), [("legacy-key", "specialist"), ("alice-key", "forbidden")])
def test_delegation_fails_closed(api: _ApiHarness, key: str, target: str) -> None:
    """Unmapped callers and forbidden targets never reach child execution."""

    async def delegate(
        _ctx: ResponseTurnContext,
        *,
        execution_identity: ToolExecutionIdentity,
        **_kwargs: object,
    ) -> str:
        tool = DelegateTools("leader", [target], api.runtime_paths, api.config, execution_identity)
        return await tool.delegate_task(target, "help")

    with (
        patch.object(openai_compat, "ai_response", side_effect=delegate),
        patch("mindroom.custom_tools.delegate.ai_response", new_callable=AsyncMock) as child,
    ):
        response = api.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "leader", "messages": [{"role": "user", "content": "help"}]},
        )
    assert response.status_code == 200
    assert "Cannot delegate" in response.text
    child.assert_not_called()


def test_mapped_model_visibility_and_direct_access(api: _ApiHarness) -> None:
    """The model list and direct selection enforce the same requester permissions."""
    headers = {"Authorization": "Bearer alice-key"}
    response = api.client.get("/v1/models", headers=headers)
    assert {model["id"] for model in response.json()["data"]} == {"auto", "leader", "specialist"}
    response = api.client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "forbidden", "messages": [{"role": "user", "content": "help"}]},
    )
    assert response.status_code == 403


def test_team_access_requires_every_member(api: _ApiHarness) -> None:
    """A permitted team cannot be used to reach a forbidden member."""
    api.config.teams["mixed"] = TeamConfig(
        display_name="Mixed",
        role="Test team",
        agents=["specialist", "forbidden"],
        access=ResponderAccessConfig(users=["@alice:example.org"]),
    )
    headers = {"Authorization": "Bearer alice-key"}
    response = api.client.get("/v1/models", headers=headers)
    assert "team/mixed" not in {model["id"] for model in response.json()["data"]}
    response = api.client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "team/mixed", "messages": [{"role": "user", "content": "help"}]},
    )
    assert response.status_code == 403


def test_bot_identity_cannot_be_used_as_requester(api: _ApiHarness) -> None:
    """Mapped bot identities cannot gain the internal-sender authorization bypass."""
    api.config.bot_accounts = ["@alice:example.org"]
    response = api.client.get("/v1/models", headers={"Authorization": "Bearer alice-key"})
    assert response.status_code == 403


def test_mapping_does_not_authenticate_revoked_key(api: _ApiHarness) -> None:
    """Only the configured active key list grants authentication."""
    response = api.client.get("/v1/models", headers={"Authorization": "Bearer revoked-key"})
    assert response.status_code == 401


def test_nested_delegation_retains_authority_and_denies_forbidden_target(api: _ApiHarness) -> None:
    """A specialist can delegate again but cannot acquire another requester's grants."""
    api.config.agents["specialist"].delegate_to = ["forbidden"]

    async def parent(_ctx: ResponseTurnContext, *, execution_identity: ToolExecutionIdentity, **_kwargs: object) -> str:
        tool = DelegateTools("leader", ["specialist"], api.runtime_paths, api.config, execution_identity)
        return await tool.delegate_task("specialist", "help")

    async def child(ctx: ResponseTurnContext, *, execution_identity: ToolExecutionIdentity, **_kwargs: object) -> str:
        assert ctx.requester_id == "@alice:example.org"
        assert execution_identity.agent_name == "specialist"
        tool = DelegateTools(
            "specialist",
            ["forbidden"],
            api.runtime_paths,
            api.config,
            execution_identity,
            delegation_depth=1,
        )
        return await tool.delegate_task("forbidden", "help")

    with (
        patch.object(openai_compat, "ai_response", side_effect=parent),
        patch("mindroom.custom_tools.delegate.ai_response", side_effect=child) as child_mock,
    ):
        response = api.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": "leader", "messages": [{"role": "user", "content": "help"}]},
        )
    assert response.status_code == 200
    assert "not allowed to reply" in response.text
    assert child_mock.await_count == 1


@pytest.mark.parametrize("policy", ["revoke", "room", "replace_runtime"])
def test_delegation_rechecks_current_authorization(api: _ApiHarness, policy: str, tmp_path: Path) -> None:
    """Changed policy and absent room state cannot reuse authority from request admission."""

    async def parent(_ctx: ResponseTurnContext, *, execution_identity: ToolExecutionIdentity, **_kwargs: object) -> str:
        replacement = api.config.model_copy(deep=True)
        replacement.agents["specialist"].access = ResponderAccessConfig(
            current_room_members=True,
            members_of_rooms=["lobby"] if policy == "room" else [],
        )
        snapshot = config_lifecycle.require_api_state(api.client.app).snapshot
        snapshot.runtime_config = replacement
        if policy == "replace_runtime":
            snapshot.runtime_paths = resolve_runtime_paths(config_path=tmp_path / "replaced.yaml", process_env={})
        tool = DelegateTools("leader", ["specialist"], api.runtime_paths, api.config, execution_identity)
        return await tool.delegate_task("specialist", "help")

    with (
        patch.object(openai_compat, "ai_response", side_effect=parent),
        patch("mindroom.custom_tools.delegate.ai_response", new_callable=AsyncMock) as child,
    ):
        response = api.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": "leader", "messages": [{"role": "user", "content": "help"}]},
        )
    assert response.status_code == 200
    assert "Cannot delegate" in response.text
    child.assert_not_called()


def test_delegation_rechecks_current_caller_allowlist(api: _ApiHarness) -> None:
    """Hot reload revoking a caller's delegation edge stops an active API run."""

    async def parent(_ctx: ResponseTurnContext, *, execution_identity: ToolExecutionIdentity, **_kwargs: object) -> str:
        replacement = api.config.model_copy(deep=True)
        replacement.agents["leader"].delegate_to = []
        config_lifecycle.require_api_state(api.client.app).snapshot.runtime_config = replacement
        tool = DelegateTools("leader", ["specialist"], api.runtime_paths, api.config, execution_identity)
        return await tool.delegate_task("specialist", "help")

    with (
        patch.object(openai_compat, "ai_response", side_effect=parent),
        patch("mindroom.custom_tools.delegate.ai_response", new_callable=AsyncMock) as child,
    ):
        response = api.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": "leader", "messages": [{"role": "user", "content": "help"}]},
        )
    assert response.status_code == 200
    assert "no longer an allowed target" in response.text
    child.assert_not_called()


def test_auto_route_only_receives_permitted_agents(api: _ApiHarness) -> None:
    """The routing model cannot select a target outside this caller's access."""
    with (
        patch.object(openai_compat, "suggest_responder", new=AsyncMock(return_value="specialist")) as route,
        patch.object(openai_compat, "ai_response", new=AsyncMock(return_value="done")),
    ):
        response = api.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": "auto", "messages": [{"role": "user", "content": "help"}]},
        )
    assert response.status_code == 200
    assert set(route.call_args.args[1]) == {"leader", "specialist"}


def test_rebound_key_has_a_separate_session(api: _ApiHarness) -> None:
    """Reassigning a key's canonical requester cannot open its former conversation."""
    sessions: list[str] = []

    async def parent(ctx: ResponseTurnContext, **_kwargs: object) -> str:
        sessions.append(ctx.session_id)
        return "done"

    api.config.agents["leader"].access = ResponderAccessConfig(users=["@alice:example.org", "@bob:example.org"])
    with patch.object(openai_compat, "ai_response", side_effect=parent):
        for requester in ["@alice:example.org", "@bob:example.org"]:
            with patch.object(openai_compat, "_api_key_requester", return_value=requester):
                response = api.client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer alice-key", "X-Session-ID": "same"},
                    json={"model": "leader", "messages": [{"role": "user", "content": "help"}]},
                )
            assert response.status_code == 200
    assert sessions[0] != sessions[1]


@pytest.mark.parametrize(
    "mapping",
    ["{", "[]", '{"key": null}', '{"key": 5}', '{"key": "alice"}', '{"key": "@*:example.org"}'],
)
def test_invalid_mapping_is_generic_configuration_error(tmp_path: Path, mapping: str) -> None:
    """Bad identity configuration fails closed without returning secrets."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"OPENAI_COMPAT_API_KEYS": "key", "OPENAI_COMPAT_API_KEY_REQUESTERS": mapping},
    )
    response = openai_compat._authenticate_request("Bearer key", runtime_paths)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert json.loads(response.body)["error"]["message"] == "Invalid API requester configuration"
