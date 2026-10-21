"""The owned knowledge code reproduces the knowledge goldens captured from the Agno-backed path."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import chromadb
import httpx
import numpy as np
import ollama
import pytest
from openai import AsyncOpenAI, OpenAI

import mindroom.embeddings.sentence_transformers_embedder as local_embedder
from mindroom.config.main import Config
from mindroom.embeddings.factory import create_configured_embedder
from mindroom.embeddings.ollama_embedder import OllamaEmbedder
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.knowledge.read_proxy import ChromaReadProxy
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding
from mindroom.knowledge.registry import (
    load_published_index_state,
    published_index_metadata_path,
    published_index_storage_path,
    resolve_published_index_key,
)
from tests.agno_parity.knowledge_vectors import parity_vector
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from chromadb.api.models.Collection import Collection

    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder, EmbeddingPurpose

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "agno_parity" / "knowledge"
EMBEDDER_GOLDENS: dict[str, Any] = json.loads((GOLDEN_DIR / "embedders.json").read_text(encoding="utf-8"))


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def openai(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"method": request.method, "url": str(request.url), "body": json.loads(request.content)})
        inputs = json.loads(request.content)["input"]
        count = 1 if isinstance(inputs, str) else len(inputs)
        data = [{"object": "embedding", "index": index, "embedding": [0.5, 0.25]} for index in range(count)]
        return httpx.Response(200, json={"object": "list", "model": "parity", "data": data})

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
        transport: Callable[[httpx.Request], httpx.Response] = recorder.openai
        embedder.client = OpenAI(
            api_key=embedder.api_key,
            base_url=embedder.base_url,
            http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        )
        embedder.async_client = AsyncOpenAI(
            api_key=embedder.api_key,
            base_url=embedder.base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        )
    elif isinstance(embedder, OllamaEmbedder):
        embedder.client = ollama.Client(host=embedder.host, transport=httpx.MockTransport(recorder.ollama))
        embedder.async_client = ollama.AsyncClient(host=embedder.host, transport=httpx.MockTransport(recorder.ollama))


@pytest.mark.parametrize("case", sorted(EMBEDDER_GOLDENS))
def test_embedders_send_the_request_bodies_agno_sent(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every embedder call sends the same request body (or local model call) for the same purpose and texts."""
    golden = EMBEDDER_GOLDENS[case]
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate({"models": {}, "memory": {"embedder": golden["embedder"]}}),
        runtime_paths,
    )
    model = _RecordingModel()
    monkeypatch.setattr(local_embedder, "_SENTENCE_TRANSFORMER_CLIENT", None)
    monkeypatch.setattr(local_embedder, "ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", lambda _model: model)
    for call in golden["calls"]:
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
        assert len(vectors) == len(texts)
        if "model_calls" in call:
            assert model.calls == call["model_calls"], call["agno_method"]
        else:
            assert recorder.requests == call["requests"], call["agno_method"]


ROW_GOLDENS: dict[str, Any] = json.loads((GOLDEN_DIR / "rows.json").read_text(encoding="utf-8"))
SEARCH_GOLDENS: dict[str, Any] = json.loads((GOLDEN_DIR / "search.json").read_text(encoding="utf-8"))
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class _ParityEmbedder:
    """Owned-protocol embedder whose vectors are ``parity_vector`` of each text, as the goldens were embedded."""

    sends_batches = False

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        del purpose
        return [parity_vector(text) for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


def _with_approximate_distances(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare distances to float32 precision; Chroma's own arithmetic decides their last digits."""
    approximate: list[dict[str, Any]] = []
    for document in documents:
        meta_data = dict(document["meta_data"])
        for key in ("distances", "similarity_score"):
            meta_data[key] = pytest.approx(meta_data[key], rel=1e-6)
        approximate.append({**document, "meta_data": meta_data})
    return approximate


def _golden_config(root: Path) -> tuple[Config, RuntimePaths]:
    """Write the goldens' source files under ``root`` and bind their config there."""
    golden_input = ROW_GOLDENS["input"]
    mtime = golden_input["source_mtime_ns"]
    for directory, files in golden_input["files"].items():
        for relative_path, text in files.items():
            path = root / directory / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            os.utime(path, ns=(mtime, mtime))
    runtime_paths = test_runtime_paths(root)
    return bind_runtime_paths(Config.model_validate(golden_input["config"]), runtime_paths), runtime_paths


def _normalized_rows(collection: Collection) -> list[dict[str, Any]]:
    """Read every row, checking and normalizing the values that hash per-version identity."""
    rows = collection.get(include=["documents", "metadatas", "embeddings"])
    normalized: list[dict[str, Any]] = []
    for row_id, document, metadata, embedding in zip(
        rows["ids"],
        cast("list[str]", rows["documents"]),
        cast("list[dict[str, Any]]", rows["metadatas"]),
        cast("list[list[float]]", rows["embeddings"]),
        strict=True,
    ):
        assert _HEX32.fullmatch(row_id), row_id
        stored = dict(metadata)
        assert _HEX64.fullmatch(str(stored["content_hash"])), stored
        assert stored["content_id"] == str(uuid.uuid5(uuid.NAMESPACE_DNS, str(stored["content_hash"])))
        stored["content_hash"] = "<sha256>"
        stored["content_id"] = "<uuid5>"
        normalized.append(
            {
                "id": "<md5>",
                "document": document,
                "metadata": dict(sorted(stored.items())),
                "embedding": [float(value) for value in embedding],
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


async def _refresh_golden_bases(config: Config, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Refresh every golden base through the production refresh path and read back what it published."""
    published: dict[str, Any] = {}
    for base_id in ROW_GOLDENS["bases"]:
        await _refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths)
        key = resolve_published_index_key(base_id, config=config, runtime_paths=runtime_paths)
        state = load_published_index_state(published_index_metadata_path(key))
        assert state is not None
        assert state.status == "complete"
        assert state.collection is not None
        client = chromadb.PersistentClient(path=str(published_index_storage_path(key)))
        try:
            collection = client.get_collection(state.collection)
            published[base_id] = {
                "collection_metadata": collection.metadata,
                "collection_configuration": collection.configuration_json,
                "rows": _normalized_rows(collection),
            }
        finally:
            client.close()
    return published


@pytest.fixture
def parity_embedder(monkeypatch: pytest.MonkeyPatch) -> _ParityEmbedder:
    """Embed refreshes and searches with the goldens' deterministic vectors."""
    embedder = _ParityEmbedder()
    monkeypatch.setattr("mindroom.knowledge.manager.create_configured_embedder", lambda *_args, **_kwargs: embedder)
    monkeypatch.setattr("mindroom.knowledge.registry.create_configured_embedder", lambda *_args, **_kwargs: embedder)
    monkeypatch.setattr("mindroom.knowledge.registry._published_indexes", {})
    return embedder


@pytest.mark.asyncio
@pytest.mark.usefixtures("parity_embedder")
async def test_refreshes_write_the_rows_and_collection_settings_agno_wrote(tmp_path: Path) -> None:
    """Every chunk row keeps its document, metadata keys and values, and embedding; collections keep cosine space."""
    config, runtime_paths = _golden_config(tmp_path)

    assert await _refresh_golden_bases(config, runtime_paths) == ROW_GOLDENS["bases"]


def test_rows_agno_wrote_stay_searchable(tmp_path: Path, parity_embedder: _ParityEmbedder) -> None:
    """A collection Agno created and filled answers the owned read path exactly as Agno's search did."""
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
        documents = proxy.search(search["query"], limit=10)
        assert _with_approximate_distances([document.to_dict() for document in documents]) == search["documents"]
