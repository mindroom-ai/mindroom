"""Knowledge published-index state codec tests."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from mindroom.knowledge.index_metadata import (
    PublishedIndexState,
    load_published_index_state,
    save_published_index_state,
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


def _full_state() -> PublishedIndexState:
    return PublishedIndexState(
        settings=_settings(),
        status="complete",
        collection="published_collection",
        last_published_at="2026-01-02T03:04:05+00:00",
        published_revision="deadbeef",
        indexed_count=7,
        source_signature="source-signature",
        refresh_job="running",
        reason="refreshing",
        last_error="boom",
        updated_at="2026-01-02T03:05:06+00:00",
        last_refresh_at="2026-01-02T03:06:07+00:00",
        consecutive_refresh_failures=3,
    )


def test_every_state_field_round_trips_through_the_single_writer(tmp_path: Path) -> None:
    """One writer and one loader must carry the whole schema, field for field."""
    metadata_path = tmp_path / "indexing_settings.json"
    state = _full_state()

    save_published_index_state(metadata_path, state)

    assert load_published_index_state(metadata_path) == state
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    # Every field of the type is on disk, so no writer can drop one by omission.
    assert set(payload) == {field.name for field in dataclasses.fields(state)}


def test_absent_optional_fields_are_omitted_rather_than_written_as_null(tmp_path: Path) -> None:
    """Fields the state does not carry stay off disk, and reload as their defaults."""
    metadata_path = tmp_path / "indexing_settings.json"
    state = PublishedIndexState(settings=_settings(), status="indexing")

    save_published_index_state(metadata_path, state)

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert set(payload) == {"settings", "status", "refresh_job", "consecutive_refresh_failures"}
    assert load_published_index_state(metadata_path) == state


def test_an_unfinished_record_keeps_the_collection_the_last_publication_left(tmp_path: Path) -> None:
    """A record that is not a publication still names the collection that is live.

    The collection name is the only on-disk proof of which candidate-prefixed
    collection candidate cleanup must spare, so a record that carries nothing
    else must still carry that. ``indexing`` with a collection is the shape
    older versions wrote for every in-progress refresh.
    """
    metadata_path = tmp_path / "indexing_settings.json"
    write_json_atomic(
        metadata_path,
        {"settings": _settings().to_metadata(), "status": "indexing", "collection": "published_collection"},
    )

    state = load_published_index_state(metadata_path)

    assert state is not None
    assert state.collection == "published_collection"
    assert state.indexed_count is None
    assert state.source_signature is None


@pytest.mark.parametrize(
    "missing_field",
    ["collection", "indexed_count", "source_signature"],
)
def test_a_complete_record_that_proves_nothing_is_no_state_at_all(tmp_path: Path, missing_field: str) -> None:
    """A publication nothing can check is corrupt, not merely thin."""
    metadata_path = tmp_path / "indexing_settings.json"
    payload: dict[str, object] = {
        "settings": _settings().to_metadata(),
        "status": "complete",
        "collection": "published_collection",
        "indexed_count": 7,
        "source_signature": "source-signature",
    }
    del payload[missing_field]
    write_json_atomic(metadata_path, payload)

    assert load_published_index_state(metadata_path) is None


def test_file_mode_metadata_keeps_its_mode_without_a_collection(tmp_path: Path) -> None:
    """File-mode bases publish source metadata and never name a collection."""
    metadata_path = tmp_path / "indexing_settings.json"
    state = PublishedIndexState(
        settings=_settings(mode="files"),
        status="complete",
        indexed_count=0,
        source_signature="source-signature",
    )

    save_published_index_state(metadata_path, state)
    loaded = load_published_index_state(metadata_path)

    assert loaded is not None
    assert loaded.settings.mode == "files"
    assert loaded.collection is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"status": "complete"}, id="settings_missing"),
        pytest.param({"settings": {"base": "storage"}, "status": "complete"}, id="settings_unrecognized"),
        pytest.param({"settings": {"base_id": 1}, "status": "complete"}, id="settings_not_strings"),
        pytest.param({"settings": None, "status": "complete"}, id="settings_not_an_object"),
    ],
)
def test_a_record_without_usable_settings_is_no_state_at_all(tmp_path: Path, payload: dict[str, object]) -> None:
    """Settings identify what the index was built under; without them nothing is known."""
    metadata_path = tmp_path / "indexing_settings.json"
    write_json_atomic(metadata_path, payload)

    assert load_published_index_state(metadata_path) is None


@pytest.mark.parametrize("status", [None, "", "publishing", 3, ["complete"], {"status": "complete"}])
def test_an_unknown_status_is_no_state_at_all(tmp_path: Path, status: object) -> None:
    """A status outside the schema leaves the rest of the record meaningless.

    A JSON array or object decodes to an unhashable value, so a record must be
    refused rather than raising out of a loader every caller treats as total.
    """
    metadata_path = tmp_path / "indexing_settings.json"
    write_json_atomic(metadata_path, {"settings": _settings().to_metadata(), "status": status})

    assert load_published_index_state(metadata_path) is None


@pytest.mark.parametrize("refresh_job", ["sprinting", 4, ["running"], {"job": "running"}])
def test_an_unknown_refresh_job_falls_back_to_idle(tmp_path: Path, refresh_job: object) -> None:
    """Refresh-job bookkeeping never invalidates a record that is otherwise usable."""
    metadata_path = tmp_path / "indexing_settings.json"
    write_json_atomic(
        metadata_path,
        {"settings": _settings().to_metadata(), "status": "indexing", "refresh_job": refresh_job},
    )

    state = load_published_index_state(metadata_path)

    assert state is not None
    assert state.refresh_job == "idle"


def test_a_torn_or_missing_file_is_no_state_at_all(tmp_path: Path) -> None:
    """Neither a missing file nor a partially written one may raise."""
    missing_path = tmp_path / "missing.json"
    torn_path = tmp_path / "torn.json"
    torn_path.write_text("{ truncated", encoding="utf-8")

    assert load_published_index_state(missing_path) is None
    assert load_published_index_state(torn_path) is None


def test_bytes_that_are_not_utf8_are_no_state_at_all(tmp_path: Path) -> None:
    """Undecodable bytes raise ``UnicodeDecodeError``, which is not an ``OSError``."""
    metadata_path = tmp_path / "indexing_settings.json"
    metadata_path.write_bytes(b'{"status": "\xff\xfe"}')

    assert load_published_index_state(metadata_path) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(7, 7, id="int"),
        pytest.param(7.0, 7, id="whole_float"),
        pytest.param("7", 7, id="ascii_digits"),
        pytest.param("١٢", 12, id="arabic_indic_digits"),
        pytest.param("²", None, id="superscript_int_refuses"),
        pytest.param("1" * 5000, None, id="past_the_integer_conversion_limit"),
        pytest.param(-1, None, id="negative"),
        pytest.param(True, None, id="bool"),
        pytest.param(7.5, None, id="fractional"),
    ],
)
def test_counts_accept_only_values_int_can_take(tmp_path: Path, raw: object, expected: int | None) -> None:
    """A number ``int`` refuses must read as absent, not raise out of the loader.

    Two independent ways to be refused, neither of them a property of the
    payload's shape: ``"²".isdigit()`` is true while ``int("²")`` raises, and a
    perfectly well-formed decimal string raises once it is longer than
    CPython's integer conversion limit. Either would turn one hostile field in
    a state file into a failed manager construction instead of a base that
    simply refreshes itself. Both counts are parsed by the same helper.
    """
    metadata_path = tmp_path / "indexing_settings.json"
    write_json_atomic(
        metadata_path,
        {
            "settings": _settings().to_metadata(),
            "status": "indexing",
            "indexed_count": raw,
            "consecutive_refresh_failures": raw,
        },
    )

    state = load_published_index_state(metadata_path)

    assert state is not None
    assert state.indexed_count == expected
    assert state.consecutive_refresh_failures == (expected or 0)


def test_write_json_atomic_uses_unique_temp_and_cleans_failed_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic writes use unique hidden temp files and clean them up on failure."""
    metadata_path = tmp_path / "indexing_settings.json"
    attempted_temp_paths: list[Path] = []
    original_replace = Path.replace

    def _fail_temp_replace(self: Path, target: Path) -> Path:
        if self.parent == tmp_path and self.name.startswith(".indexing_settings.json.") and self.name.endswith(".tmp"):
            attempted_temp_paths.append(self)
            msg = "replace failed"
            raise OSError(msg)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fail_temp_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_published_index_state(metadata_path, PublishedIndexState(settings=_settings(), status="complete"))

    assert attempted_temp_paths
    assert attempted_temp_paths[0].name != "indexing_settings.json.tmp"
    assert not attempted_temp_paths[0].exists()
