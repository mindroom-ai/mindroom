"""Published knowledge index handle with parent embeddings and isolated native reads."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.knowledge.collection_lifetime import read_collection
from mindroom.knowledge.read_process import read_chroma_async
from mindroom.knowledge.read_protocol import ReadRequest

if TYPE_CHECKING:
    from mindroom.embeddings.base import Embedder
    from mindroom.knowledge.indexing_config import IndexingSettings
    from mindroom.knowledge.read_protocol import SearchHit


def collection_exists(path: str, collection: str) -> bool:
    """Read fresh publication metadata without booting the native search engine.

    This is Chroma's persisted sysdb schema (tenants/databases migration 4),
    not its vector index. Keep reads scoped to the default tenant/database used
    by our published handles. Call off-loop, like native reads: rollback-journal
    writers can temporarily block readers while refreshing the index. Let SQLite
    retry those locks for up to five seconds, without treating a busy or corrupt
    database as a missing collection.
    """
    database_path = Path(path).resolve() / "chroma.sqlite3"
    if not database_path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=5.0)) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM collections AS c JOIN databases AS d ON c.database_id = d.id "
                    "WHERE c.name = ? AND d.name = 'default_database' AND d.tenant_id = 'default_tenant' LIMIT 1",
                    (collection,),
                ).fetchone()
                is not None
            )
    except sqlite3.Error as exc:
        message = "Knowledge collection metadata unavailable"
        raise RuntimeError(message) from exc


@dataclass
class ChromaReadProxy:
    """A scoped read descriptor; the worker selects and holds its current publication."""

    collection_name: str
    path: str
    embedder: Embedder
    published_settings: IndexingSettings | None = None

    def _request(self) -> ReadRequest:
        """Return the request that selects this handle's compatible publication."""
        return ReadRequest(
            path=self.path,
            collection=self.collection_name,
            published_settings=self.published_settings.to_metadata() if self.published_settings is not None else None,
        )

    def exists(self) -> bool:
        """Probe the same compatible publication that a subsequent search resolves."""
        with read_collection(self._request()) as collection_name:
            return collection_exists(self.path, collection_name)

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        """Embed under the caller's credentials, then query outside this process, which starts while the embedding runs."""

        async def prepare_request() -> ReadRequest:
            embedding = (await self.embedder.aembed([query], purpose="query"))[0]
            return replace(self._request(), query=query, embedding=embedding, limit=limit)

        return (await read_chroma_async(prepare_request)).hits
