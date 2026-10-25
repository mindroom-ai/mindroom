"""The owned knowledge code reproduces the knowledge goldens.

The embedder requests and search results regenerate from owned code (``tests.agno_parity.knowledge_goldens``); the
rows are the Chroma rows Agno wrote, which owned refreshes must still write and the owned read path must still search.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import chromadb
import pytest

from mindroom.knowledge.read_proxy import ChromaReadProxy
from mindroom.knowledge.search import (
    KNOWLEDGE_SEARCH_TOOL_NAME,
    AgentKnowledge,
    KnowledgeSource,
    KnowledgeSourceDescription,
    _format_search_hits,
    _knowledge_search_tool_description,
)
from mindroom.tool_system.agno_toolkits import adapt_function
from mindroom.tool_system.tool_access import tool_definition
from tests.agno_parity.golden_files import render, sorted_keys
from tests.agno_parity.knowledge_goldens import (
    EMBEDDERS_PATH,
    GOLDEN_DIR,
    ROWS_PATH,
    SEARCH_PATH,
    ParityEmbedder,
    embedder_case,
    golden_config,
    install_parity_embedder,
    load,
    refresh_golden_bases,
    regenerated_search,
)

if TYPE_CHECKING:
    from pathlib import Path

EMBEDDER_GOLDENS: dict[str, Any] = load(EMBEDDERS_PATH)
ROW_GOLDENS: dict[str, Any] = load(ROWS_PATH)
SEARCH_GOLDENS: dict[str, Any] = load(SEARCH_PATH)


@pytest.mark.parametrize("path", [EMBEDDERS_PATH, ROWS_PATH, SEARCH_PATH], ids=lambda path: path.name)
def test_every_knowledge_golden_is_written_as_the_regeneration_writes_it(path: Path) -> None:
    """Each file is pretty JSON with sorted keys, so regenerating an unchanged golden rewrites nothing."""
    text = path.read_text(encoding="utf-8")
    assert render(sorted_keys(json.loads(text))) == text


@pytest.mark.parametrize("case", sorted(EMBEDDER_GOLDENS))
def test_embedders_send_the_request_bodies_agno_sent(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every embedder call sends the same request body (or local model call) for the same purpose and texts."""
    golden = EMBEDDER_GOLDENS[case]

    assert embedder_case(golden, tmp_path, monkeypatch) == golden


def _with_approximate_distances(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare distances to float32 precision; Chroma's own arithmetic decides their last digits."""
    approximate: list[dict[str, Any]] = []
    for document in documents:
        meta_data = dict(document["meta_data"])
        for key in ("distances", "similarity_score"):
            meta_data[key] = pytest.approx(meta_data[key], rel=1e-6)
        approximate.append({**document, "meta_data": meta_data})
    return approximate


@pytest.fixture
def parity_embedder(monkeypatch: pytest.MonkeyPatch) -> ParityEmbedder:
    """Embed refreshes and searches with the goldens' deterministic vectors."""
    return install_parity_embedder(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("parity_embedder")
async def test_refreshes_write_the_rows_and_collection_settings_agno_wrote(tmp_path: Path) -> None:
    """Every chunk row keeps its document, metadata keys and values, and embedding; collections keep cosine space."""
    config, runtime_paths = golden_config(tmp_path)

    assert await refresh_golden_bases(config, runtime_paths) == ROW_GOLDENS["bases"]


@pytest.mark.asyncio
async def test_rows_agno_wrote_stay_searchable(tmp_path: Path, parity_embedder: ParityEmbedder) -> None:
    """A collection Agno created and filled answers the owned read path exactly as the owned search tool does."""
    handbook = ROW_GOLDENS["bases"]["handbook"]
    client = chromadb.PersistentClient(path=str(tmp_path))
    try:
        collection = client.create_collection("agno_written", metadata=handbook["collection_metadata"])
        collection.add(
            ids=[f"{index:032x}" for index in range(len(handbook["rows"]))],
            embeddings=[row["embedding"] for row in handbook["rows"]],
            documents=[row["document"] for row in handbook["rows"]],
            metadatas=[row["metadata"] for row in handbook["rows"]],
        )
    finally:
        client.close()
    proxy = ChromaReadProxy("agno_written", str(tmp_path), parity_embedder)

    for search in SEARCH_GOLDENS["searches"]:
        if search["agent"] != "librarian" or "documents" not in search:
            continue
        hits = await proxy.asearch(search["query"], limit=10)
        assert json.loads(_format_search_hits(hits)) == _with_approximate_distances(search["documents"])


@pytest.mark.asyncio
async def test_search_tool_results_match_agnos(tmp_path: Path, parity_embedder: ParityEmbedder) -> None:
    """The adapted search tool returns the golden results over the refreshed golden bases.

    Documents keep their values and layout, merged bases interleave, an empty base reports no documents, and a
    failed query embedding reports the error type.
    """
    regenerated = await regenerated_search(tmp_path, parity_embedder)

    expected = [
        {**search, "documents": _with_approximate_distances(search["documents"])} if "documents" in search else search
        for search in SEARCH_GOLDENS["searches"]
    ]
    assert regenerated == {**SEARCH_GOLDENS, "searches": expected}


@pytest.mark.parametrize("memory_search_available", [False, True])
def test_search_function_definition_is_the_golden_tool(memory_search_available: bool) -> None:
    """The adapted search tool's definition is the ``search_knowledge_base`` entry the prompt goldens recorded."""
    source = KnowledgeSource(
        description=KnowledgeSourceDescription(base_id="handbook", description="The team handbook and its policies."),
        index=cast("ChromaReadProxy", object()),
    )
    knowledge = AgentKnowledge(sources=(source,))

    definition = tool_definition(
        adapt_function(knowledge.search_function(memory_search_available=memory_search_available)).definition(),
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
