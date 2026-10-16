"""The knowledge graph runs exactly while some agent enables it."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph import runtime as runtime_module
from mindroom.knowledge_graph.runtime import KnowledgeGraphRuntime
from mindroom.knowledge_graph.store import (
    knowledge_graph_failure,
    knowledge_graph_store,
    record_knowledge_graph_failure,
)
from tests.test_knowledge_graph_ingestion import _FakeStore, _seed

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import EpisodeInput
    from mindroom.knowledge_graph.usage import UsageTarget


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


class _HangingStore(_FakeStore):
    """Starts extracting and never finishes, like a model that does not answer."""

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        del scope, episode, usage
        self.added.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_stop_cancels_a_pass_in_progress_and_clears_health(tmp_path: Path) -> None:
    """Shutting down or turning the graph off does not wait for slow extraction, and leaves no stale failure."""
    config = _config(enabled=True)
    config.knowledge_graph.ingest.idle_seconds = 1
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    _seed(config, paths, "!room:x:$t1", ["r1"], at=int(time.time()) - 60)
    store = _HangingStore()
    runtime = KnowledgeGraphRuntime(runtime_paths=paths, config_provider=lambda: config)
    with patch.object(runtime_module, "GraphitiStore", return_value=store):
        await runtime.sync()
    await asyncio.wait_for(store.added.wait(), timeout=10)
    record_knowledge_graph_failure("ValueError: earlier failure")

    await asyncio.wait_for(runtime.stop(), timeout=5)

    assert knowledge_graph_failure() is None
