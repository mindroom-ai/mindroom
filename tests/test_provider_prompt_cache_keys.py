"""Provider cache groups share instructions without merging conversation identity."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.registry import build_model_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.identity_helpers import persist_entity_accounts
from tests.providers.golden_support import Wire
from tests.providers.openai_family_support import write_cli_logins

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

_SESSION_HEADERS = ("session_id", "x-client-request-id", "x-codex-window-id")


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """Record every request sent through the shared model HTTP client and reject it, so nothing else runs."""
    recorder = Wire(httpx.Response(400, json={"error": {"message": "bad", "type": "invalid_request_error"}}))
    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", recorder.client.send)
    return recorder


def _sent(wire: Wire, provider: ModelProvider) -> dict[str, Any]:
    """Return the prompt-cache key and the Codex session headers of the request ``provider`` sends."""
    with pytest.raises(ProviderError):
        asyncio.run(complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], stream=False)))
    request = wire.requests[-1]
    body = json.loads(request.content)
    headers = {name: request.headers[name] for name in _SESSION_HEADERS if name in request.headers}
    sent: dict[str, Any] = {"extra_headers": headers}
    if "prompt_cache_key" in body:
        sent["prompt_cache_key"] = body["prompt_cache_key"]
    return sent


def _cli_logins(root: Path) -> dict[str, str]:
    """Return the ``extra_kwargs`` pointing the Codex and Kimi routes at CLI logins under ``root``."""
    logins = root / "logins"
    if not logins.exists():
        logins.mkdir(parents=True)
        write_cli_logins(logins)
    return {"codex_home": str(logins / "codex"), "kimi_home": str(logins / "kimi")}


@pytest.fixture
def identity() -> ToolExecutionIdentity:
    """An authenticated agent execution with explicit tenant and account scope."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$first",
        resolved_thread_id="$first",
        session_id="!room:example.org:$first",
        tenant_id="tenant-a",
        account_id="account-a",
    )


def _request(
    wire: Wire,
    root: Path,
    provider: str,
    identity: ToolExecutionIdentity | None,
    **extra_kwargs: object,
) -> dict[str, Any]:
    config = Config(
        models={
            "default": ModelConfig(
                provider=provider,
                id="k3" if provider in {"kimi", "kimi_code"} else "gpt-6-astra",
                extra_kwargs={_home_key(provider): _cli_logins(root)[_home_key(provider)], **extra_kwargs},
            ),
        },
        agents={},
    )
    runtime_paths = resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "data",
        process_env={},
    )
    return _sent(wire, build_model_provider(config, runtime_paths, execution_identity=identity))


def _home_key(provider: str) -> str:
    return "kimi_home" if provider in {"kimi", "kimi_code"} else "codex_home"


@pytest.mark.parametrize("provider", ["codex", "openai_codex", "kimi", "kimi_code"])
def test_threads_share_cache_group(wire: Wire, tmp_path: Path, identity: ToolExecutionIdentity, provider: str) -> None:
    """Changing rooms and threads must not partition one agent's shared cache."""
    first = _request(wire, tmp_path, provider, identity)
    second = _request(
        wire,
        tmp_path,
        provider,
        replace(
            identity,
            room_id="!other:example.org",
            thread_id="$second",
            resolved_thread_id="$second",
            session_id="second",
        ),
    )

    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    if provider in {"codex", "openai_codex"}:
        assert first["extra_headers"]["session_id"] != second["extra_headers"]["session_id"]
        assert first["extra_headers"]["x-codex-window-id"] != second["extra_headers"]["x-codex-window-id"]
        assert first["extra_headers"]["session_id"] != first["prompt_cache_key"]
        assert _request(wire, tmp_path, provider, identity) == first


@pytest.mark.parametrize("provider", ["codex", "kimi"])
@pytest.mark.parametrize("field", ["agent_name", "requester_id", "tenant_id", "account_id"])
def test_cache_groups_preserve_execution_scope(
    wire: Wire,
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    provider: str,
    field: str,
) -> None:
    """Sharing threads must not combine distinct agents, users, tenants, or accounts."""
    first = _request(wire, tmp_path, provider, identity)
    second = _request(wire, tmp_path, provider, replace(identity, **{field: "different"}))

    assert first["prompt_cache_key"] != second["prompt_cache_key"]
    if provider == "codex":
        assert first["extra_headers"]["session_id"] != second["extra_headers"]["session_id"]


@pytest.mark.parametrize("provider", ["codex", "kimi"])
def test_cache_groups_preserve_storage_root_scope(
    wire: Wire,
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    provider: str,
) -> None:
    """Identical agent names under different storage roots must keep separate cache groups."""
    first = _request(wire, tmp_path / "one", provider, identity)
    second = _request(wire, tmp_path / "two", provider, identity)

    assert first["prompt_cache_key"] != second["prompt_cache_key"]


@pytest.mark.parametrize("cache_key", ["configured-cache", None])
def test_codex_cache_override_preserves_session_headers(
    wire: Wire,
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    cache_key: str | None,
) -> None:
    """Overriding or disabling the cache key must not override conversation identity."""
    original = _request(wire, tmp_path, "codex", identity)
    overridden = _request(wire, tmp_path, "codex", identity, prompt_cache_key=cache_key)

    assert overridden["extra_headers"] == original["extra_headers"]
    if cache_key is None:
        assert "prompt_cache_key" not in overridden
    else:
        assert overridden["prompt_cache_key"] == cache_key


@pytest.mark.parametrize("provider", ["codex", "kimi"])
def test_missing_execution_identity_has_no_derived_key(wire: Wire, tmp_path: Path, provider: str) -> None:
    """Model-only calls must not acquire a global fallback cache group."""
    assert "prompt_cache_key" not in _request(wire, tmp_path, provider, None)


@pytest.mark.parametrize("session_id", ["configured-session", None])
def test_codex_active_identity_owns_session_headers(
    wire: Wire,
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    session_id: str | None,
) -> None:
    """A shared model definition cannot override an active conversation's routing."""
    original = _request(wire, tmp_path, "codex", identity)
    overridden = _request(wire, tmp_path, "codex", identity, session_id=session_id)

    assert overridden["extra_headers"] == original["extra_headers"]


@pytest.mark.parametrize("provider", ["codex", "kimi"])
def test_materialized_agents_keep_distinct_cache_identity(
    wire: Wire,
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    provider: str,
) -> None:
    """Members created under one team execution retain their own model identity."""
    config = Config(
        agents={name: AgentConfig(display_name=name, include_default_tools=False) for name in ("writer", "editor")},
        models={
            "default": ModelConfig(
                provider=provider,
                id="k3" if provider == "kimi" else "gpt-6-astra",
                extra_kwargs={_home_key(provider): _cli_logins(tmp_path)[_home_key(provider)]},
            ),
        },
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    team_identity = replace(identity, agent_name="team")
    persist_entity_accounts(config, runtime_paths)
    requests = [
        _sent(wire, create_agent(name, config, runtime_paths, execution_identity=team_identity).spec.model)
        for name in config.agents
    ]

    assert requests[0]["prompt_cache_key"] != requests[1]["prompt_cache_key"]
    if provider == "codex":
        assert requests[0]["extra_headers"]["session_id"] != requests[1]["extra_headers"]["session_id"]
    assert team_identity.agent_name == "team"
