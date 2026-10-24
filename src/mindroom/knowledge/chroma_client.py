"""Named Chroma collections whose persistent clients are acquired and released under one process-wide lock."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING

import chromadb
from chromadb.api.client import Client
from chromadb.errors import NotFoundError

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

_client_lifecycle_lock = RLock()


def _persistent_client(path: str) -> Client:
    """Open the concrete persistent client for one storage directory."""
    client = chromadb.PersistentClient(path=path)
    if not isinstance(client, Client):
        message = "Expected a concrete Chroma client"
        raise TypeError(message)
    return client


@dataclass(eq=False)
class KnowledgeCollection:
    """One named collection in a knowledge base's storage directory, whose client opens on first use.

    Chroma 1.5.8 does not atomically acquire a shared system or retire its last
    reference. Every knowledge client, including retained readers, opens and
    closes under one lock so a final close cannot stop a concurrently acquired
    reader. Query operations remain outside the lock.
    """

    path: str
    name: str
    _client: Client | None = field(default=None, init=False, repr=False)

    @property
    def client(self) -> Client:
        """Acquire the concrete client under the shared lifecycle lock."""
        with _client_lifecycle_lock:
            if self._client is None:
                self._client = _persistent_client(self.path)
            return self._client

    def collection(self) -> Collection:
        """Return the existing collection; raise ``NotFoundError`` when it is absent."""
        return self.client.get_collection(name=self.name)

    def exists(self) -> bool:
        """Return whether the collection exists, letting any other store failure propagate."""
        try:
            self.collection()
        except NotFoundError:
            return False
        return True

    def close(self) -> None:
        """Release this owner's client without creating one during cleanup."""
        with _client_lifecycle_lock:
            if self._client is not None:
                self._client.close()
                self._client = None
