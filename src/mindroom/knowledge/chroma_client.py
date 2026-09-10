"""Concrete client lifecycle at the Agno/Chroma type boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chromadb.api.client import Client

if TYPE_CHECKING:
    from chromadb.api import ClientAPI


def require_chroma_client(client: ClientAPI) -> Client:
    """Require the concrete client whose public close releases its shared reference."""
    if not isinstance(client, Client):
        message = "Expected a concrete Chroma client"
        raise TypeError(message)
    return client
