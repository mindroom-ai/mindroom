"""The knowledge graph runs exactly while some agent enables it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph.runtime import KnowledgeGraphRuntime
from mindroom.knowledge_graph.store import knowledge_graph_store

if TYPE_CHECKING:
    from pathlib import Path


def _config(*, enabled: bool) -> Config:
    return Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=enabled)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
    )


@pytest.mark.asyncio
async def test_runtime_runs_while_an_agent_enables_the_graph(tmp_path: Path) -> None:
    """Enabling starts the store and worker once; disabling stops both and unregisters the store."""
    config = _config(enabled=True)
    runtime = KnowledgeGraphRuntime(
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
        config_provider=lambda: config,
    )
    try:
        await runtime.sync()
        store = knowledge_graph_store()
        assert store is not None
        await runtime.sync()
        assert knowledge_graph_store() is store

        config = _config(enabled=False)
        await runtime.sync()
        assert knowledge_graph_store() is None
    finally:
        await runtime.stop()
