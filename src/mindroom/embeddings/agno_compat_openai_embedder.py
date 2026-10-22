"""Agno OpenAI embedder request overrides with explicit owner policy hooks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.utils.log import log_info
from openai import AsyncOpenAI

from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT

if TYPE_CHECKING:
    from openai.types.create_embedding_response import CreateEmbeddingResponse

# AGNO_COMPAT: Embedding paths lack shared request and validation hooks.
# Reason: Agno inlines request building in separate sync/async/batch paths and
# exposes no common response-validation/error hook. Owner policy requires one
# request builder, sanitized failures, and complete non-empty embedding batches.
# Upstream issue: No matching public embedder request/validation hook identified.
# Upstream PR: https://github.com/agno-agi/agno/pull/9814 already adds typed provider
# failures in the installed SDK, but retains batch fallback and inlined requests.
# Remove when: Public hooks cover every request/validation path; retain owner
# dimensions/input rules, health reporting, redaction, and batch failure policy.
# Coverage: tests/test_openai_embedder.py; tests/test_embeddings.py.


class OpenAIEmbedderWithHooks(OpenAIEmbedder, ABC):
    """Copied Agno invocation paths delegated to the owner's request/outcome policy."""

    @abstractmethod
    def embedding_request_parameters(self, input_value: str | list[str], *, query: bool) -> dict[str, Any]:
        """Build one embedding request under the owner's provider policy, for search queries or documents."""
        ...

    @abstractmethod
    def embedding_request_error(self, error: Exception) -> Exception:
        """Classify a failed request for the owner, without exposing provider detail."""
        ...

    @abstractmethod
    def validate_embedding_response(
        self,
        response: CreateEmbeddingResponse,
        expected_count: int,
    ) -> list[list[float]]:
        """Validate successful responses and record owner health."""
        ...

    # AGNO_COMPAT: The embedder has no async-only HTTP client setting.
    # Reason: Agno hands `client_params` to both its sync and async SDK clients, so an async `http_client` there
    # breaks the sync one, and without one the async SDK client builds a pool whose finalizer can hang reused sockets.
    # Upstream issue: None identified (searched October 19, 2026).
    # Upstream PR: None identified.
    # Remove when: Agno embedders accept an async HTTP client separately from the sync one.
    # Coverage: tests/test_model_http_client.py::test_openai_embedder_reuses_the_model_connection.
    @property
    def aclient(self) -> AsyncOpenAI:
        """Build the async SDK client on the runtime's shared model HTTP client."""
        if self.async_client is None:
            self.async_client = AsyncOpenAI(
                api_key=self.api_key,
                organization=self.organization,
                base_url=self.base_url,
                http_client=MODEL_HTTP_CLIENT,
                **(self.client_params or {}),
            )
        return self.async_client

    # AGNO_COMPAT: Embedders cannot tell a search query from a document.
    # Reason: Agno's Embedder has one embedding entry point for both, and models such as EmbeddingGemma 2 expect
    # different query and document prompts. Like the local sentence-transformers embedder, the purpose follows from
    # the method: Agno's vector stores and MindRoom's read path embed queries with ``get_embedding``, while
    # ``Document.embed``, MindRoom's prefetch and batch paths embed documents.
    # Upstream issue: tracking gap; searching agno-agi/agno issues and PRs found no query/document input type.
    # Upstream PR: none identified.
    # Remove when: Agno's Embedder exposes separate query and document embedding and its vector stores call them.
    # Coverage: tests/test_openai_embedder.py::test_prefixes_mark_sync_queries_and_documents,
    # tests/test_openai_embedder.py::test_prefixes_mark_async_queries_and_documents.
    def response(self, text: str, *, query: bool = False) -> CreateEmbeddingResponse:
        """Request a single embedding synchronously."""
        return self.client.embeddings.create(**self.embedding_request_parameters(text, query=query))

    def get_embedding(self, text: str) -> list[float]:
        """Request one query embedding; raise a classified error on failure."""
        try:
            response = self.response(text, query=True)
        except Exception as exc:
            raise self.embedding_request_error(exc) from None
        return self.validate_embedding_response(response, 1)[0]

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Request one document embedding and its usage payload; raise a classified error on failure."""
        try:
            response = self.response(text)
        except Exception as exc:
            raise self.embedding_request_error(exc) from None
        embedding = self.validate_embedding_response(response, 1)[0]
        usage = response.usage
        return embedding, usage.model_dump() if usage else None

    async def async_get_embedding(self, text: str) -> list[float]:
        """Request a single query embedding asynchronously; raise a classified error on failure."""
        try:
            response: CreateEmbeddingResponse = await self.aclient.embeddings.create(
                **self.embedding_request_parameters(text, query=True),
            )
        except Exception as exc:
            raise self.embedding_request_error(exc) from None
        return self.validate_embedding_response(response, 1)[0]

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Request one document embedding and its usage payload asynchronously; raise a classified error on failure."""
        try:
            response = await self.aclient.embeddings.create(**self.embedding_request_parameters(text, query=False))
        except Exception as exc:
            raise self.embedding_request_error(exc) from None
        embedding = self.validate_embedding_response(response, 1)[0]
        usage = response.usage
        return embedding, usage.model_dump() if usage else None

    async def async_get_embeddings_batch_and_usage(
        self,
        texts: list[str],
        *,
        query: bool = False,
    ) -> tuple[list[list[float]], list[dict[str, Any] | None]]:
        """Request document embeddings (or query embeddings) for a batch of texts; raise a classified error on failure.

        A failing batch fails the whole call instead of retrying per item:
        after a batch-wide auth failure every retry repeats the same rejected
        credential and obscures the root cause.
        """
        all_embeddings: list[list[float]] = []
        all_usage: list[dict[str, Any] | None] = []
        log_info(f"Getting embeddings and usage for {len(texts)} texts in batches of {self.batch_size} (async)")

        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i : i + self.batch_size]
            try:
                response: CreateEmbeddingResponse = await self.aclient.embeddings.create(
                    **self.embedding_request_parameters(batch_texts, query=query),
                )
            except Exception as exc:
                raise self.embedding_request_error(exc) from None
            batch_embeddings = self.validate_embedding_response(response, len(batch_texts))
            all_embeddings.extend(batch_embeddings)
            usage_dict = response.usage.model_dump() if response.usage else None
            all_usage.extend([usage_dict] * len(batch_embeddings))

        return all_embeddings, all_usage
