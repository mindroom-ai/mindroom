"""Cross-process exclusion between collection readers and collection deletion."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.file_locks import advisory_file_lock
from mindroom.knowledge.index_metadata import load_published_index_state
from mindroom.knowledge.indexing_config import IndexingSettings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@contextmanager
def collection_lifetime_lock(storage_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Keep selecting and reading a collection atomic against its deletion.

    Readers take shared locks after embedding, for the length of one read.
    Writers take exclusive locks only while deleting, never while building
    candidates. Process exit releases either claim.
    """
    with advisory_file_lock(storage_path / "collection_lifetime.lock", exclusive=exclusive):
        yield


@contextmanager
def read_collection(path: str, collection: str, published_settings: Mapping[str, str] | None) -> Iterator[str]:
    """Resolve the collection at ``path`` a reader of ``published_settings`` may read, held until the reader closes.

    Without settings that is ``collection`` itself; with them it is the current publication compatible with them.
    """
    with collection_lifetime_lock(Path(path), exclusive=False):
        if published_settings is None:
            yield collection
            return
        expected = IndexingSettings.from_metadata(published_settings)
        state = load_published_index_state(Path(path) / "indexing_settings.json")
        if expected is None or state is None or not state.queryable_for(expected):
            message = "Published knowledge index is unavailable or incompatible with this reader"
            raise ValueError(message)
        assert state.collection is not None
        yield state.collection
