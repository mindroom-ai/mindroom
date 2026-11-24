"""Knowledge-base access and merged knowledge search behavior for agents."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge.utils import knowledge_runtime_identity, resolve_agent_knowledge_access_async
from tests.bot_helpers import (
    AgentBotTestBase,
    _FailingStubIndex,
    _StubIndex,
)
from tests.conftest import (
    runtime_paths_for,
)
from tests.fakes.knowledge import published_index_lookup

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.knowledge.registry import PublishedIndexResolution


def _hits(*contents: str) -> list[SearchHit]:
    return [SearchHit(content=content) for content in contents]


def _knowledge(*indexes: _StubIndex | _FailingStubIndex) -> AgentKnowledge:
    return AgentKnowledge(
        sources=tuple(
            KnowledgeSource(description=KnowledgeSourceDescription(base_id=f"base{index}", description=""), index=stub)
            for index, stub in enumerate(indexes)
        ),
    )


class TestAgentBot(AgentBotTestBase):
    """Agent knowledge resolution and merged knowledge search tests."""

    @pytest.mark.asyncio
    async def test_knowledge_for_agent_returns_none_when_unassigned(
        self,
        tmp_path: Path,
    ) -> None:
        """Unassigned agents should not receive knowledge access."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=[],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb"), watch=False),
            },
            runtime_root=tmp_path,
        )

        resolution = await resolve_agent_knowledge_access_async("calculator", config, runtime_paths_for(config))

        assert resolution.knowledge is None

    @pytest.mark.asyncio
    async def test_knowledge_for_agent_uses_assigned_base_manager(
        self,
        tmp_path: Path,
    ) -> None:
        """Agents should search their assigned base's published index under that base's description."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=["research"],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb"), watch=False, description="Lab notes."),
            },
            runtime_root=tmp_path,
        )
        expected_reader = _StubIndex(hits=[])
        lookup = published_index_lookup(
            "research",
            expected_reader,
            storage_root=str(tmp_path),
            knowledge_path=str(tmp_path / "kb"),
        )

        with patch("mindroom.knowledge.utils.get_published_index", return_value=lookup):
            resolution = await resolve_agent_knowledge_access_async("calculator", config, runtime_paths_for(config))
        knowledge = resolution.knowledge

        assert isinstance(knowledge, AgentKnowledge)
        [source] = knowledge.sources
        assert source.index is expected_reader
        assert source.description == KnowledgeSourceDescription(base_id="research", description="Lab notes.")

    @pytest.mark.asyncio
    async def test_knowledge_for_agent_merges_multiple_assigned_bases(
        self,
        tmp_path: Path,
    ) -> None:
        """Agents assigned to multiple bases should search across all assigned bases."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=["research", "legal"],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb_research"), watch=False),
                "legal": KnowledgeBaseConfig(path=str(tmp_path / "kb_legal"), watch=False),
            },
            runtime_root=tmp_path,
        )
        readers = {
            "research": _StubIndex(hits=_hits("research content 1", "research content 2", "research content 3")),
            "legal": _StubIndex(hits=_hits("legal content 1", "legal content 2", "legal content 3")),
        }

        def _lookup(base_id: str, **_kwargs: object) -> PublishedIndexResolution:
            return published_index_lookup(
                base_id,
                readers[base_id],
                storage_root=str(tmp_path),
                knowledge_path=str(tmp_path / f"kb_{base_id}"),
            )

        with patch("mindroom.knowledge.utils.get_published_index", side_effect=_lookup):
            resolution = await resolve_agent_knowledge_access_async("calculator", config, runtime_paths_for(config))
        combined_knowledge = resolution.knowledge
        assert combined_knowledge is not None

        hits = await combined_knowledge.asearch("knowledge query", 4)
        assert [hit.content for hit in hits] == [
            "research content 1",
            "legal content 1",
            "research content 2",
            "legal content 2",
        ]
        assert readers["research"].calls == [("knowledge query", 4)]
        assert readers["legal"].calls == [("knowledge query", 4)]

    def test_merged_knowledge_runtime_identity_tracks_underlying_handles(self) -> None:
        """Fresh merged views over unchanged indexes keep the same runtime identity."""
        indexes = [_StubIndex(hits=[]), _StubIndex(hits=[])]
        first = _knowledge(*indexes)
        second = _knowledge(*indexes)
        changed = _knowledge(indexes[0], _StubIndex(hits=[]))

        assert knowledge_runtime_identity(first) == knowledge_runtime_identity(second)
        assert knowledge_runtime_identity(first) != knowledge_runtime_identity(changed)

    @pytest.mark.asyncio
    async def test_merged_knowledge_interleaves_async_results(self) -> None:
        """Async merge should interleave every source's results."""
        knowledge = _knowledge(
            _StubIndex(hits=_hits("research 1", "research 2", "research 3")),
            _StubIndex(hits=_hits("legal 1", "legal 2", "legal 3")),
        )

        hits = await knowledge.asearch("knowledge query", 5)
        assert [hit.content for hit in hits] == ["research 1", "legal 1", "research 2", "legal 2", "research 3"]

    @pytest.mark.asyncio
    async def test_merged_knowledge_async_ignores_failing_source(self) -> None:
        """Async search should continue returning healthy source results on failures."""
        knowledge = _knowledge(_StubIndex(hits=_hits("research 1", "research 2")), _FailingStubIndex("boom"))

        hits = await knowledge.asearch("knowledge query", 3)
        assert [hit.content for hit in hits] == ["research 1", "research 2"]

    @pytest.mark.asyncio
    async def test_merged_knowledge_async_raises_when_all_sources_fail(self) -> None:
        """Async search re-raises the first captured failure when every source fails."""
        knowledge = _knowledge(_FailingStubIndex("first boom"), _FailingStubIndex("second boom"))

        with pytest.raises(RuntimeError, match="first boom"):
            await knowledge.asearch("knowledge query", 3)
