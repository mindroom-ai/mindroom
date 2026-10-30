"""The owned knowledge code reproduces the knowledge goldens.

The embedder requests and search results regenerate from owned code (``tests.agno_parity.knowledge_goldens``); the
rows are the rows Agno wrote, which owned refreshes must still write and the owned read path must still search.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import pytest

from mindroom.knowledge.index_metadata import new_publication, save_publication
from mindroom.knowledge.index_reader import PublishedIndexReader
from mindroom.knowledge.registry import resolve_published_index_key
from mindroom.knowledge.search import (
    KNOWLEDGE_SEARCH_TOOL_NAME,
    AgentKnowledge,
    KnowledgeSource,
    KnowledgeSourceDescription,
    _format_search_hits,
    _knowledge_search_tool_description,
)
from mindroom.knowledge.vector_store import KnowledgeCollection, StoredRow
from mindroom.tool_system.tool_access import tool_definition
from tests.agno_parity import knowledge_goldens
from tests.agno_parity.golden_files import render
from tests.agno_parity.knowledge_goldens import (
    EMBEDDERS_PATH,
    GOLDEN_DIR,
    ROWS_PATH,
    SEARCH_PATH,
    ParityEmbedder,
    distances_as_recorded,
    embedder_case,
    golden_config,
    install_parity_embedder,
    load,
    refresh_golden_bases,
    regenerated_search,
)
from tests.knowledge_test_support import collection_meta

if TYPE_CHECKING:
    from pathlib import Path

EMBEDDER_GOLDENS: dict[str, Any] = load(EMBEDDERS_PATH)
ROW_GOLDENS: dict[str, Any] = load(ROWS_PATH)
SEARCH_GOLDENS: dict[str, Any] = load(SEARCH_PATH)


@pytest.mark.parametrize("case", sorted(EMBEDDER_GOLDENS))
def test_embedders_send_the_golden_request_bodies(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every embedder call sends the same request body (or local model call) for the same purpose and texts."""
    golden = EMBEDDER_GOLDENS[case]

    assert embedder_case(golden, tmp_path, monkeypatch) == golden


def test_an_embedder_golden_ignores_the_shells_openai_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's ``OPENAI_BASE_URL``, such as a local model server's, reaches neither the case nor its regeneration."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    golden = EMBEDDER_GOLDENS["openai_default_dimensions"]

    assert embedder_case(golden, tmp_path, monkeypatch) == golden


@pytest.fixture
def parity_embedder(monkeypatch: pytest.MonkeyPatch) -> ParityEmbedder:
    """Embed refreshes and searches with the goldens' deterministic vectors."""
    return install_parity_embedder(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("parity_embedder")
async def test_refreshes_write_the_golden_rows(tmp_path: Path) -> None:
    """Every chunk row keeps its document, metadata keys and values, and embedding.

    Chroma returned some embeddings up to one float32 ulp from the values it was given, and the owned store keeps the
    given values, so embeddings compare within float32 precision.
    """
    config, runtime_paths = golden_config(tmp_path)

    refreshed = await refresh_golden_bases(config, runtime_paths)

    assert set(refreshed) == set(ROW_GOLDENS["bases"])
    for base_id, base in ROW_GOLDENS["bases"].items():
        rows = refreshed[base_id]["rows"]
        assert [{**row, "embedding": None} for row in rows] == [{**row, "embedding": None} for row in base["rows"]]
        for row, golden_row in zip(rows, base["rows"], strict=True):
            assert row["embedding"] == pytest.approx(golden_row["embedding"], rel=1e-6, abs=1e-7)


@pytest.mark.asyncio
async def test_rows_agno_wrote_stay_searchable(tmp_path: Path, parity_embedder: ParityEmbedder) -> None:
    """The rows Agno wrote, stored as they are, answer the owned read path exactly as the owned search tool does."""
    handbook = ROW_GOLDENS["bases"]["handbook"]
    config, runtime_paths = golden_config(tmp_path / "root")
    settings = resolve_published_index_key("handbook", config=config, runtime_paths=runtime_paths).indexing_settings
    store = tmp_path / "store"
    collection = KnowledgeCollection(path=store, name="agno_written")
    collection.create(collection_meta(settings.to_metadata(), stage="complete"))
    source_paths = list(dict.fromkeys(row["metadata"]["source_path"] for row in handbook["rows"]))
    for source_path in source_paths:
        first = next(row for row in handbook["rows"] if row["metadata"]["source_path"] == source_path)
        collection.write_file(
            source_path,
            (0, first["metadata"]["source_size"], first["metadata"]["source_digest"]),
            [
                StoredRow(
                    id=f"{index:032x}",
                    document=row["document"],
                    metadata=row["metadata"],
                    embedding=row["embedding"],
                )
                for index, row in enumerate(handbook["rows"])
                if row["metadata"]["source_path"] == source_path
            ],
        )
    save_publication(
        store,
        new_publication(
            settings=settings,
            collection=collection.name,
            indexed_count=len(source_paths),
            source_signature="agno-written",
            published_revision=None,
        ),
    )
    reader = PublishedIndexReader(path=store, embedder=parity_embedder, settings=settings)

    for search in SEARCH_GOLDENS["searches"]:
        if search["agent"] != "librarian" or "documents" not in search:
            continue
        hits = await reader.asearch(search["query"], limit=10)
        documents = search["documents"]
        assert distances_as_recorded(json.loads(_format_search_hits(hits)), documents) == documents


@pytest.mark.asyncio
async def test_search_tool_results_match_the_search_golden(tmp_path: Path, parity_embedder: ParityEmbedder) -> None:
    """The adapted search tool returns the golden results over the refreshed golden bases.

    Documents keep their values and layout, merged bases interleave, an empty base reports no documents, and a
    failed query embedding reports the error type.
    """
    assert await regenerated_search(tmp_path, parity_embedder) == SEARCH_GOLDENS


@pytest.mark.asyncio
async def test_the_failing_search_still_fails_after_its_error_text_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parity_embedder: ParityEmbedder,
) -> None:
    """Which search's embedding fails is authored, so a reworded error regenerates as the new error, not a search."""
    reworded = deepcopy(SEARCH_GOLDENS)
    [failing] = [search for search in reworded["searches"] if search.get("embedding_fails")]
    failing["text"] = "Knowledge search failed: EmbedderRequestError"
    golden = tmp_path / "search.json"
    golden.write_text(render(reworded), encoding="utf-8")
    monkeypatch.setattr(knowledge_goldens, "SEARCH_PATH", golden)

    regenerated = await regenerated_search(tmp_path / "root", parity_embedder)

    assert regenerated == SEARCH_GOLDENS


def test_a_distance_differing_only_in_float32_last_bits_regenerates_as_recorded() -> None:
    """An aarch64 machine's distance one float32 ulp from the recorded one keeps the recorded value; a real change does not.

    ``0.6045231819152832`` is what an aarch64 run returns for the distance an x86_64 run recorded as
    ``0.604523241519928``.
    """
    recorded = next(search["documents"] for search in SEARCH_GOLDENS["searches"] if "documents" in search)
    [index] = [
        index for index, document in enumerate(recorded) if document["meta_data"]["distance"] == 0.604523241519928
    ]
    on_aarch64 = deepcopy(recorded)
    on_aarch64[index]["meta_data"] |= {"distance": 0.6045231819152832}
    moved = deepcopy(recorded)
    moved[index]["meta_data"]["distance"] = 0.6046
    replaced = deepcopy(on_aarch64)
    replaced[index]["content"] = "Another chunk."

    assert distances_as_recorded(on_aarch64, recorded) == recorded
    assert distances_as_recorded(moved, recorded)[index]["meta_data"]["distance"] == 0.6046
    assert distances_as_recorded(replaced, recorded)[index]["meta_data"]["distance"] == 0.6045231819152832


@pytest.mark.parametrize("memory_search_available", [False, True])
def test_search_function_definition_is_the_golden_tool(memory_search_available: bool) -> None:
    """The adapted search tool's definition is the ``search_knowledge_base`` entry the prompt goldens recorded."""
    source = KnowledgeSource(
        description=KnowledgeSourceDescription(base_id="handbook", description="The team handbook and its policies."),
        index=cast("PublishedIndexReader", object()),
    )
    knowledge = AgentKnowledge(sources=(source,))

    definition = tool_definition(
        knowledge.search_function(memory_search_available=memory_search_available).definition(),
    )

    prompt_golden = json.loads((GOLDEN_DIR.parent / "prompts" / "agent_openai_chat.json").read_text(encoding="utf-8"))
    [golden_tool] = [
        tool
        for request in prompt_golden["requests"]
        for tool in request["tools"]
        if tool["name"] == KNOWLEDGE_SEARCH_TOOL_NAME
    ][:1]
    expected_description = _knowledge_search_tool_description(
        (source.description,),
        memory_search_available=memory_search_available,
    )
    assert definition == {**golden_tool, "description": expected_description}
    if not memory_search_available:
        assert definition == golden_tool


def test_prompt_block_is_the_one_in_every_prompt_golden() -> None:
    """The owned prompt block is the ``<knowledge_base>`` block every knowledge-enabled prompt golden carries."""
    block = AgentKnowledge(sources=()).prompt_block()
    carrying = [
        path.name
        for path in sorted((GOLDEN_DIR.parent / "prompts").glob("*.json"))
        if "<knowledge_base>" in path.read_text(encoding="utf-8")
    ]
    assert carrying
    for name in carrying:
        golden = json.loads((GOLDEN_DIR.parent / "prompts" / name).read_text(encoding="utf-8"))
        system_texts = [
            message["content"]
            for request in golden["requests"]
            for message in request["messages"]
            if message["role"] == "system" and "<knowledge_base>" in message["content"]
        ]
        assert system_texts, name
        assert all(f"{block}\n" in text or text.endswith(block) for text in system_texts), name
