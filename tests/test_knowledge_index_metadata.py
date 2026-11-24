"""The two knowledge state documents: the publication and the refresh status."""

from __future__ import annotations

import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from mindroom.knowledge.index_metadata import (
    Publication,
    RefreshStatus,
    load_publication,
    load_refresh_status,
    publication_path,
    save_publication,
    update_refresh_status,
    write_json_atomic,
)
from mindroom.knowledge.indexing_config import IndexingSettings


def _settings(**overrides: str) -> IndexingSettings:
    settings = IndexingSettings(
        base_id="docs",
        storage_root="/storage",
        knowledge_path="/knowledge/docs",
        mode="semantic",
        embedder_provider="openai",
        embedder_model="text-embedding-3-small",
        embedder_host="",
        embedder_dimensions="1536",
        chunk_size="1000",
        chunk_overlap="100",
        repo_identity="",
        git_branch="",
        git_lfs="",
        git_skip_hidden="",
        git_include_patterns="",
        git_exclude_patterns="",
        include_patterns="",
        exclude_patterns="",
        include_extensions=".md",
        exclude_extensions="",
    )
    # Empty filter keys normalize on the way in, so parse once to get settings
    # that are already in the shape the codec round-trips unchanged.
    normalized = IndexingSettings.from_metadata(dataclasses.replace(settings, **overrides).to_metadata())
    assert normalized is not None
    return normalized


def _publication(**changes: object) -> Publication:
    publication = Publication(
        settings=_settings(),
        collection="published_collection",
        indexed_count=7,
        source_signature="source-signature",
        published_revision="deadbeef",
        last_published_at="2026-01-02T03:04:05+00:00",
        retired="retired_collection",
    )
    return dataclasses.replace(publication, **changes)


def _publication_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "settings": _settings().to_metadata(),
        "collection": "published_collection",
        "indexed_count": 7,
        "source_signature": "source-signature",
        "published_revision": "deadbeef",
        "last_published_at": "2026-01-02T03:04:05+00:00",
        "retired": "retired_collection",
    }
    payload.update(changes)
    return payload


def _full_status() -> RefreshStatus:
    return RefreshStatus(
        reason="filesystem_watch",
        reason_generation=4,
        last_error="boom",
        consecutive_refresh_failures=3,
        last_refresh_at="2026-01-02T03:06:07+00:00",
    )


def _status_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = dataclasses.asdict(_full_status())
    payload.update(changes)
    return payload


def _status_path(storage: Path) -> Path:
    return storage / "refresh_status.json"


def test_every_publication_field_round_trips(tmp_path: Path) -> None:
    """One writer and one loader carry the whole publication, field for field."""
    publication = _publication()

    save_publication(tmp_path, publication)

    assert publication_path(tmp_path) == tmp_path / "publication.json"
    assert load_publication(tmp_path) == publication
    payload = json.loads(publication_path(tmp_path).read_text(encoding="utf-8"))
    # Every field of the type is on disk, so no writer can drop one by omission.
    assert set(payload) == {field.name for field in dataclasses.fields(publication)}


def test_a_publication_without_a_revision_or_retired_collection_round_trips(tmp_path: Path) -> None:
    """A local base has no revision, and a forced rebuild keeps no retired collection."""
    publication = _publication(published_revision=None, retired=None)

    save_publication(tmp_path, publication)

    assert load_publication(tmp_path) == publication


def test_a_files_mode_publication_has_no_collection(tmp_path: Path) -> None:
    """File-mode bases publish source metadata and never name a collection, so they serve no vector search."""
    publication = _publication(settings=_settings(mode="files"), collection=None, retired=None)

    save_publication(tmp_path, publication)
    loaded = load_publication(tmp_path)

    assert loaded == publication
    assert not publication.serves(_settings(mode="files"))


def test_a_semantic_publication_without_a_collection_reads_as_none(tmp_path: Path) -> None:
    """A semantic publication that names no collection proves nothing a search could read."""
    write_json_atomic(publication_path(tmp_path), _publication_payload(collection=None))

    assert load_publication(tmp_path) is None


@pytest.mark.parametrize(
    "missing_field",
    ["settings", "collection", "indexed_count", "source_signature", "last_published_at"],
)
def test_a_publication_missing_a_required_field_reads_as_none(tmp_path: Path, missing_field: str) -> None:
    """A publication nothing can check is corrupt, not merely thin."""
    payload = _publication_payload()
    del payload[missing_field]
    write_json_atomic(publication_path(tmp_path), payload)

    assert load_publication(tmp_path) is None


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"base": "storage"}, id="settings_unrecognized"),
        pytest.param({**_settings().to_metadata(), "mode": "graph"}, id="mode_unknown"),
        pytest.param({**_settings().to_metadata(), "base_id": 1}, id="settings_not_strings"),
        pytest.param(None, id="settings_null"),
        pytest.param(["docs"], id="settings_not_an_object"),
    ],
)
def test_a_publication_without_usable_settings_reads_as_none(tmp_path: Path, settings: object) -> None:
    """Settings identify what the index was built under; without them nothing is known."""
    write_json_atomic(publication_path(tmp_path), _publication_payload(settings=settings))

    assert load_publication(tmp_path) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collection", ""),
        ("collection", 3),
        ("collection", ["published_collection"]),
        ("source_signature", ""),
        ("source_signature", 5),
        ("published_revision", ""),
        ("published_revision", 5),
        ("last_published_at", 5),
        ("last_published_at", None),
        ("retired", ""),
        ("retired", {"collection": "retired_collection"}),
    ],
)
def test_a_publication_with_a_malformed_field_reads_as_none(tmp_path: Path, field: str, value: object) -> None:
    """A field of the wrong type or empty where a name is required makes the whole publication unreadable."""
    write_json_atomic(publication_path(tmp_path), _publication_payload(**{field: value}))

    assert load_publication(tmp_path) is None


@pytest.mark.parametrize(
    ("raw", "accepted"),
    [
        pytest.param(7, True, id="int"),
        pytest.param(0, True, id="zero"),
        pytest.param(7.0, False, id="whole_float"),
        pytest.param("7", False, id="ascii_digits"),
        pytest.param("١٢", False, id="arabic_indic_digits"),
        pytest.param("1" * 5000, False, id="past_the_integer_conversion_limit"),
        pytest.param(-1, False, id="negative"),
        pytest.param(True, False, id="bool"),
        pytest.param(7.5, False, id="fractional"),
        pytest.param(None, False, id="null"),
    ],
)
def test_counts_accept_only_non_negative_integers(tmp_path: Path, raw: object, *, accepted: bool) -> None:
    """A count that is not a JSON integer of zero or more is refused without raising out of either loader.

    A refused publication count makes the publication unreadable; a refused status count reads as zero and keeps the
    rest of the status.
    """
    write_json_atomic(publication_path(tmp_path), _publication_payload(indexed_count=raw))
    write_json_atomic(
        _status_path(tmp_path),
        _status_payload(reason_generation=raw, consecutive_refresh_failures=raw),
    )

    publication = load_publication(tmp_path)
    status = load_refresh_status(tmp_path)

    expected = raw if accepted else 0
    assert publication == (_publication(indexed_count=raw) if accepted else None)
    assert status == dataclasses.replace(
        _full_status(),
        reason_generation=expected,
        consecutive_refresh_failures=expected,
    )


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param(b"{ truncated", id="torn"),
        pytest.param(b'{"reason": "\xff\xfe"}', id="not_utf8"),
        pytest.param(b'["publication"]', id="not_an_object"),
        pytest.param(b"null", id="null"),
    ],
)
def test_a_missing_or_unparsable_state_file_reads_as_nothing_known(tmp_path: Path, content: bytes | None) -> None:
    """Neither a missing file nor a partially written or foreign one may raise.

    Undecodable bytes raise ``UnicodeDecodeError``, which is not an ``OSError``.
    """
    if content is not None:
        publication_path(tmp_path).write_bytes(content)
        _status_path(tmp_path).write_bytes(content)

    assert load_publication(tmp_path) is None
    assert load_refresh_status(tmp_path) == RefreshStatus()


def test_every_refresh_status_field_round_trips(tmp_path: Path) -> None:
    """One writer and one loader carry the whole refresh status, field for field, and the writer returns what it wrote."""
    status = _full_status()

    assert update_refresh_status(tmp_path, lambda _current: status) == status

    assert load_refresh_status(tmp_path) == status
    payload = json.loads(_status_path(tmp_path).read_text(encoding="utf-8"))
    assert set(payload) == {field.name for field in dataclasses.fields(status)}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", ""),
        ("reason", 3),
        ("reason", ["filesystem_watch"]),
        ("reason", {"reason": "filesystem_watch"}),
        ("last_error", ""),
        ("last_error", ["boom"]),
        ("last_refresh_at", ""),
        ("last_refresh_at", 3),
        ("reason_generation", "4"),
        ("consecutive_refresh_failures", 1.5),
    ],
)
def test_a_malformed_refresh_status_field_reads_as_its_default(tmp_path: Path, field: str, value: object) -> None:
    """One field outside the schema reads as unknown and never invalidates the rest of the status.

    A JSON array or object decodes to an unhashable value, so a field must be refused rather than raising out of a
    loader every caller treats as total.
    """
    write_json_atomic(_status_path(tmp_path), _status_payload(**{field: value}))

    default = {status_field.name: status_field.default for status_field in dataclasses.fields(RefreshStatus)}[field]
    assert load_refresh_status(tmp_path) == dataclasses.replace(_full_status(), **{field: default})


def test_refresh_status_updates_never_lose_a_concurrent_update(tmp_path: Path) -> None:
    """Each update is a load-modify-save under the base's status lock, so concurrent writers never overwrite each other."""
    updates_per_writer = 100
    barrier = Barrier(2)

    def increment(status: RefreshStatus) -> RefreshStatus:
        # Hand the GIL to the other writer between its read and its write, where an unlocked update would lose one.
        time.sleep(0)
        return dataclasses.replace(
            status,
            reason_generation=status.reason_generation + 1,
            consecutive_refresh_failures=status.consecutive_refresh_failures + 1,
        )

    def write_many() -> None:
        barrier.wait(timeout=10)
        for _update in range(updates_per_writer):
            update_refresh_status(tmp_path, increment)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writers = [pool.submit(write_many) for _writer in range(2)]
        for writer in writers:
            writer.result(timeout=60)

    status = load_refresh_status(tmp_path)
    assert status.reason_generation == 2 * updates_per_writer
    assert status.consecutive_refresh_failures == 2 * updates_per_writer


def test_write_json_atomic_uses_unique_temp_and_cleans_failed_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic writes use unique hidden temp files, clean them up on failure, and keep the previous document."""
    save_publication(tmp_path, _publication())
    attempted_temp_paths: list[Path] = []
    original_replace = Path.replace

    def _fail_temp_replace(self: Path, target: Path) -> Path:
        if self.parent == tmp_path and self.name.startswith(".publication.json.") and self.name.endswith(".tmp"):
            attempted_temp_paths.append(self)
            msg = "replace failed"
            raise OSError(msg)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fail_temp_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_publication(tmp_path, _publication(collection="replacement"))

    assert attempted_temp_paths
    assert attempted_temp_paths[0].name != "publication.json.tmp"
    assert not attempted_temp_paths[0].exists()
    assert load_publication(tmp_path) == _publication()
