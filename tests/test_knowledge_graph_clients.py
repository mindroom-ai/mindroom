"""Graphiti's model, embedder, and reranker run on MindRoom's own model and embedder construction."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from graphiti_core.prompts.models import Message as GraphitiMessage
from pydantic import BaseModel

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.knowledge_graph import clients
from mindroom.knowledge_graph.clients import MindRoomEmbedder, MindRoomLLMClient, UnusedCrossEncoder
from mindroom.knowledge_graph.store import UnusableExtractionError
from mindroom.knowledge_graph.usage import UsageTarget, usage_target
from mindroom.llm.errors import ProviderError
from mindroom.providers import registry as provider_registry
from tests.conftest import install_model_providers
from tests.fakes.scripted_model import Await, ScriptedModel, answer

if TYPE_CHECKING:
    from pathlib import Path


class _Entities(BaseModel):
    names: list[str]


def _json_model(answer_text: str) -> ScriptedModel:
    """Return a model that answers one extraction request with ``answer_text``."""
    return ScriptedModel([answer(answer_text)])


def _sent_text(model: ScriptedModel) -> str:
    """Return every message of the model's only request as one text."""
    [request] = model.requests
    return "\n".join(message.get_content_string() for message in request.messages)


def _config() -> Config:
    return Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
    )


@pytest.mark.asyncio
async def test_llm_client_returns_structured_output_and_records_usage(tmp_path: Path) -> None:
    """An extraction call runs on the configured model and is charged to the conversation it came from."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    model = _json_model(json.dumps({"names": ["Alice", "Project Atlas"]}))
    client = MindRoomLLMClient(lambda: config, paths, asyncio.get_running_loop())
    record = AsyncMock()
    messages = [
        GraphitiMessage(role="system", content="Extract entities."),
        GraphitiMessage(role="user", content="Alice runs Atlas."),
    ]

    with (
        patch.object(provider_registry, "build_model_provider", return_value=model) as build,
        patch.object(clients, "record_helper_usage", record),
        usage_target(UsageTarget(agent_name="mind", session_id="!room:x:$thread", execution_identity=None)),
    ):
        result = await client._generate_response(messages, _Entities)

    assert result == {"names": ["Alice", "Project Atlas"]}
    assert build.call_args.args[2] == "luna"
    [request] = model.requests
    assert request.messages[0].role == "system"
    assert request.messages[0].get_content_string().startswith("Extract entities.")
    assert request.messages[1].get_content_string() == "Alice runs Atlas."
    assert record.await_args.kwargs["kind"] == "knowledge_graph"
    assert record.await_args.kwargs["owner"].session_id == "!room:x:$thread"


@pytest.mark.asyncio
async def test_llm_client_without_usage_target_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call outside ingestion, such as a search, has no conversation to charge."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    model = _json_model(json.dumps({"names": []}))
    record = AsyncMock()

    install_model_providers(monkeypatch, {"luna": model})
    with patch.object(clients, "record_helper_usage", record):
        await MindRoomLLMClient(lambda: config, paths, asyncio.get_running_loop())._generate_response(
            [GraphitiMessage(role="user", content="x")],
            _Entities,
        )

    record.assert_not_awaited()


async def _reject_key() -> None:
    msg = "401 invalid api key"
    raise ProviderError(msg, provider="OpenAI", status_code=401)


@pytest.mark.asyncio
async def test_llm_client_reports_the_providers_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected request surfaces the provider's reason, which ingestion shows in health."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    install_model_providers(monkeypatch, {"luna": ScriptedModel([[Await(_reject_key)]])})
    with pytest.raises(ProviderError, match="invalid api key"):
        await MindRoomLLMClient(lambda: config, paths, asyncio.get_running_loop())._generate_response(
            [GraphitiMessage(role="user", content="x")],
            _Entities,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_text", ["[1, 2]", "null", json.dumps({"other": 1})])
async def test_llm_client_rejects_output_of_the_wrong_shape(
    tmp_path: Path,
    answer_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output that is not the requested object counts as unusable, so ingestion can drop it after a few tries."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    install_model_providers(monkeypatch, {"luna": _json_model(answer_text)})
    with pytest.raises(ValueError, match="knowledge graph extraction"):
        await MindRoomLLMClient(lambda: config, paths, asyncio.get_running_loop())._generate_response(
            [GraphitiMessage(role="user", content="x")],
            _Entities,
        )


@pytest.mark.asyncio
async def test_extraction_runs_on_the_runtime_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Graphiti calls from its own loop, but the model runs on the runtime loop that owns the providers' HTTP clients."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    runtime_loop = asyncio.get_running_loop()
    loops: list[asyncio.AbstractEventLoop] = []

    async def record_loop() -> None:
        loops.append(asyncio.get_running_loop())

    model = ScriptedModel([[Await(record_loop), *answer(json.dumps({"names": ["Alice"]}))]])
    record = AsyncMock()
    target = UsageTarget(agent_name="mind", session_id="!room:x:$thread", execution_identity=None)
    client = MindRoomLLMClient(lambda: config, paths, runtime_loop)

    async def from_graph_loop() -> dict[str, object]:
        with usage_target(target):
            return await client._generate_response([GraphitiMessage(role="user", content="x")], _Entities)

    install_model_providers(monkeypatch, {"luna": model})
    with patch.object(clients, "record_helper_usage", record):
        result = await asyncio.to_thread(asyncio.run, from_graph_loop())

    assert result == {"names": ["Alice"]}
    assert loops == [runtime_loop]
    assert record.await_args.kwargs["owner"].session_id == "!room:x:$thread"


@pytest.mark.asyncio
async def test_unusable_extraction_crosses_back_to_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output the model got wrong reaches Graphiti's loop as the error ingestion counts toward dropping the episode."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    client = MindRoomLLMClient(lambda: config, paths, asyncio.get_running_loop())
    call = client._generate_response([GraphitiMessage(role="user", content="x")], _Entities)

    install_model_providers(monkeypatch, {"luna": _json_model("no")})
    with pytest.raises(UnusableExtractionError):
        await asyncio.to_thread(asyncio.run, call)


class _Embedder:
    sends_batches = False

    async def aembed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        assert purpose == "query"
        return [[float(len(text)), 1.0] for text in texts]


@pytest.mark.asyncio
async def test_embedder_embeds_the_single_text_graphiti_sends() -> None:
    """Graphiti sends one text as a one-element list."""
    embedder = MindRoomEmbedder(_Embedder())
    assert await embedder.create(["abc"]) == [3.0, 1.0]
    assert await embedder.create_batch(["a", "abcd"]) == [[1.0, 1.0], [4.0, 1.0]]


@pytest.mark.asyncio
async def test_embedder_sends_an_openai_batch_as_batch_requests() -> None:
    """Graphiti embeds every node name or fact of an episode at once; one request per text timed out connecting."""

    async def create(**kwargs: object) -> SimpleNamespace:
        texts = cast("list[str]", kwargs["input"])
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(text))]) for text in texts], usage=None)

    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=create)
    embedder = MindRoomEmbedder(
        OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test", async_client=async_client),
    )

    texts = ["x" * (index % 7 + 1) for index in range(150)]
    assert await embedder.create_batch(texts) == [[float(len(text))] for text in texts]
    assert async_client.embeddings.create.await_count == 2


@pytest.mark.asyncio
async def test_embedder_embeds_every_graph_text_as_a_query() -> None:
    """Graphiti embeds names, facts and searches through the same calls, so all of them get the query prefix."""

    async def create(**kwargs: object) -> SimpleNamespace:
        texts = kwargs["input"]
        count = len(texts) if isinstance(texts, list) else 1
        return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])] * count, usage=None)

    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=create)
    embedder = MindRoomEmbedder(
        OpenAIEmbedder(
            model="embeddinggemma-2",
            api_key="sk-test",
            base_url="http://local/v1",
            async_client=async_client,
            query_prefix="q: ",
            document_prefix="d: ",
        ),
    )

    await embedder.create(["Bas"])
    await embedder.create_batch(["Harry", "MindRoom"])

    inputs = [call.kwargs["input"] for call in async_client.embeddings.create.await_args_list]
    assert inputs == ["q: Bas", ["q: Harry", "q: MindRoom"]]


@pytest.mark.asyncio
async def test_embedder_embeds_one_text_per_request_when_a_server_answers_a_batch_with_one_vector() -> None:
    """Some OpenAI-compatible servers accept an array but return one vector; they still embed one text at a time."""

    async def create(**kwargs: object) -> SimpleNamespace:
        texts = kwargs["input"]
        first = texts[0] if isinstance(texts, list) else texts
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(cast("str", first)))])], usage=None)

    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=create)
    embedder = MindRoomEmbedder(
        OpenAIEmbedder(
            model="local-embed",
            api_key="sk-test",
            base_url="http://local/v1",
            async_client=async_client,
        ),
    )

    assert await embedder.create_batch(["a", "abc"]) == [[1.0], [3.0]]
    assert await embedder.create_batch(["ab", "abcd"]) == [[2.0], [4.0]]
    # The batch is tried once; later batches go straight to one text per request.
    assert async_client.embeddings.create.await_count == 1 + 2 + 2


@pytest.mark.asyncio
async def test_embedder_bounds_concurrent_requests_without_batch_support() -> None:
    """An embedder without batch requests embeds one text per request, a few at a time."""
    active = peak = 0

    class _Counting:
        sends_batches = False

        async def aembed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
            nonlocal active, peak
            del purpose
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return [[float(len(text))] for text in texts]

    texts = ["x" * (index % 5 + 1) for index in range(60)]
    assert await MindRoomEmbedder(_Counting()).create_batch(texts) == [[float(len(text))] for text in texts]
    assert peak == clients._EMBED_CONCURRENCY


@pytest.mark.asyncio
async def test_unused_cross_encoder_raises() -> None:
    """MindRoom's search recipes never rerank, so a call would be a bug."""
    with pytest.raises(RuntimeError, match="cross-encoder"):
        await UnusedCrossEncoder().rank("query", ["passage"])
