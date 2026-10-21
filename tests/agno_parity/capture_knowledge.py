"""Capture what MindRoom's Agno-backed knowledge path sends to embedders, writes to Chroma, and returns to the model.

Run ``.venv/bin/python -m tests.agno_parity.capture_knowledge`` from the repository root to rewrite the fixtures in
``tests/fixtures/agno_parity/knowledge/``.
Every embedder request is answered by a mock transport or a fake model, refreshes run against a real Chroma store in a
temporary directory, and searches run through Agno's own ``search_knowledge_base`` tool.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import chromadb
import httpx
import numpy as np
import ollama
from agno.agent import Agent
from agno.agent._default_tools import create_knowledge_search_tool
from agno.knowledge.embedder.base import Embedder
from openai import AsyncOpenAI, OpenAI

import mindroom.embeddings.signatures as embedding_signatures
import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.main import Config
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.embeddings.factory import create_configured_embedder
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding
from mindroom.knowledge.registry import load_published_index_state, published_index_metadata_path
from mindroom.knowledge.registry import resolve_published_index_key as resolve_index_key
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from tests.agno_parity.knowledge_vectors import PARITY_VECTOR_DIMENSIONS, parity_vector
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "knowledge"

#: Every source file gets this mtime, so ``source_mtime_ns`` is part of the golden.
SOURCE_MTIME_NS = 1_790_000_000_000_000_000
CHUNK_SIZE = 128
CHUNK_OVERLAP = 24
#: The width ``OllamaEmbedder`` requests and accepts.
OLLAMA_DIMENSIONS = 4096

HANDBOOK_FILES = {
    "guide.md": (
        "# Travel guide\n\n"
        "Conference travel needs approval from your lead two weeks ahead of the trip.\n"
        "Book economy class for flights under six hours, and keep every receipt you get.\n\n"
        "## Hotels\n\n"
        "Pick a hotel within walking distance of the venue. The nightly cap is 180 EUR in most cities.\n"
    ),
    "team/policies.txt": (
        "Plain notes with\ttabs and  double  spaces.\r\n"
        "Expense reports are due within thirty days of the expense, and late reports need a short written reason.\n"
    ),
    "records.json": json.dumps(
        [{"name": "Ada", "role": "lead"}, {"name": "Grace", "role": "engineer", "tags": ["cobol", "navy"]}],
    ),
    "settings.json": json.dumps({"timezone": "UTC", "features": {"search": True}}),
    "broken.json": '{"unterminated": [1, 2,\n',
    "table.csv": "city,cap\nBerlin,180\nParis,  200  \n\nLondon,220\n",
    "empty.md": "",
    "control.txt": "before\x00after the null byte",
    "unicode.md": "Café menu: crème brûlée and 東京 ramen, served until late.\n",
}
NOTES_FILES = {"notes.md": "Remember to book the conference hotel early, because the venue fills up fast.\n"}
EMPTY_FILES = {"blank.md": ""}

SEARCH_QUERIES = ("hotel nightly cap", "who leads the team")

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _base_config(path: str, description: str) -> dict[str, Any]:
    return {"path": path, "description": description, "chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP}


def knowledge_config() -> dict[str, Any]:
    """Return the authored config every refresh and search golden uses."""
    return {
        "agents": {
            "librarian": {"display_name": "Librarian", "knowledge_bases": ["handbook"]},
            "researcher": {"display_name": "Researcher", "knowledge_bases": ["handbook", "notes"]},
            "archivist": {"display_name": "Archivist", "knowledge_bases": ["empty"]},
        },
        "models": {},
        "knowledge_bases": {
            "handbook": _base_config("./handbook", "The team handbook."),
            "notes": _base_config("./notes", "Personal travel notes."),
            "empty": _base_config("./empty", "A base without content."),
        },
    }


def write_sources(root: Path) -> None:
    """Write every base's source files with a fixed mtime."""
    for directory, files in (("handbook", HANDBOOK_FILES), ("notes", NOTES_FILES), ("empty", EMPTY_FILES)):
        for relative_path, text in files.items():
            path = root / directory / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            os.utime(path, ns=(SOURCE_MTIME_NS, SOURCE_MTIME_NS))


def bound_config(root: Path, authored: dict[str, Any]) -> tuple[Config, RuntimePaths]:
    """Bind one authored config to runtime paths under ``root``."""
    runtime_paths = test_runtime_paths(root)
    return bind_runtime_paths(Config.model_validate(authored), runtime_paths), runtime_paths


# --------------------------------------------------------------------------
# Embedder request bodies
# --------------------------------------------------------------------------


@dataclass
class _RecordedRequests:
    requests: list[dict[str, Any]] = field(default_factory=list)

    def record(self, request: httpx.Request) -> None:
        self.requests.append(
            {"method": request.method, "url": str(request.url), "body": json.loads(request.content)},
        )


def _openai_transport(recorded: _RecordedRequests) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        recorded.record(request)
        inputs = json.loads(request.content)["input"]
        count = 1 if isinstance(inputs, str) else len(inputs)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "parity",
                "data": [{"object": "embedding", "index": index, "embedding": [0.5, 0.25]} for index in range(count)],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    return handle


def _ollama_transport(recorded: _RecordedRequests) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        recorded.record(request)
        return httpx.Response(200, json={"model": "parity", "embeddings": [[0.5] * OLLAMA_DIMENSIONS]})

    return handle


class _RecordingSentenceTransformer:
    """Records the calls a local embedder makes to its sentence-transformers model."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def encode_query(self, text: object, **kwargs: object) -> np.ndarray:
        self.calls.append({"method": "encode_query", "input": text, "kwargs": kwargs})
        return np.array([0.5, 0.25])

    def encode_document(self, text: object, **kwargs: object) -> np.ndarray:
        self.calls.append({"method": "encode_document", "input": text, "kwargs": kwargs})
        return np.array([0.5, 0.25])


#: Every embedder call the golden pins: the Agno-era method, the owned purpose, and the texts.
EMBEDDER_CALLS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("get_embedding", "query", ("Which hotels are allowed?",)),
    ("get_embedding_and_usage", "document", ("files/report.md is a path, not a file reference",)),
    ("async_get_embedding", "query", ("async query",)),
    ("async_get_embedding_and_usage", "document", ("async document",)),
)
OPENAI_BATCH_CALLS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("get_embeddings_batch", "document", ("first chunk", "files/second chunk")),
    ("async_get_embeddings_batch_and_usage", "query", ("graph name", "graph fact", "graph search")),
)

EMBEDDER_CASES: dict[str, dict[str, Any]] = {
    "openai_default_dimensions": {
        "provider": "openai",
        "config": {"model": "text-embedding-3-small", "api_key": "sk-parity"},
    },
    "openai_explicit_dimensions": {
        "provider": "openai",
        "config": {"model": "text-embedding-3-large", "api_key": "sk-parity", "dimensions": 256},
    },
    "openai_compatible_prefixes": {
        "provider": "openai",
        "config": {
            "model": "embeddinggemma-2",
            "api_key": "sk-parity",
            "host": "http://embeddings.local:8080/v1",
            "query_prefix": "task: search result | query: ",
            "document_prefix": "title: none | text: ",
        },
    },
    "ollama": {
        "provider": "ollama",
        "config": {"model": "nomic-embed-text", "host": "http://ollama.local:11434", "dimensions": 768},
    },
    "sentence_transformers": {
        "provider": "sentence_transformers",
        "config": {"model": "google/embeddinggemma-2-300m"},
    },
}


async def _call(embedder: Any, method: str, texts: tuple[str, ...]) -> None:  # noqa: ANN401
    if method == "async_get_embeddings_batch_and_usage":
        await embedder.async_get_embeddings_batch_and_usage(list(texts), query=True)
        return
    argument: object = list(texts) if method == "get_embeddings_batch" else texts[0]
    result = getattr(embedder, method)(argument)
    if asyncio.iscoroutine(result):
        await result


async def _capture_embedder_case(root: Path, name: str, embedder_config: dict[str, Any]) -> dict[str, Any]:
    config, runtime_paths = bound_config(root / name, {"models": {}, "memory": {"embedder": embedder_config}})
    provider = embedder_config["provider"]
    calls = EMBEDDER_CALLS + (OPENAI_BATCH_CALLS if provider == "openai" else ())
    captured: list[dict[str, Any]] = []
    model = _RecordingSentenceTransformer()
    if provider == "sentence_transformers":
        # The cached client stands in for the real model, which would download weights.
        embedding_signatures._SENTENCE_TRANSFORMER_CLIENT = (embedder_config["config"]["model"], model)
    for method, purpose, texts in calls:
        recorded = _RecordedRequests()
        embedder = create_configured_embedder(config, runtime_paths)
        if provider == "openai":
            transport = _openai_transport(recorded)
            embedder.openai_client = OpenAI(
                api_key=embedder.api_key,
                base_url=embedder.base_url,
                http_client=httpx.Client(transport=httpx.MockTransport(transport)),
            )
            embedder.async_client = AsyncOpenAI(
                api_key=embedder.api_key,
                base_url=embedder.base_url,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
            )
        elif provider == "ollama":
            transport = _ollama_transport(recorded)
            embedder.ollama_client = ollama.Client(host=embedder.host, transport=httpx.MockTransport(transport))
            embedder.async_client = ollama.AsyncClient(host=embedder.host, transport=httpx.MockTransport(transport))
        model.calls.clear()
        await _call(embedder, method, texts)
        entry: dict[str, Any] = {"agno_method": method, "purpose": purpose, "texts": list(texts)}
        if provider == "sentence_transformers":
            entry["model_calls"] = list(model.calls)
        else:
            entry["requests"] = recorded.requests
        captured.append(entry)
    embedding_signatures._SENTENCE_TRANSFORMER_CLIENT = None
    return {"embedder": embedder_config, "calls": captured}


async def capture_embedders(root: Path) -> dict[str, Any]:
    """Return the request bodies (or local model calls) of every embedder case."""
    return {name: await _capture_embedder_case(root, name, case) for name, case in EMBEDDER_CASES.items()}


# --------------------------------------------------------------------------
# Chroma rows and search results
# --------------------------------------------------------------------------


@dataclass
class ParityEmbedder(Embedder):
    """Agno embedder whose vectors are ``parity_vector`` of each text; it fails every request when ``failing``."""

    failing: bool = False
    dimensions: int | None = PARITY_VECTOR_DIMENSIONS

    def _vector(self, text: str) -> list[float]:
        if self.failing:
            message = "embedder request failed (HTTP 503)"
            raise EmbedderRequestError(message)
        return parity_vector(text)

    def get_embedding(self, text: str) -> list[float]:
        """Embed a query."""
        return self._vector(text)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Embed a document."""
        return self._vector(text), None

    async def async_get_embedding(self, text: str) -> list[float]:
        """Embed a query."""
        return self._vector(text)

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Embed a document."""
        return self._vector(text), None


def _normalized_row(row_id: str, document: str, metadata: dict[str, Any], embedding: list[float]) -> dict[str, Any]:
    assert _HEX32.fullmatch(row_id), row_id
    normalized = dict(metadata)
    assert _HEX64.fullmatch(str(normalized["content_hash"])), normalized
    assert _UUID.fullmatch(str(normalized["content_id"])), normalized
    normalized["content_hash"] = "<sha256>"
    normalized["content_id"] = "<uuid5>"
    return {"id": "<md5>", "document": document, "metadata": dict(sorted(normalized.items())), "embedding": embedding}


def _row_sort_key(row: dict[str, Any]) -> tuple[str, int, int, int, str]:
    metadata = row["metadata"]
    return (
        metadata["source_path"],
        int(metadata.get("page", 0)),
        int(metadata.get("row_number", 0)),
        int(metadata.get("chunk", 0)),
        row["document"],
    )


def published_collection_rows(storage_path: Path, collection_name: str) -> dict[str, Any]:
    """Read one collection's rows and settings, normalizing the per-refresh random values."""
    client = chromadb.PersistentClient(path=str(storage_path))
    try:
        collection = client.get_collection(name=collection_name)
        rows = collection.get(include=["documents", "metadatas", "embeddings"])
        normalized = [
            _normalized_row(row_id, document, dict(metadata), [float(value) for value in embedding])
            for row_id, document, metadata, embedding in zip(
                rows["ids"],
                rows["documents"],
                rows["metadatas"],
                rows["embeddings"],
                strict=True,
            )
        ]
        return {
            "collection_metadata": collection.metadata,
            "collection_configuration": collection.configuration_json,
            "rows": sorted(normalized, key=_row_sort_key),
        }
    finally:
        client.close()


async def _refresh(base_id: str, config: Config, runtime_paths: RuntimePaths) -> tuple[Path, str]:
    result = await _refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths)
    assert result.indexed_count > 0 or base_id == "empty", result
    key = resolve_index_key(base_id, config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert state is not None
    assert state.status == "complete", state
    assert state.collection is not None
    return knowledge_registry.published_index_storage_path(key), state.collection


async def _search_tool_output(agent_name: str, query: str, config: Config, runtime_paths: RuntimePaths) -> str:
    knowledge_registry._published_indexes.clear()
    knowledge = resolve_agent_knowledge_access(agent_name, config, runtime_paths).knowledge
    assert knowledge is not None
    agent = Agent(knowledge=knowledge, search_knowledge=True)
    function = create_knowledge_search_tool(agent, async_mode=True)
    assert function.entrypoint is not None
    return await function.entrypoint(query=query)


def _search_entry(agent_name: str, query: str, output: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"agent": agent_name, "query": query}
    if not output.startswith("["):
        entry["text"] = output
        return entry
    # Agno renders each document from an unordered set and Chroma returns metadata in hash order,
    # so the values and the layout of the JSON are stable, but not its key order.
    assert output == json.dumps(json.loads(output), indent=2, ensure_ascii=False)
    documents = json.loads(output)
    for document in documents:
        assert _HEX64.fullmatch(document["meta_data"]["content_hash"]), document
        document["meta_data"]["content_hash"] = "<sha256>"
    entry["documents"] = documents
    return entry


async def capture_rows_and_search(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Refresh every base through the production refresh path, then read rows and search through Agno's tool."""
    write_sources(root)
    authored = knowledge_config()
    config, runtime_paths = bound_config(root, authored)
    embedder = ParityEmbedder()
    with (
        patch("mindroom.knowledge.manager.create_configured_embedder", lambda *_args, **_kwargs: embedder),
        patch("mindroom.knowledge.registry.create_configured_embedder", lambda *_args, **_kwargs: embedder),
    ):
        published = {
            base_id: await _refresh(base_id, config, runtime_paths) for base_id in ("empty", "handbook", "notes")
        }
        rows = {
            "input": {
                "config": authored,
                "files": {"handbook": HANDBOOK_FILES, "notes": NOTES_FILES, "empty": EMPTY_FILES},
                "source_mtime_ns": SOURCE_MTIME_NS,
                "embedding": "tests.agno_parity.knowledge_vectors.parity_vector of each chunk",
            },
            "bases": {
                base_id: published_collection_rows(storage_path, collection)
                for base_id, (storage_path, collection) in published.items()
            },
        }
        searches = [
            _search_entry(agent_name, query, await _search_tool_output(agent_name, query, config, runtime_paths))
            for agent_name in ("librarian", "researcher")
            for query in SEARCH_QUERIES
        ]
        searches.append(
            _search_entry(
                "archivist",
                SEARCH_QUERIES[0],
                await _search_tool_output("archivist", SEARCH_QUERIES[0], config, runtime_paths),
            ),
        )
        embedder.failing = True
        searches.append(
            _search_entry(
                "librarian",
                SEARCH_QUERIES[0],
                await _search_tool_output("librarian", SEARCH_QUERIES[0], config, runtime_paths),
            ),
        )
    knowledge_registry._published_indexes.clear()
    search = {
        "input": {"config": authored, "query_embedding": "parity_vector of the query", "failing_query_embedder": True},
        "format": {"indent": 2, "ensure_ascii": False},
        "searches": searches,
    }
    return rows, search


def _dump(name: str, payload: dict[str, Any], *, sort_keys: bool) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=sort_keys) + "\n"
    (FIXTURE_DIR / name).write_text(text, encoding="utf-8")


async def _capture_all() -> None:
    with tempfile.TemporaryDirectory(prefix="mindroom-knowledge-goldens-") as temporary:
        root = Path(temporary).resolve()
        embedders = await capture_embedders(root / "embedders")
        rows, search = await capture_rows_and_search(root / "store")
    _dump("embedders.json", embedders, sort_keys=True)
    _dump("rows.json", rows, sort_keys=True)
    _dump("search.json", search, sort_keys=True)


def main() -> None:
    """Rewrite every knowledge golden."""
    asyncio.run(_capture_all())


if __name__ == "__main__":
    main()
