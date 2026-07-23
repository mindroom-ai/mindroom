"""Knowledge-base access and multi-knowledge vector DB behavior for AgentBot."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge

from mindroom.bot import AgentBot
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.utils import _MultiKnowledgeVectorDb, knowledge_runtime_identity
from mindroom.knowledge_source_descriptions import KnowledgeWithSourceDescriptions
from tests.bot_helpers import (
    AgentBotTestBase,
    _AsyncStubVectorDb,
    _FailingStubVectorDb,
    _fake_indexing_settings,
    _SyncStubVectorDb,
    make_mock_agent_user,
)
from tests.conftest import (
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.users import AgentMatrixUser


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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        assert bot._knowledge_access_support.for_agent("calculator") is None

    def test_knowledge_for_agent_uses_assigned_base_manager(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agents should receive knowledge from their assigned knowledge base manager."""
        config = self.create_config_with_knowledge_bases(
            assigned_bases=["research"],
            knowledge_bases={
                "research": KnowledgeBaseConfig(path=str(tmp_path / "kb"), watch=False),
            },
            runtime_root=tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        expected_knowledge = Knowledge()
        lookup = SimpleNamespace(
            key=SimpleNamespace(
                base_id="research",
                storage_root=str(tmp_path),
                knowledge_path=str(tmp_path / "kb"),
                indexing_settings=_fake_indexing_settings("research"),
            ),
            index=SimpleNamespace(
                knowledge=expected_knowledge,
                state=SimpleNamespace(
                    source_signature=hashlib.sha256().hexdigest(),
                    last_published_at="2999-01-01T00:00:00+00:00",
                ),
            ),
            availability=KnowledgeAvailability.READY,
            state=None,
        )

        with patch("mindroom.knowledge.utils.get_published_index", return_value=lookup):
            assert bot._knowledge_access_support.for_agent("calculator") is expected_knowledge

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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        research_vector_db = MagicMock()
        research_vector_db.search.return_value = [
            Document(content="research content 1"),
            Document(content="research content 2"),
            Document(content="research content 3"),
        ]
        research_knowledge = Knowledge(vector_db=research_vector_db)

        legal_vector_db = MagicMock()
        legal_vector_db.search.return_value = [
            Document(content="legal content 1"),
            Document(content="legal content 2"),
            Document(content="legal content 3"),
        ]
        legal_knowledge = Knowledge(vector_db=legal_vector_db)

        def _lookup(base_id: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                key=SimpleNamespace(
                    base_id=base_id,
                    storage_root=str(tmp_path),
                    knowledge_path=str(tmp_path / f"kb_{base_id}"),
                    indexing_settings=_fake_indexing_settings(base_id),
                ),
                index=SimpleNamespace(
                    knowledge={"research": research_knowledge, "legal": legal_knowledge}[base_id],
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
        research_vector_db.search.assert_called_once_with(query="knowledge query", limit=4, filters=None)
        legal_vector_db.search.assert_called_once_with(query="knowledge query", limit=4, filters=None)

    def test_multi_knowledge_vector_db_interleaves_sync_results(self) -> None:
        """Round-robin merge should include top results from each knowledge base."""
        vector_db = _MultiKnowledgeVectorDb(
            vector_dbs=[
                _SyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                        Document(content="research 3"),
                    ],
                ),
                _SyncStubVectorDb(
                    documents=[
                        Document(content="legal 1"),
                        Document(content="legal 2"),
                        Document(content="legal 3"),
                    ],
                ),
            ],
        )

        docs = vector_db.search(query="knowledge query", limit=4)
        assert [doc.content for doc in docs] == ["research 1", "legal 1", "research 2", "legal 2"]

    def test_multi_knowledge_runtime_identity_tracks_underlying_handles(self) -> None:
        """Fresh merged views over unchanged indexes keep the same runtime identity."""
        sources = [_SyncStubVectorDb(documents=[]), _SyncStubVectorDb(documents=[])]
        first = KnowledgeWithSourceDescriptions(
            name="merged",
            vector_db=_MultiKnowledgeVectorDb(vector_dbs=sources),
        )
        second = KnowledgeWithSourceDescriptions(
            name="merged",
            vector_db=_MultiKnowledgeVectorDb(vector_dbs=sources),
        )
        changed = KnowledgeWithSourceDescriptions(
            name="merged",
            vector_db=_MultiKnowledgeVectorDb(vector_dbs=[sources[0], _SyncStubVectorDb(documents=[])]),
        )

        assert knowledge_runtime_identity(first) == knowledge_runtime_identity(second)
        assert knowledge_runtime_identity(first) != knowledge_runtime_identity(changed)

    def test_multi_knowledge_vector_db_sync_ignores_failing_source(self) -> None:
        """A failing knowledge source should not suppress healthy source results."""
        vector_db = _MultiKnowledgeVectorDb(
            vector_dbs=[
                _SyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                    ],
                ),
                _FailingStubVectorDb(error_message="boom"),
            ],
        )

        docs = vector_db.search(query="knowledge query", limit=3)
        assert [doc.content for doc in docs] == ["research 1", "research 2"]

    @pytest.mark.asyncio
    async def test_multi_knowledge_vector_db_interleaves_async_results(self) -> None:
        """Async merge should interleave and support sync-only vector DBs."""
        vector_db = _MultiKnowledgeVectorDb(
            vector_dbs=[
                _AsyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                        Document(content="research 3"),
                    ],
                ),
                _SyncStubVectorDb(
                    documents=[
                        Document(content="legal 1"),
                        Document(content="legal 2"),
                        Document(content="legal 3"),
                    ],
                ),
            ],
        )

        docs = await vector_db.async_search(query="knowledge query", limit=5)
        assert [doc.content for doc in docs] == [
            "research 1",
            "legal 1",
            "research 2",
            "legal 2",
            "research 3",
        ]

    @pytest.mark.asyncio
    async def test_multi_knowledge_vector_db_async_ignores_failing_source(self) -> None:
        """Async search should continue returning healthy source results on failures."""
        vector_db = _MultiKnowledgeVectorDb(
            vector_dbs=[
                _AsyncStubVectorDb(
                    documents=[
                        Document(content="research 1"),
                        Document(content="research 2"),
                    ],
                ),
                _FailingStubVectorDb(error_message="boom"),
            ],
        )

        docs = await vector_db.async_search(query="knowledge query", limit=3)
        assert [doc.content for doc in docs] == ["research 1", "research 2"]

    def test_multi_knowledge_vector_db_sync_raises_when_all_sources_fail(self) -> None:
        """When every knowledge source fails, the first error surfaces instead of empty results."""
        vector_db = _MultiKnowledgeVectorDb(
            vector_dbs=[
                _FailingStubVectorDb(error_message="first boom"),
                _FailingStubVectorDb(error_message="second boom"),
            ],
        )

        with pytest.raises(RuntimeError, match="first boom"):
            vector_db.search(query="knowledge query", limit=3)

    @pytest.mark.asyncio
    async def test_multi_knowledge_vector_db_async_raises_when_all_sources_fail(self) -> None:
        """Async search re-raises the first captured failure when every source fails."""
        vector_db = _MultiKnowledgeVectorDb(
            vector_dbs=[
                _FailingStubVectorDb(error_message="first boom"),
                _FailingStubVectorDb(error_message="second boom"),
            ],
        )

        with pytest.raises(RuntimeError, match="first boom"):
            await vector_db.async_search(query="knowledge query", limit=3)

    def test_strict_knowledge_handle_propagates_search_failure(self) -> None:
        """The Knowledge handle agents query raises instead of agno's swallow-to-[].

        agno's ``Knowledge.search`` catches every vector-db exception and
        returns an empty list, so the agent-facing ``search_knowledge_base``
        tool would report "No documents found" during an embedder outage.
        MindRoom read handles must keep the failure loud through the same
        ``retrieve -> search`` chain the generated tool uses.
        """
        knowledge = KnowledgeWithSourceDescriptions(
            name="merged",
            vector_db=_MultiKnowledgeVectorDb(vector_dbs=[_FailingStubVectorDb(error_message="first boom")]),
        )

        with pytest.raises(RuntimeError, match="first boom"):
            knowledge.search(query="knowledge query")
        with pytest.raises(RuntimeError, match="first boom"):
            knowledge.retrieve(query="knowledge query")

    @pytest.mark.asyncio
    async def test_strict_knowledge_handle_propagates_async_search_failure(self) -> None:
        """The async search path agents use raises instead of returning []."""
        knowledge = KnowledgeWithSourceDescriptions(
            name="merged",
            vector_db=_MultiKnowledgeVectorDb(vector_dbs=[_FailingStubVectorDb(error_message="first boom")]),
        )

        with pytest.raises(RuntimeError, match="first boom"):
            await knowledge.asearch(query="knowledge query")
