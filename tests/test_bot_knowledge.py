"""Knowledge-base access and merged knowledge search behavior for AgentBot."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge.strict_knowledge import StrictSearchKnowledge
from mindroom.knowledge.utils import knowledge_runtime_identity
from tests.bot_helpers import (
    AgentBotTestBase,
    _FailingStubIndex,
    _fake_indexing_settings,
    _StubIndex,
    make_mock_agent_user,
    make_test_agent_bot,
)
from tests.conftest import (
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.users import AgentMatrixUser


def _hits(*contents: str) -> list[SearchHit]:
    return [SearchHit(content=content) for content in contents]


def _knowledge(*indexes: _StubIndex | _FailingStubIndex) -> AgentKnowledge:
    return AgentKnowledge(
        sources=tuple(
            KnowledgeSource(description=KnowledgeSourceDescription(base_id=f"base{index}", description=""), index=stub)
            for index, stub in enumerate(indexes)
        ),
    )


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    def test_knowledge_for_agent_returns_none_when_unassigned(
        self,
        mock_agent_user: AgentMatrixUser,
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
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        assert bot._knowledge_access_support.for_agent("calculator") is None

    def test_knowledge_for_agent_uses_assigned_base_manager(
        self,
        mock_agent_user: AgentMatrixUser,
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
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        expected_reader = _StubIndex(hits=[])
        lookup = SimpleNamespace(
            key=SimpleNamespace(
                base_id="research",
                storage_root=str(tmp_path),
                knowledge_path=str(tmp_path / "kb"),
                indexing_settings=_fake_indexing_settings("research"),
            ),
            index=SimpleNamespace(
                reader=expected_reader,
                state=SimpleNamespace(
                    source_signature=hashlib.sha256().hexdigest(),
                    last_published_at="2999-01-01T00:00:00+00:00",
                ),
            ),
            availability=KnowledgeAvailability.READY,
            state=None,
        )

        with patch("mindroom.knowledge.utils.get_published_index", return_value=lookup):
            knowledge = bot._knowledge_access_support.for_agent("calculator")

        assert isinstance(knowledge, StrictSearchKnowledge)
        [source] = knowledge.agent_knowledge.sources
        assert source.index is expected_reader
        assert source.description == KnowledgeSourceDescription(base_id="research", description="Lab notes.")

    def test_knowledge_for_agent_merges_multiple_assigned_bases(
        self,
        mock_agent_user: AgentMatrixUser,
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
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        readers = {
            "research": _StubIndex(hits=_hits("research content 1", "research content 2", "research content 3")),
            "legal": _StubIndex(hits=_hits("legal content 1", "legal content 2", "legal content 3")),
        }

        def _lookup(base_id: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                key=SimpleNamespace(
                    base_id=base_id,
                    storage_root=str(tmp_path),
                    knowledge_path=str(tmp_path / f"kb_{base_id}"),
                    indexing_settings=_fake_indexing_settings(base_id),
                ),
                index=SimpleNamespace(
                    reader=readers[base_id],
                    state=SimpleNamespace(
                        source_signature=hashlib.sha256().hexdigest(),
                        last_published_at="2999-01-01T00:00:00+00:00",
                    ),
                ),
                availability=KnowledgeAvailability.READY,
                state=None,
            )

        with patch("mindroom.knowledge.utils.get_published_index", side_effect=_lookup):
            combined_knowledge = bot._knowledge_access_support.for_agent("calculator")
        assert combined_knowledge is not None

        docs = combined_knowledge.search("knowledge query", max_results=4)
        assert [doc.content for doc in docs] == [
            "research content 1",
            "legal content 1",
            "research content 2",
            "legal content 2",
        ]
        assert readers["research"].calls == [("knowledge query", 4)]
        assert readers["legal"].calls == [("knowledge query", 4)]

    def test_merged_knowledge_interleaves_sync_results(self) -> None:
        """Round-robin merge should include top results from each knowledge base."""
        knowledge = _knowledge(
            _StubIndex(hits=_hits("research 1", "research 2", "research 3")),
            _StubIndex(hits=_hits("legal 1", "legal 2", "legal 3")),
        )

        hits = knowledge.search("knowledge query", 4)
        assert [hit.content for hit in hits] == ["research 1", "legal 1", "research 2", "legal 2"]

    def test_merged_knowledge_runtime_identity_tracks_underlying_handles(self) -> None:
        """Fresh merged views over unchanged indexes keep the same runtime identity."""
        indexes = [_StubIndex(hits=[]), _StubIndex(hits=[])]
        first = StrictSearchKnowledge(_knowledge(*indexes))
        second = StrictSearchKnowledge(_knowledge(*indexes))
        changed = StrictSearchKnowledge(_knowledge(indexes[0], _StubIndex(hits=[])))

        assert knowledge_runtime_identity(first) == knowledge_runtime_identity(second)
        assert knowledge_runtime_identity(first) != knowledge_runtime_identity(changed)

    def test_merged_knowledge_sync_ignores_failing_source(self) -> None:
        """A failing knowledge source should not suppress healthy source results."""
        knowledge = _knowledge(_StubIndex(hits=_hits("research 1", "research 2")), _FailingStubIndex("boom"))

        assert [hit.content for hit in knowledge.search("knowledge query", 3)] == ["research 1", "research 2"]

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

    def test_merged_knowledge_sync_raises_when_all_sources_fail(self) -> None:
        """When every knowledge source fails, the first error surfaces instead of empty results."""
        knowledge = _knowledge(_FailingStubIndex("first boom"), _FailingStubIndex("second boom"))

        with pytest.raises(RuntimeError, match="first boom"):
            knowledge.search("knowledge query", 3)

    @pytest.mark.asyncio
    async def test_merged_knowledge_async_raises_when_all_sources_fail(self) -> None:
        """Async search re-raises the first captured failure when every source fails."""
        knowledge = _knowledge(_FailingStubIndex("first boom"), _FailingStubIndex("second boom"))

        with pytest.raises(RuntimeError, match="first boom"):
            await knowledge.asearch("knowledge query", 3)

    def test_strict_knowledge_handle_propagates_search_failure(self) -> None:
        """The Knowledge handle agents query raises instead of agno's swallow-to-[].

        agno's ``Knowledge.search`` catches every vector-db exception and
        returns an empty list, so the agent-facing ``search_knowledge_base``
        tool would report "No documents found" during an embedder outage.
        MindRoom read handles must keep the failure loud through the same
        ``retrieve -> search`` chain the generated tool uses.
        """
        knowledge = StrictSearchKnowledge(_knowledge(_FailingStubIndex("first boom")))

        with pytest.raises(RuntimeError, match="first boom"):
            knowledge.search(query="knowledge query")
        with pytest.raises(RuntimeError, match="first boom"):
            knowledge.retrieve(query="knowledge query")

    @pytest.mark.asyncio
    async def test_strict_knowledge_handle_propagates_async_search_failure(self) -> None:
        """The async search path agents use raises instead of returning []."""
        knowledge = StrictSearchKnowledge(_knowledge(_FailingStubIndex("first boom")))

        with pytest.raises(RuntimeError, match="first boom"):
            await knowledge.asearch(query="knowledge query")
