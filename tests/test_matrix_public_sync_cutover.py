"""Fail-closed ownership gates for the Task 6 public-sync restoration."""

from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).parents[1] / "src" / "mindroom"
_PUBLIC_SYNC_OWNERS = (
    _SOURCE_ROOT / "bot.py",
    _SOURCE_ROOT / "matrix" / "sync_loop.py",
    _SOURCE_ROOT / "matrix" / "journal_ingress.py",
    _SOURCE_ROOT / "matrix" / "client_session.py",
    _SOURCE_ROOT / "matrix" / "sync_certification.py",
    _SOURCE_ROOT / "matrix" / "sync_checkpoint_trust.py",
)
_FORK_ONLY_NIO_REFERENCES = (
    "nio.SlidingSyncResponse",
    ".add_event_admission_callback",
    ".acknowledge_classic_sync",
    ".reset_classic_sync_state",
    ".has_uncommitted_classic_sync_state",
    ".clear_persisted_sync_recovery",
    ".recovered_room_ids",
    ".unrecovered_room_ids",
)


@pytest.mark.parametrize("source_path", _PUBLIC_SYNC_OWNERS, ids=lambda path: path.name)
def test_mindroom_production_has_no_fork_only_public_sync_reference(
    source_path: Path,
) -> None:
    """MindRoom must release every public nio fork symbol before nio removes it."""
    source = source_path.read_text()
    references = tuple(reference for reference in _FORK_ONLY_NIO_REFERENCES if reference in source)

    assert references == ()
