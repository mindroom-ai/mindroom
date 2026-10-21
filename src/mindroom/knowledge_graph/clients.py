"""Graphiti's model, embedder, reranker, and database clients; the model and embedder use MindRoom's own construction.

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
from agno.run.base import RunStatus
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from pydantic import BaseModel

from mindroom.embeddings.errors import EmbedderRequestError, embedder_batch_cardinality_mismatch
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.knowledge_graph.store import UnusableExtractionError, run_on_loop
from mindroom.knowledge_graph.usage import current_usage_target
from mindroom.providers import model_loading
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from graphiti_core.driver.driver import GraphDriver
    from graphiti_core.prompts.models import Message

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder

# Requests in flight at once for an embedder that embeds one text per request.
_EMBED_CONCURRENCY = 8

# Graphiti's FalkorDB searches return every entity's and fact's embedding and then discard it.
# Reason: the entity and fact return clauses select `properties(n)` and `properties(e)`, which include
# `name_embedding` and `fact_embedding`, and building the records pops them unused. With 3072-dimension embeddings
# the reply parsing took about 15 ms of event loop time per returned row, so ingestion stalled every agent's replies.
# Upstream issue: https://github.com/getzep/graphiti/issues/2021
# Upstream PR: https://github.com/getzep/graphiti/pull/2022 (https://github.com/getzep/graphiti/pull/1234 only adds
# an opt-in lightweight mode to `get_by_group_ids`).
# Remove when: the pinned Graphiti's FalkorDB searches no longer read embeddings back.
# Coverage: tests/test_knowledge_graph_store.py::test_lookups_do_not_read_embeddings_back
_WITHOUT_EMBEDDINGS = (
    ("properties(n)", "n {.*, name_embedding: NULL}"),
    ("properties(e)", "e {.*, fact_embedding: NULL}"),
)


class MindRoomLLMClient(LLMClient):
    """Run Graphiti's extraction prompts on the ``knowledge_graph.model`` entry with structured output."""

    def __init__(
        self,
        config_provider: Callable[[], Config],
        runtime_paths: RuntimePaths,
        runtime_loop: asyncio.AbstractEventLoop,
    ) -> None:
        # The model is read from config on every call; Graphiti uses its own copy only as a cache key, and caching is off.
        super().__init__(None)
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths
        self._runtime_loop = runtime_loop

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
        # Graphiti calls from the knowledge graph loop, but model providers share HTTP clients bound to the runtime loop
        # (the Codex and Kimi providers use Agno's global async client), so the extraction itself runs there.
        return await run_on_loop(self._runtime_loop, self._extract(messages, response_model))

    async def _extract(self, messages: list[Message], response_model: type[BaseModel] | None) -> dict[str, Any]:
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
        # Cleared once the server answers a batch with the wrong number of vectors.
        self._batches = embedder.sends_batches

    # Graphiti embeds node names, facts and search queries through the same two calls, so it cannot say which a text
    # is. Every graph text is embedded as a query, which keeps names, facts and searches comparable with each other.
    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        """Embed one text, which Graphiti passes as a one-element list."""
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            return (await self._embedder.aembed([input_data[0]], purpose="query"))[0]
        msg = "The knowledge graph embeds text only"
        raise TypeError(msg)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """Embed several texts, in batch requests where the embedder sends them.

        Graphiti passes every node name or fact of an episode at once, and one request per text then opened so many
        connections at the same moment that they timed out connecting.
        """
        if self._batches:
            try:
                embeddings = await self._embedder.aembed(input_data_list, purpose="query")
            except EmbedderRequestError as exc:
                # Some OpenAI-compatible servers accept an array but answer with one vector.
                if not embedder_batch_cardinality_mismatch(exc):
                    raise
                self._batches = False
            else:
                return embeddings
        slots = asyncio.Semaphore(_EMBED_CONCURRENCY)

        async def embed_one(text: str) -> list[float]:
            async with slots:
                return (await self._embedder.aembed([text], purpose="query"))[0]

        return list(await asyncio.gather(*(embed_one(text) for text in input_data_list)))


class UnusedCrossEncoder(CrossEncoderClient):
    """Stand in for Graphiti's default OpenAI reranker, which needs an OpenAI key; MindRoom's searches never rerank."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        """Fail loudly, because a call means a search recipe started reranking."""
        del query, passages
        msg = "The knowledge graph searches without a cross-encoder"
        raise RuntimeError(msg)


class MindRoomFalkorDriver(FalkorDriver):
    """Graphiti's FalkorDB driver, with the embeddings its searches discard replaced by null in each query."""

    async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """Run Graphiti's query without reading entity and fact embeddings back."""
        for selected, projected in _WITHOUT_EMBEDDINGS:
            cypher_query_ = cypher_query_.replace(selected, projected)
        return await super().execute_query(cypher_query_, **kwargs)

    def clone(self, database: str) -> GraphDriver:
        """Address another graph over the same connection; Graphiti's own clone would build a plain driver."""
        if database == self._database:
            return self
        if database == self.default_group_id:
            return MindRoomFalkorDriver(falkor_db=self.client)
        return MindRoomFalkorDriver(falkor_db=self.client, database=database)
