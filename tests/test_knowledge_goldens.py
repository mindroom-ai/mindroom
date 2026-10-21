"""The owned knowledge code reproduces the knowledge goldens captured from the Agno-backed path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.embeddings.base import Embedder, EmbeddingPurpose

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "agno_parity" / "knowledge"
EMBEDDER_GOLDENS: dict[str, Any] = json.loads((GOLDEN_DIR / "embedders.json").read_text(encoding="utf-8"))
#: The width Agno's Ollama embedder accepted, so these canned replies pass any width check.
_OLLAMA_REPLY_WIDTH = 4096


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
        return httpx.Response(200, json={"model": "parity", "embeddings": [[0.5] * _OLLAMA_REPLY_WIDTH]})


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
