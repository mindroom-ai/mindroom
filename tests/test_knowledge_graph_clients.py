"""Graphiti's model, embedder, and reranker run on MindRoom's own model and embedder construction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from agno.models.message import Message
from agno.models.response import ModelResponse
from graphiti_core.prompts.models import Message as GraphitiMessage
from pydantic import BaseModel

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph import clients
from mindroom.knowledge_graph.clients import MindRoomEmbedder, MindRoomLLMClient, UnusedCrossEncoder
from mindroom.knowledge_graph.usage import UsageTarget, usage_target
from tests.conftest import FakeModel

if TYPE_CHECKING:
    from pathlib import Path


class _Entities(BaseModel):
    names: list[str]


@dataclass
class _JsonModel(FakeModel):
    """Answers every request with one fixed JSON document and records what it was sent."""

    answer: str = "{}"
    requests: list[str] = field(default_factory=list)

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        messages = cast("list[Message]", kwargs.get("messages") or args[0])
        self.requests.append("\n".join(str(message.content) for message in messages))
        return ModelResponse(content=self.answer)


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
    model = _JsonModel(id="luna", provider="test", answer=json.dumps({"names": ["Alice", "Project Atlas"]}))
    client = MindRoomLLMClient(lambda: config, paths)
    record = AsyncMock()
    messages = [
        GraphitiMessage(role="system", content="Extract entities."),
        GraphitiMessage(role="user", content="Alice runs Atlas."),
    ]

    with (
        patch("mindroom.providers.model_loading.get_model_instance", return_value=model) as get_model,
        patch.object(clients, "record_helper_usage", record),
        usage_target(UsageTarget(agent_name="mind", session_id="!room:x:$thread", execution_identity=None)),
    ):
        result = await client._generate_response(messages, _Entities)

    assert result == {"names": ["Alice", "Project Atlas"]}
    assert get_model.call_args.args[2] == "luna"
    assert "Alice runs Atlas." in model.requests[0]
    assert record.await_args.kwargs["kind"] == "knowledge_graph"
    assert record.await_args.kwargs["owner"].session_id == "!room:x:$thread"


@pytest.mark.asyncio
async def test_llm_client_without_usage_target_records_nothing(tmp_path: Path) -> None:
    """A call outside ingestion, such as a search, has no conversation to charge."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    model = _JsonModel(id="luna", provider="test", answer=json.dumps({"names": []}))
    record = AsyncMock()

    with (
        patch("mindroom.providers.model_loading.get_model_instance", return_value=model),
        patch.object(clients, "record_helper_usage", record),
    ):
        await MindRoomLLMClient(lambda: config, paths)._generate_response(
            [Message(role="user", content="x")],
            _Entities,
        )

    record.assert_not_awaited()


@dataclass
class _FailingModel(FakeModel):
    """Fails like a provider that rejects the key."""

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        del args, kwargs
        msg = "401 invalid api key"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_llm_client_reports_the_providers_error(tmp_path: Path) -> None:
    """A rejected request surfaces the provider's reason, which ingestion shows in health."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    with (
        patch("mindroom.providers.model_loading.get_model_instance", return_value=_FailingModel(id="luna")),
        pytest.raises(RuntimeError, match="invalid api key"),
    ):
        await MindRoomLLMClient(lambda: config, paths)._generate_response(
            [GraphitiMessage(role="user", content="x")],
            _Entities,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["[1, 2]", "null", json.dumps({"other": 1})])
async def test_llm_client_rejects_output_of_the_wrong_shape(tmp_path: Path, answer: str) -> None:
    """Output that is not the requested object counts as unusable, so ingestion can drop it after a few tries."""
    config = _config()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    with (
        patch("mindroom.providers.model_loading.get_model_instance", return_value=_JsonModel(id="luna", answer=answer)),
        pytest.raises(ValueError, match="knowledge graph extraction"),
    ):
        await MindRoomLLMClient(lambda: config, paths)._generate_response(
            [GraphitiMessage(role="user", content="x")],
            _Entities,
        )


class _Embedder:
    async def async_get_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


@pytest.mark.asyncio
async def test_embedder_embeds_the_single_text_graphiti_sends() -> None:
    """Graphiti sends one text as a one-element list."""
    embedder = MindRoomEmbedder(_Embedder())
    assert await embedder.create(["abc"]) == [3.0, 1.0]
    assert await embedder.create("ab") == [2.0, 1.0]
    assert await embedder.create_batch(["a", "abcd"]) == [[1.0, 1.0], [4.0, 1.0]]


@pytest.mark.asyncio
async def test_unused_cross_encoder_raises() -> None:
    """MindRoom's search recipes never rerank, so a call would be a bug."""
    with pytest.raises(RuntimeError, match="cross-encoder"):
        await UnusedCrossEncoder().rank("query", ["passage"])
