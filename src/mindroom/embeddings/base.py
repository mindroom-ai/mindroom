"""The embedder interface every MindRoom semantic index embeds through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Why a text is embedded; models such as EmbeddingGemma 2 embed queries and documents with different prompts.
EmbeddingPurpose = Literal["query", "document"]


class Embedder(Protocol):
    """Embed texts for one purpose.

    Implementations return exactly one non-empty vector per text, in input order, and raise
    ``EmbedderRequestError`` instead of returning an empty or partial result.
    """

    @property
    def sends_batches(self) -> bool:
        """Return whether one provider request can carry several texts."""
        ...

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text."""
        ...

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text without blocking the event loop."""
        ...
