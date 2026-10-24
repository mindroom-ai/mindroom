"""Local sentence-transformers embedder, which installs and loads its optional model runtime on first use."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from mindroom.embeddings.errors import EmbedderRequestError, describe_embedder_error
from mindroom.embeddings.signatures import ensure_sentence_transformers_dependencies
from mindroom.model_defaults import SENTENCE_TRANSFORMERS_DEFAULT
from mindroom.tool_system.dependencies import optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np

    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import EmbeddingPurpose


class _SentenceTransformerModel(Protocol):
    """The two encode calls MindRoom makes on a loaded ``SentenceTransformer``."""

    def encode_query(self, text: str, *, normalize_embeddings: bool) -> np.ndarray:
        """Embed a search query with the model's query prompt."""
        ...

    def encode_document(self, text: str, *, normalize_embeddings: bool) -> np.ndarray:
        """Embed a document with the model's document prompt."""
        ...


#: One torch model per process is shared by every indexing thread, and torch's
#: Metal shader-library caches (``libMap``, ``cplMap``, ``kernelCache`` in
#: ``at::native::mps::MetalShaderLibrary``) are plain ``std::unordered_map``
#: instances with no lock. Torch drops the GIL inside those calls, so two
#: threads embedding at once can corrupt a bucket chain: the process then
#: either spins forever in a cache lookup or dies with SIGSEGV. Local
#: embedding runs one call at a time. Nothing is lost by serializing, because
#: the work already funnels through a single GPU queue.
_LOCAL_EMBEDDING_LOCK = threading.RLock()
#: The model loaded last, shared by every embedder for that model; a new model replaces it.
_SENTENCE_TRANSFORMER_CLIENT: tuple[str, _SentenceTransformerModel] | None = None


def _load_sentence_transformer(model: str) -> _SentenceTransformerModel:
    """Load one model's weights; the heavy import waits until a local embedder is first built."""
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    # Its encode calls also accept batches and return tensors on request; MindRoom passes one text and gets an array.
    return cast("_SentenceTransformerModel", SentenceTransformer(model_name_or_path=model))


@dataclass
class _SentenceTransformersEmbedder:
    """Embed one text per call with the model's own query or document prompt, one call at a time per process."""

    client: _SentenceTransformerModel = field(repr=False)

    @property
    def sends_batches(self) -> bool:
        """Return false: each model call embeds one text, as the vectors in existing indexes were embedded."""
        return False

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text; raise a classified error when the model fails."""
        encode = self.client.encode_query if purpose == "query" else self.client.encode_document
        vectors: list[list[float]] = []
        for text in texts:
            with _LOCAL_EMBEDDING_LOCK:
                try:
                    embedding = encode(text, normalize_embeddings=False)
                except Exception as exc:
                    raise EmbedderRequestError(describe_embedder_error(exc)) from exc
            vectors.append([float(value) for value in embedding.tolist()])
        return vectors

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Embed off the event loop, because local models compute in-process."""
        return await asyncio.to_thread(self.embed, texts, purpose=purpose)


def create_sentence_transformers_embedder(
    runtime_paths: RuntimePaths,
    model: str = SENTENCE_TRANSFORMERS_DEFAULT,
) -> _SentenceTransformersEmbedder:
    """Create a local sentence-transformers embedder after ensuring its optional extra exists."""
    ensure_sentence_transformers_dependencies(runtime_paths)
    global _SENTENCE_TRANSFORMER_CLIENT
    with optional_dependency_environment(), _LOCAL_EMBEDDING_LOCK:
        if _SENTENCE_TRANSFORMER_CLIENT is None or _SENTENCE_TRANSFORMER_CLIENT[0] != model:
            _SENTENCE_TRANSFORMER_CLIENT = (model, _load_sentence_transformer(model))
        return _SentenceTransformersEmbedder(client=_SENTENCE_TRANSFORMER_CLIENT[1])
