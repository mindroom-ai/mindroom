"""Focused tests for the shared file-memory knowledge overlay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.file_memory_knowledge import (
    resolve_agent_file_memory_knowledge,
    resolve_file_memory_knowledge,
)
from mindroom.knowledge.indexing_config import indexing_settings_key
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _identity(agent_name: str, requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def test_memory_and_agent_resolvers_share_base_and_index_identity(tmp_path: Path) -> None:
    """Memory and knowledge callers must resolve one compatible published index."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha")},
            models={},
            memory={
                "backend": "file",
                "search": {
                    "mode": "semantic",
                    "include_entrypoint": True,
                },
            },
        ),
        runtime_paths,
    )
    root = agent_workspace_root_path(runtime_paths.storage_root, "alpha")
    search_config = config.resolve_entity("alpha").memory_search

    memory_resolution = resolve_file_memory_knowledge(
        scope_user_id=agent_scope_user_id("alpha"),
        root=root,
        config=config,
        search_config=search_config,
    )
    agent_resolution = resolve_agent_file_memory_knowledge(
        "alpha",
        config,
        runtime_paths,
        execution_identity=None,
    )

    assert agent_resolution is not None
    assert memory_resolution.base_id == agent_resolution.base_id
    assert memory_resolution.root == agent_resolution.root == root.resolve()
    assert memory_resolution.config.knowledge_bases[memory_resolution.base_id].include_patterns == [
        "memory/**/*.md",
        "MEMORY.md",
    ]
    assert agent_resolution.config.knowledge_bases[agent_resolution.base_id].include_patterns == [
        "memory/**/*.md",
        "MEMORY.md",
    ]
    assert indexing_settings_key(
        memory_resolution.config,
        runtime_paths.storage_root,
        memory_resolution.base_id,
        memory_resolution.root,
    ) == indexing_settings_key(
        agent_resolution.config,
        runtime_paths.storage_root,
        agent_resolution.base_id,
        agent_resolution.root,
    )


def test_agent_resolver_returns_none_for_non_agent_before_runtime_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router resolution must not attempt to materialize agent-scoped file memory."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha")},
            models={},
            memory={"backend": "file", "search": {"mode": "semantic"}},
        ),
        runtime_paths,
    )

    def fail_runtime_resolution(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Non-agent names must return before runtime resolution")

    monkeypatch.setattr("mindroom.file_memory_knowledge.resolve_agent_runtime", fail_runtime_resolution)

    assert (
        resolve_agent_file_memory_knowledge(
            "router",
            config,
            runtime_paths,
            execution_identity=None,
        )
        is None
    )


@pytest.mark.parametrize(
    ("backend", "mode"),
    [
        ("mem0", "semantic"),
        ("file", "keyword"),
    ],
)
def test_agent_resolver_requires_file_backend_and_semantic_mode(
    tmp_path: Path,
    backend: str,
    mode: str,
) -> None:
    """Only effective semantic file-memory agents receive a runtime overlay."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha")},
            models={},
            memory={"backend": backend, "search": {"mode": mode}},
        ),
        runtime_paths,
    )

    assert (
        resolve_agent_file_memory_knowledge(
            "alpha",
            config,
            runtime_paths,
            execution_identity=None,
        )
        is None
    )


def test_private_agent_resolver_fails_closed_and_scopes_requesters(tmp_path: Path) -> None:
    """Private file-memory overlays require identity and remain requester-isolated."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(
                    display_name="Alpha",
                    private=AgentPrivateConfig(per="user", root="mind_data"),
                ),
            },
            models={},
            memory={"backend": "file", "search": {"mode": "semantic"}},
        ),
        runtime_paths,
    )

    with pytest.raises(ValueError, match="requires an active execution identity"):
        resolve_agent_file_memory_knowledge(
            "alpha",
            config,
            runtime_paths,
            execution_identity=None,
        )

    alice = resolve_agent_file_memory_knowledge(
        "alpha",
        config,
        runtime_paths,
        execution_identity=_identity("alpha", "@alice:localhost"),
    )
    bob = resolve_agent_file_memory_knowledge(
        "alpha",
        config,
        runtime_paths,
        execution_identity=_identity("alpha", "@bob:localhost"),
    )

    assert alice is not None
    assert bob is not None
    assert alice.root != bob.root
    assert alice.base_id != bob.base_id
