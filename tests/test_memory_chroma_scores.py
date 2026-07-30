"""Tests for the Chroma distance-to-similarity fix behind mem0 memory search."""

from __future__ import annotations

import chromadb
import pytest
from mem0.utils.scoring import score_and_rank
from mem0.vector_stores.chroma import ChromaDB

from mindroom.memory.config import _chroma_similarity_from_distance, _install_chroma_similarity_scores

_SCOPE_FILTER = {"user_id": "agent_general"}
_MEM0_DEFAULT_THRESHOLD = 0.1


def _chroma_store() -> ChromaDB:
    store = ChromaDB(collection_name="memories", client=chromadb.EphemeralClient())
    store.insert(
        vectors=[[1.0, 0.0, 0.0], [0.8, 0.6, 0.0], [0.0, 0.0, 1.0]],
        payloads=[
            {"data": "IonQ MindRoom COI lawyer review", "user_id": "agent_general"},
            {"data": "IonQ hardware notes", "user_id": "agent_general"},
            {"data": "unrelated grocery list", "user_id": "agent_general"},
        ],
        ids=["verbatim", "related", "unrelated"],
    )
    return store


def _rank_with_mem0(store: ChromaDB) -> list[dict[str, object]]:
    """Run mem0's real semantic scoring gate over one verbatim-overlap query."""
    hits = store.search(
        query="IonQ COI lawyer review",
        vectors=[1.0, 0.0, 0.0],
        top_k=3,
        filters=_SCOPE_FILTER,
    )
    candidates = [{"id": hit.id, "score": hit.score, "payload": hit.payload} for hit in hits]
    return score_and_rank(
        semantic_results=candidates,
        bm25_scores={},
        entity_boosts={},
        threshold=_MEM0_DEFAULT_THRESHOLD,
        top_k=3,
    )


def test_similarity_conversion_per_space() -> None:
    """Each Chroma distance space converts to a monotone similarity."""
    assert _chroma_similarity_from_distance(None, "l2") is None
    assert _chroma_similarity_from_distance(0.0, "l2") == pytest.approx(1.0)
    assert _chroma_similarity_from_distance(0.4, "l2") == pytest.approx(0.8)
    assert _chroma_similarity_from_distance(2.0, "l2") == pytest.approx(0.0)
    assert _chroma_similarity_from_distance(0.0, "cosine") == pytest.approx(1.0)
    assert _chroma_similarity_from_distance(0.25, "cosine") == pytest.approx(0.75)
    assert _chroma_similarity_from_distance(0.1, "ip") == pytest.approx(0.9)


def test_unwrapped_chroma_scores_drop_the_verbatim_match() -> None:
    """Document the upstream bug: raw distances make mem0 drop the closest match."""
    ranked = _rank_with_mem0(_chroma_store())

    ranked_ids = [entry["id"] for entry in ranked]
    assert "verbatim" not in ranked_ids
    # What survives is ranked worst-first because distances sort descending.
    assert ranked_ids == ["unrelated", "related"]


def test_wrapped_chroma_scores_return_the_verbatim_match_first() -> None:
    """With similarity scores installed, mem0 keeps and top-ranks the verbatim match."""
    store = _chroma_store()
    _install_chroma_similarity_scores(store)

    hits = store.search(
        query="IonQ COI lawyer review",
        vectors=[1.0, 0.0, 0.0],
        top_k=3,
        filters=_SCOPE_FILTER,
    )
    scores = {hit.id: hit.score for hit in hits}
    assert scores["verbatim"] == pytest.approx(1.0, abs=1e-6)
    assert scores["verbatim"] > scores["related"] > scores["unrelated"]

    ranked = _rank_with_mem0(store)
    assert [entry["id"] for entry in ranked] == ["verbatim", "related"]
    assert ranked[0]["payload"]["data"] == "IonQ MindRoom COI lawyer review"


def test_install_ignores_non_chroma_stores() -> None:
    """Non-Chroma vector stores are left untouched."""
    sentinel = object()
    _install_chroma_similarity_scores(sentinel)
