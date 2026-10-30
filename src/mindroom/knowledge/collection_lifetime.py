"""Cross-process exclusion between collection readers and writers that delete or take over a collection."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from mindroom.file_locks import advisory_file_lock
from mindroom.knowledge.index_metadata import load_publication

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.knowledge.indexing_config import IndexingSettings


@contextmanager
def collection_lifetime_lock(storage_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Keep selecting and reading a collection atomic against its deletion or reuse.

    Readers take shared locks after embedding, for the length of one read.
    Writers take exclusive locks only while deleting a collection or taking a
    retired one over as their candidate, never while writing candidates.
    Process exit releases either claim.
    """
    with advisory_file_lock(storage_path / "collection_lifetime.lock", exclusive=exclusive):
        yield


@contextmanager
def read_collection(storage_path: Path, settings: IndexingSettings) -> Iterator[str]:
    """Resolve the published collection a reader of ``settings`` may read, held until the reader closes."""
    with collection_lifetime_lock(storage_path, exclusive=False):
        publication = load_publication(storage_path)
        if publication is None or publication.collection is None or not publication.serves(settings):
            message = "Published knowledge index is unavailable or incompatible with this reader"
            raise ValueError(message)
        yield publication.collection
