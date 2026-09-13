"""Provider cache groups share instructions without merging conversation identity."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    from pathlib import Path


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
                extra_kwargs=extra_kwargs,
            ),
        },
        agents={},
    )
    runtime_paths = resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "data",
        process_env={},
    )
    return get_model_instance(config, runtime_paths, execution_identity=identity).get_request_params()


@pytest.mark.parametrize("provider", ["codex", "openai_codex", "kimi", "kimi_code"])
def test_threads_share_cache_group(tmp_path: Path, identity: ToolExecutionIdentity, provider: str) -> None:
    """Changing rooms and threads must not partition one agent's shared cache."""
    first = _request(tmp_path, provider, identity)
    second = _request(
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
        assert _request(tmp_path, provider, identity) == first


@pytest.mark.parametrize("provider", ["codex", "kimi"])
@pytest.mark.parametrize("field", ["agent_name", "requester_id", "tenant_id", "account_id"])
def test_cache_groups_preserve_execution_scope(
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    provider: str,
    field: str,
) -> None:
    """Sharing threads must not combine distinct agents, users, tenants, or accounts."""
    first = _request(tmp_path, provider, identity)
    second = _request(tmp_path, provider, replace(identity, **{field: "different"}))

    assert first["prompt_cache_key"] != second["prompt_cache_key"]
    if provider == "codex":
        assert first["extra_headers"]["session_id"] != second["extra_headers"]["session_id"]


@pytest.mark.parametrize("provider", ["codex", "kimi"])
def test_cache_groups_preserve_installation_scope(
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    provider: str,
) -> None:
    """Identical agent names in separate installations must keep separate cache groups."""
    first = _request(tmp_path / "one", provider, identity)
    second = _request(tmp_path / "two", provider, identity)

    assert first["prompt_cache_key"] != second["prompt_cache_key"]


@pytest.mark.parametrize("cache_key", ["configured-cache", None])
def test_codex_cache_override_preserves_session_headers(
    tmp_path: Path,
    identity: ToolExecutionIdentity,
    cache_key: str | None,
) -> None:
    """Overriding or disabling the cache key must not override conversation identity."""
    original = _request(tmp_path, "codex", identity)
    overridden = _request(tmp_path, "codex", identity, prompt_cache_key=cache_key)

    assert overridden["extra_headers"] == original["extra_headers"]
    if cache_key is None:
        assert "prompt_cache_key" not in overridden
    else:
        assert overridden["prompt_cache_key"] == cache_key


@pytest.mark.parametrize("provider", ["codex", "kimi"])
def test_missing_execution_identity_has_no_derived_key(tmp_path: Path, provider: str) -> None:
    """Model-only calls must not acquire a global fallback cache group."""
    assert "prompt_cache_key" not in _request(tmp_path, provider, None)
