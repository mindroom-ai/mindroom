"""Compute the knowledge goldens from the owned knowledge code: embedder requests, refreshed rows, and search results.

`tests/fixtures/agno_parity/README.md` (Knowledge goldens) describes the files.
`tests/test_knowledge_goldens.py` compares what these functions compute with the goldens, and
`tests.agno_parity.regenerate_goldens` rewrites `embedders.json` and `search.json` through the same functions.
`rows.json` holds the rows Agno wrote, so it stays a read-only input: its authored `input` holds the config and
source files every refresh and search runs on, and its rows are what owned refreshes must still write.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import httpx2
import numpy as np
import ollama
from openai import AsyncOpenAI, OpenAI

import mindroom.embeddings.sentence_transformers_embedder as local_embedder
from mindroom.config.main import Config
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.embeddings.factory import create_configured_embedder
from mindroom.embeddings.ollama_embedder import OllamaEmbedder
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding
from mindroom.knowledge.registry import (
    load_published_index_state,
    published_index_metadata_path,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.tool_system.agno_toolkits import adapt_function
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from tests.agno_parity.golden_files import sorted_keys
from tests.agno_parity.knowledge_vectors import parity_vector
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.providers.golden_support import hide_provider_environment

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder, EmbeddingPurpose
    from mindroom.knowledge.search import AgentKnowledge

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "knowledge"
EMBEDDERS_PATH = GOLDEN_DIR / "embedders.json"
ROWS_PATH = GOLDEN_DIR / "rows.json"
SEARCH_PATH = GOLDEN_DIR / "search.json"
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_QUERY_EMBEDDING_ERROR = "embedder request failed (HTTP 503)"
# Each cosine distance is computed in float32 and summed in an order the CPU's vector width decides, so its last bits
# can differ between machines: an x86_64 run returned 0.604523241519928 where an aarch64 run returned
# 0.6045231819152832, one float32 ulp apart. Comparisons accept up to this relative difference in a search document's
# distances.
DISTANCE_KEYS = ("distances", "similarity_score")
DISTANCE_RELATIVE_TOLERANCE = 1e-6


def load(path: Path) -> dict[str, Any]:
    """Return one knowledge golden."""
    return json.loads(path.read_text(encoding="utf-8"))


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def openai(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append({"method": request.method, "url": str(request.url), "body": json.loads(request.content)})
        inputs = json.loads(request.content)["input"]
        count = 1 if isinstance(inputs, str) else len(inputs)
        data = [{"object": "embedding", "index": index, "embedding": [0.5, 0.25]} for index in range(count)]
        return httpx2.Response(200, json={"object": "list", "model": "parity", "data": data})

    def ollama(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"method": request.method, "url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"model": "parity", "embeddings": [[0.5, 0.25]]})


class _RecordingModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def encode_query(self, text: str, **kwargs: object) -> np.ndarray:
        self.calls.append({"method": "encode_query", "input": text, "kwargs": kwargs})
        return np.array([0.5, 0.25])

    def encode_document(self, text: str, **kwargs: object) -> np.ndarray:
        self.calls.append({"method": "encode_document", "input": text, "kwargs": kwargs})
        return np.array([0.5, 0.25])


def _wire(embedder: Embedder, recorder: _Recorder) -> None:
    """Answer the embedder's SDK requests with canned replies, recording each one."""
    if isinstance(embedder, OpenAIEmbedder):
        embedder.client = OpenAI(
            api_key=embedder.api_key,
            base_url=embedder.base_url,
            http_client=httpx2.Client(transport=httpx2.MockTransport(recorder.openai)),
        )
        embedder.async_client = AsyncOpenAI(
            api_key=embedder.api_key,
            base_url=embedder.base_url,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder.openai)),
        )
    elif isinstance(embedder, OllamaEmbedder):
        embedder.client = ollama.Client(host=embedder.host, transport=httpx.MockTransport(recorder.ollama))
        embedder.async_client = ollama.AsyncClient(host=embedder.host, transport=httpx.MockTransport(recorder.ollama))


def embedder_case(case: dict[str, Any], root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return one embedder case with what each of its calls sent: its HTTP requests, or its local model calls.

    Each call builds the authored embedder afresh and embeds its texts for its purpose, asynchronously when the
    Agno-era method it stands for was asynchronous. Provider endpoints and credentials are hidden from the
    environment, so an SDK fallback such as ``OPENAI_BASE_URL`` cannot reach a recorded request.
    """
    hide_provider_environment(monkeypatch)
    runtime_paths = test_runtime_paths(root)
    config = bind_runtime_paths(
        Config.model_validate({"models": {}, "memory": {"embedder": case["embedder"]}}),
        runtime_paths,
    )
    model = _RecordingModel()
    monkeypatch.setattr(local_embedder, "_SENTENCE_TRANSFORMER_CLIENT", None)
    monkeypatch.setattr(local_embedder, "ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", lambda _model: model)
    calls: list[dict[str, Any]] = []
    for call in case["calls"]:
        recorder = _Recorder()
        model.calls.clear()
        embedder = create_configured_embedder(config, runtime_paths)
        _wire(embedder, recorder)
        texts = call["texts"]
        purpose = cast("EmbeddingPurpose", call["purpose"])
        if call["agno_method"].startswith("async_"):
            vectors = asyncio.run(embedder.aembed(texts, purpose=purpose))
        else:
            vectors = embedder.embed(texts, purpose=purpose)
        assert len(vectors) == len(texts), call["agno_method"]
        sent = {"model_calls": list(model.calls)} if model.calls else {"requests": recorder.requests}
        calls.append({"agno_method": call["agno_method"], "purpose": call["purpose"], "texts": texts, **sent})
    return sorted_keys({"calls": calls, "embedder": case["embedder"]})


class ParityEmbedder:
    """Owned-protocol embedder whose vectors are ``parity_vector`` of each text, as the goldens were embedded.

    ``failing`` makes every embedding fail as an unavailable embedder does.
    """

    sends_batches = False

    def __init__(self) -> None:
        self.failing = False

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return each text's parity vector, or fail when ``failing`` is set."""
        del purpose
        if self.failing:
            raise EmbedderRequestError(_QUERY_EMBEDDING_ERROR)
        return [parity_vector(text) for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return what ``embed`` returns."""
        return self.embed(texts, purpose=purpose)


def install_parity_embedder(monkeypatch: pytest.MonkeyPatch) -> ParityEmbedder:
    """Embed refreshes and searches with the goldens' deterministic vectors, from a fresh set of published indexes."""
    embedder = ParityEmbedder()
    monkeypatch.setattr("mindroom.knowledge.manager.create_configured_embedder", lambda *_args, **_kwargs: embedder)
    monkeypatch.setattr("mindroom.knowledge.registry.create_configured_embedder", lambda *_args, **_kwargs: embedder)
    monkeypatch.setattr("mindroom.knowledge.registry._published_indexes", {})
    return embedder


def golden_config(root: Path) -> tuple[Config, RuntimePaths]:
    """Write the goldens' source files under ``root`` and bind their config there."""
    golden_input = load(ROWS_PATH)["input"]
    mtime = golden_input["source_mtime_ns"]
    for directory, files in golden_input["files"].items():
        for relative_path, text in files.items():
            path = root / directory / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            os.utime(path, ns=(mtime, mtime))
    runtime_paths = test_runtime_paths(root)
    return bind_runtime_paths(Config.model_validate(golden_input["config"]), runtime_paths), runtime_paths


def _normalized_rows(database: Path) -> list[dict[str, Any]]:
    """Read every row of one collection file, checking and normalizing the values that hash per-version identity.

    The stamp is the file's ctime, which no test can set, so it is normalized like the hashes it feeds.
    """
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT id, document, metadata, embedding FROM chunks").fetchall()
    normalized: list[dict[str, Any]] = []
    for row_id, document, metadata, embedding in rows:
        assert _HEX32.fullmatch(row_id), row_id
        stored = json.loads(metadata)
        assert _HEX64.fullmatch(str(stored["content_hash"])), stored
        assert stored["content_id"] == str(uuid.uuid5(uuid.NAMESPACE_DNS, str(stored["content_hash"])))
        stored["content_hash"] = "<sha256>"
        stored["content_id"] = "<uuid5>"
        assert isinstance(stored["source_mtime_ns"], int), stored
        stored["source_mtime_ns"] = "<stamp>"
        normalized.append(
            {
                "id": "<md5>",
                "document": document,
                "metadata": dict(sorted(stored.items())),
                "embedding": [float(value) for value in np.frombuffer(embedding, dtype="<f4")],
            },
        )
    return sorted(
        normalized,
        key=lambda row: (
            row["metadata"]["source_path"],
            int(row["metadata"].get("page", 0)),
            int(row["metadata"].get("row_number", 0)),
            int(row["metadata"].get("chunk", 0)),
            row["document"],
        ),
    )


async def refresh_golden_bases(config: Config, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Refresh every golden base through the production refresh path and read back the rows it published."""
    published: dict[str, Any] = {}
    for base_id in load(ROWS_PATH)["bases"]:
        await _refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths)
        key = resolve_published_index_key(base_id, config=config, runtime_paths=runtime_paths)
        state = load_published_index_state(published_index_metadata_path(key))
        assert state is not None
        assert state.status == "complete"
        assert state.collection is not None
        published[base_id] = {
            "rows": _normalized_rows(published_index_storage_path(key) / f"{state.collection}.sqlite3"),
        }
    return published


def _normalized_search_documents(output: str) -> list[dict[str, Any]]:
    """Parse a search result, checking its layout and normalizing the per-version content hash and stamp."""
    documents = json.loads(output)
    assert output == json.dumps(documents, indent=2, ensure_ascii=False)
    for document in documents:
        assert list(document) == ["name", "meta_data", "content"], document
        assert _HEX64.fullmatch(document["meta_data"]["content_hash"]), document
        document["meta_data"]["content_hash"] = "<sha256>"
        if "source_mtime_ns" in document["meta_data"]:
            assert isinstance(document["meta_data"]["source_mtime_ns"], int), document
            document["meta_data"]["source_mtime_ns"] = "<stamp>"
    return documents


def _within_distance_tolerance(found: float, recorded: float) -> bool:
    """Return whether two distances differ by no more than float32 arithmetic makes them differ."""
    return abs(found - recorded) <= DISTANCE_RELATIVE_TOLERANCE * abs(recorded)


def distances_as_recorded(found: list[dict[str, Any]], recorded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the ``found`` documents with each distance ``recorded`` holds kept where only float32 arithmetic differs.

    A document keeps a recorded distance when it is the recorded document in the same place, apart from its
    distances, and the distance is within ``DISTANCE_RELATIVE_TOLERANCE`` of the recorded one; any other difference,
    a changed document, order, or distance, stays as found, so the comparison and a regeneration show it.
    """
    kept: list[dict[str, Any]] = []
    for index, document in enumerate(found):
        before = recorded[index] if index < len(recorded) else None
        meta_data = document["meta_data"]
        if before is not None and _without_distances(document) == _without_distances(before):
            meta_data = {
                key: before["meta_data"][key]
                if key in DISTANCE_KEYS and _within_distance_tolerance(value, before["meta_data"][key])
                else value
                for key, value in meta_data.items()
            }
        kept.append({**document, "meta_data": meta_data})
    return kept


def _without_distances(document: dict[str, Any]) -> dict[str, Any]:
    meta_data = {key: value for key, value in document["meta_data"].items() if key not in DISTANCE_KEYS}
    return {**document, "meta_data": meta_data}


async def _search_tool_output(knowledge: AgentKnowledge, query: str) -> str:
    """Run the adapted ``search_knowledge_base`` tool the way the loop runs a tool call."""
    function = adapt_function(knowledge.search_function(memory_search_available=False))
    context = ToolCallContext(run_id="run", session_id="session", user_id=None)
    outcome = await execute_tool_call(function, {"query": query}, context, middleware=())
    assert not outcome.is_error
    return outcome.content


async def regenerated_search(root: Path, embedder: ParityEmbedder) -> dict[str, Any]:
    """Return the search golden the owned search tool regenerates over the golden bases refreshed under ``root``.

    The agents and their bases are ``rows.json``'s authored ``input``, which the refresh also reads. Each search is
    the tool call of its agent: the documents it found, or the text it answered with, as its golden
    records. A search the golden marks ``embedding_fails`` runs with its query embedding failing. Distances that
    differ from the golden's only by float32 arithmetic keep the golden's value, so every machine regenerates the
    same file.
    """
    golden = load(SEARCH_PATH)
    config, runtime_paths = golden_config(root)
    await refresh_golden_bases(config, runtime_paths)
    searches: list[dict[str, Any]] = []
    for search in golden["searches"]:
        embedder.failing = search.get("embedding_fails", False)
        handle = resolve_agent_knowledge_access(search["agent"], config, runtime_paths).knowledge
        assert handle is not None
        output = await _search_tool_output(handle, search["query"])
        found = (
            {"documents": distances_as_recorded(_normalized_search_documents(output), search["documents"])}
            if "documents" in search
            else {"text": output}
        )
        authored = {key: search[key] for key in ("agent", "embedding_fails", "query") if key in search}
        searches.append({**authored, **found})
    embedder.failing = False
    return sorted_keys({"searches": searches})
