"""Graphiti's model, embedder, and reranker clients, backed by MindRoom's own model and embedder construction.

Every provider, credential rule, and base URL MindRoom supports then works for extraction without a second provider
mapping, and each extraction call is an Agno run whose tokens are recorded like other helper work.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.agent import Agent
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.run.base import RunStatus
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from pydantic import BaseModel

from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.knowledge_graph.store import UnusableExtractionError
from mindroom.knowledge_graph.usage import current_usage_target
from mindroom.providers import model_loading
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from agno.knowledge.embedder.base import Embedder
    from graphiti_core.prompts.models import Message

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# Requests in flight at once for an embedder that embeds one text per request.
_EMBED_CONCURRENCY = 8


class MindRoomLLMClient(LLMClient):
    """Run Graphiti's extraction prompts on the ``knowledge_graph.model`` entry with structured output."""

    def __init__(self, config_provider: Callable[[], Config], runtime_paths: RuntimePaths) -> None:
        super().__init__(LLMConfig(model=config_provider().knowledge_graph.model), cache=False)
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        # The configured model entry decides its output limit, and one model serves every prompt.
        del max_tokens, model_size, model
        config = self._config_provider()
        model_name = config.knowledge_graph.model
        if model_name is None:
            msg = "knowledge_graph.model is not set"
            raise ValueError(msg)
        target = current_usage_target()
        identity = target.execution_identity if target is not None else None
        agent = Agent(
            name="KnowledgeGraphExtractor",
            model=model_loading.get_model_instance(
                config,
                self._runtime_paths,
                model_name,
                execution_identity=identity,
            ),
            instructions="\n\n".join(message.content for message in messages if message.role == "system") or None,
            output_schema=response_model,
            telemetry=False,
        )
        invocation_id = uuid4().hex
        response = await agent.arun(
            "\n\n".join(message.content for message in messages if message.role != "system"),
            run_id=invocation_id,
        )
        if target is not None:
            await record_helper_usage(
                response,
                owner=HelperUsageOwner(
                    storage_factory=partial(
                        create_session_storage,
                        target.agent_name,
                        config,
                        self._runtime_paths,
                        execution_identity=identity,
                    ),
                    session_id=target.session_id,
                ),
                invocation_id=invocation_id,
                kind="knowledge_graph",
                requester_id=target.requester_id or (identity.requester_id if identity is not None else None),
            )
        content = response.content
        if response.status == RunStatus.error:
            # Agno returns a provider failure as the run's content; report it instead of a JSON parse error.
            msg = f"Knowledge graph extraction failed: {content}"
            raise RuntimeError(msg)
        if isinstance(content, BaseModel):
            return content.model_dump()
        # Output Graphiti cannot use counts toward dropping the episode; other errors wait like an outage.
        try:
            parsed = json.loads(content if isinstance(content, str) else "")
            if response_model is not None:
                return response_model.model_validate(parsed).model_dump()
        except ValueError as exc:
            msg = f"The knowledge graph extraction output is unusable: {exc}"
            raise UnusableExtractionError(msg) from exc
        if not isinstance(parsed, dict):
            msg = "The knowledge graph extraction output is not a JSON object"
            raise UnusableExtractionError(msg)
        return parsed


class MindRoomEmbedder(EmbedderClient):
    """Embed with the ``memory.embedder`` that MindRoom's other semantic search uses."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        """Embed one text; Graphiti passes it alone or as a one-element list."""
        if isinstance(input_data, str):
            return await self._embedder.async_get_embedding(input_data)
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            return await self._embedder.async_get_embedding(input_data[0])
        msg = "The knowledge graph embeds text only"
        raise TypeError(msg)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """Embed several texts, in batch requests where the embedder sends them.

        Graphiti passes every node name or fact of an episode at once, and one request per text then opened so many
        connections at the same moment that they timed out connecting.
        """
        if isinstance(self._embedder, OpenAIEmbedder):
            embeddings, _usage = await self._embedder.async_get_embeddings_batch_and_usage(input_data_list)
            return embeddings
        slots = asyncio.Semaphore(_EMBED_CONCURRENCY)

        async def embed(text: str) -> list[float]:
            async with slots:
                return await self._embedder.async_get_embedding(text)

        return list(await asyncio.gather(*(embed(text) for text in input_data_list)))


class UnusedCrossEncoder(CrossEncoderClient):
    """Stand in for Graphiti's default OpenAI reranker, which needs an OpenAI key; MindRoom's searches never rerank."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        """Fail loudly, because a call means a search recipe started reranking."""
        del query, passages
        msg = "The knowledge graph searches without a cross-encoder"
        raise RuntimeError(msg)
