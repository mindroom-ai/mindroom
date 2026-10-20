"""Derived state of agents: knowledge indexes, Git directories, private-instance roots and files of older versions."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.constants import tracking_dir
from mindroom.knowledge.file_memory_knowledge import resolve_agent_file_memory_knowledge, resolve_file_memory_knowledge
from mindroom.knowledge.indexing_config import knowledge_git_dir
from mindroom.knowledge.registry import published_index_storage_path, resolve_published_index_key
from mindroom.legacy_handled_turns import legacy_responses_file_path
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.runtime_resolution import resolve_agent_runtime, shared_knowledge_path
from mindroom.tool_system.worker_routing import agent_state_root_path, build_requester_execution_identity

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


OLD = "code"

# Its name extends the renamed agent's, so a substring match would wrongly touch it.
PREFIX = "code_team"
HELPER = "helper"
REQUESTER = "@alice:localhost"


def index_config(**agent_fields: object) -> Config:
    """Return a config with the renamed agent, the agent whose name extends it, and a helper."""
    return Config(
        agents={name: AgentConfig(display_name=name.title(), **agent_fields) for name in (OLD, PREFIX, HELPER)},
        models={"default": {"provider": "openai", "id": "gpt-5"}},
        knowledge_graph={"model": "default"},
    )


def make_store_dir(path: Path) -> Path:
    """Create ``path`` with one file in it, like a store that holds data."""
    path.mkdir(parents=True)
    (path / "data").write_bytes(b"data")
    return path


def touch_file(path: Path) -> Path:
    """Create ``path`` holding a few bytes, with its parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return path


def file_memory_index(agent: str, config: Config, runtime_paths: RuntimePaths, *, root: Path | None = None) -> Path:
    """Create the store of an agent's file-memory search index where MindRoom keeps it, for its root or ``root``."""
    if root is None:
        resolution = resolve_agent_file_memory_knowledge(agent, config, runtime_paths, None)
        assert resolution is not None
    else:
        resolution = resolve_file_memory_knowledge(
            scope_user_id=agent_scope_user_id(agent),
            root=root,
            config=config,
            search_config=config.resolve_entity(agent).memory_search,
        )
    key = resolve_published_index_key(resolution.base_id, config=resolution.config, runtime_paths=runtime_paths)
    return make_store_dir(published_index_storage_path(key))


def private_config() -> Config:
    """Return a config in which the renamed agent and the agent whose name extends it are private."""
    private = AgentPrivateConfig(per="user")
    return Config(
        agents={
            OLD: AgentConfig(display_name="Code", private=private),
            PREFIX: AgentConfig(display_name="Code Team", private=private),
            HELPER: AgentConfig(display_name="Helper"),
        },
    )


def private_state_root(agent: str, config: Config, runtime_paths: RuntimePaths) -> Path:
    """Create the state root MindRoom keeps for one requester's instance of a private agent."""
    identity = build_requester_execution_identity(agent_name=agent, runtime_paths=runtime_paths, requester_id=REQUESTER)
    state_root = resolve_agent_runtime(agent, config, runtime_paths, identity, create=True).state_root
    assert state_root.is_dir()
    return state_root


def git_config(bases: dict[str, str]) -> Config:
    """Return a config with Git-backed knowledge bases, each checked out in the workspace of the agent it maps to."""
    config = index_config()
    knowledge_bases = {
        base_id: KnowledgeBaseConfig(
            path=f"${{MINDROOM_STORAGE_PATH}}/agents/{agent}/workspace/docs",
            git=KnowledgeGitConfig(repo_url=f"https://example.org/{base_id}.git"),
        )
        for base_id, agent in bases.items()
    }
    return config.model_copy(update={"knowledge_bases": knowledge_bases})


def knowledge_git(base_id: str, config: Config, runtime_paths: RuntimePaths) -> Path:
    """Create the Git directory MindRoom keeps for a knowledge base's checkout."""
    checkout = shared_knowledge_path(config.knowledge_bases[base_id].path, runtime_paths)
    return make_store_dir(knowledge_git_dir(runtime_paths.storage_root, checkout))


def legacy_files(runtime_paths: RuntimePaths, agent: str) -> list[Path]:
    """Create the files older MindRoom versions kept for an agent, which no code reads any more."""
    storage = runtime_paths.storage_root
    principal = hashlib.sha256(f"@mindroom_{agent}:localhost".encode()).hexdigest()[:12]
    # The memory search index of file memory before #1165: memory_search_db/<scope id>_<16 hex digits>.
    scope = agent_scope_user_id(agent)
    search_digest = hashlib.sha256(f"{scope}:{agent_state_root_path(storage, agent)}".encode()).hexdigest()[:16]
    responded = legacy_responses_file_path(storage, agent)
    return [
        # The dispatch obligations before the event journal (#1800): dispatch_obligations-<entity>-<12 hex>.sqlite3.
        touch_file(tracking_dir(runtime_paths) / f"dispatch_obligations-{agent}-{principal}.sqlite3"),
        touch_file(tracking_dir(runtime_paths) / f"dispatch_obligations-{agent}-{principal}.sqlite3-wal"),
        touch_file(storage / "sessions" / f"{agent}.db"),
        touch_file(storage / f"{agent}.db"),
        make_store_dir(storage / "memory_search_db" / f"{scope}_{search_digest}"),
        touch_file(responded.with_suffix(f"{responded.suffix}.imported")),
        # An older ledger reader set an unreadable ledger aside like this.
        touch_file(responded.with_name(f"{responded.name}.corrupt-20260801T000000Z")),
    ]
