"""Each agent and private instance gets its own graph, named in code and never after a requester."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import mindroom.knowledge_graph.scope as scope_module
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.embeddings.signatures import effective_mem0_embedder_signature
from mindroom.knowledge_graph.scope import _graph_name, graph_scope, graph_scopes
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.scope import GraphScope

ALICE = "@alice:example.test"
BOB = "@bob:example.test"


def _config(**embedder: object) -> Config:
    return Config(
        agents={
            "mind": AgentConfig(display_name="Mind", knowledge_graph=True),
            "pal": AgentConfig(display_name="Pal", knowledge_graph=True, private=AgentPrivateConfig(per="user")),
            "coder": AgentConfig(display_name="Coder"),
        },
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
        memory={"embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small", **embedder}}},
    )


def _requester(scope: GraphScope) -> str | None:
    return scope.execution_identity.requester_id if scope.execution_identity is not None else None


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)


def _create_private_instance(config: Config, paths: RuntimePaths, requester: str) -> None:
    identity = build_requester_execution_identity(agent_name="pal", runtime_paths=paths, requester_id=requester)
    resolve_agent_runtime("pal", config, paths, identity, create=True)


def test_graph_name_is_stable_and_hides_the_scope() -> None:
    """The same scope and embedder always name the same graph, and the name reveals neither."""
    signature = ("openai", "text-embedding-3-small", "", "1536")
    name = _graph_name("private_instances/abc/pal", signature)
    assert name == _graph_name("private_instances/abc/pal", signature)
    assert re.fullmatch(r"kg_[0-9a-f]{24}", name)
    assert "pal" not in name


def test_embedder_change_changes_the_graph_name(tmp_path: Path) -> None:
    """Vectors from another embedder are incompatible, so the graph changes with it."""
    paths = _paths(tmp_path)
    before = graph_scope("mind", _config(), paths, None).graph_name
    after = graph_scope("mind", _config(dimensions=256), paths, None).graph_name
    assert before != after


def test_local_embedder_graph_is_separate_from_graphs_without_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph vectors from a local model now carry its prompts, so a graph built without them is not reused."""
    paths = _paths(tmp_path)
    local = Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
        memory={"embedder": {"provider": "sentence_transformers", "config": {"model": "google/embeddinggemma-2"}}},
    )
    # The name the graph had while local vectors were embedded without prompts, under the plain model like Mem0's.
    monkeypatch.setattr(scope_module, "effective_knowledge_embedder_signature", effective_mem0_embedder_signature)
    unprompted = graph_scope("mind", local, paths, None).graph_name
    monkeypatch.undo()

    assert graph_scope("mind", local, paths, None).graph_name != unprompted


def test_private_instances_get_distinct_graphs(tmp_path: Path) -> None:
    """Two requesters of one private agent never share a graph, and a shared agent has its own."""
    config = _config()
    paths = _paths(tmp_path)
    for requester in (ALICE, BOB):
        _create_private_instance(config, paths, requester)

    scopes = graph_scopes(config, paths)

    by_owner = {(scope.agent_name, _requester(scope)): scope.graph_name for scope in scopes}
    assert set(by_owner) == {("mind", None), ("pal", ALICE), ("pal", BOB)}
    assert len(set(by_owner.values())) == 3
    # The tool and the ingestion worker resolve the same graph for one requester.
    for requester in (ALICE, BOB):
        identity = build_requester_execution_identity(agent_name="pal", runtime_paths=paths, requester_id=requester)
        assert graph_scope("pal", config, paths, identity).graph_name == by_owner[("pal", requester)]
