"""Typed Chroma ownership with atomic shared-system acquisition and release."""

from __future__ import annotations

from threading import RLock

from agno.vectordb.chroma import ChromaDb as AgnoChromaDb
from chromadb.api.client import Client

_client_lifecycle_lock = RLock()


class ChromaDb(AgnoChromaDb):
    """Own one lazy client while coordinating Chroma's process-wide system cache.

    Chroma 1.5.8 does not atomically acquire a shared system or retire its last
    reference. Every knowledge client, including retained readers, uses this
    boundary so a final close cannot stop a concurrently acquired reader.
    Query operations remain outside the lifecycle lock.
    """

    @property
    def client(self) -> Client:
        """Acquire the concrete client under the shared lifecycle lock."""
        with _client_lifecycle_lock:
            client = super().client
            if not isinstance(client, Client):
                message = "Expected a concrete Chroma client"
                raise TypeError(message)
            return client

    def close(self) -> None:
        """Release this owner's client without creating one during cleanup."""
        with _client_lifecycle_lock:
            if self._client is not None:
                self.client.close()
