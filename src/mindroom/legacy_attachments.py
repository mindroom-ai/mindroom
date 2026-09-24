"""Reopen files named by attachment records written before retained media copies."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_regular_file_within_root

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# LEGACY_COMPAT: Attachment records that reference the registered caller file in place.
# Legacy format: `attachments/<id>.json` whose `local_path` is the resolved workspace or absolute source path, selected when its parent is not the current `incoming_media/` directory.
# Last legacy release: v2026.9.272 registered local files in place; replacement: the next release retains an opaque copy in `incoming_media/` and records only that copy.
# Handling: The first load opens the recorded canonical path through a no-follow walk from the filesystem root, copies it under the retained-media cap only when its bytes match the recorded SHA-256, and rewrites the record to the copy; unverifiable records are logged and rejected until retention cleanup removes them.
# Coverage: tests/test_attachments.py::test_load_attachment_adopts_verified_legacy_record, tests/test_attachments.py::test_load_attachment_rejects_legacy_record_that_cannot_be_verified, tests/test_attachments.py::test_load_attachment_rejects_records_outside_retained_media.


def legacy_attachment_source(raw_payload: Mapping[str, object]) -> tuple[Path, str] | None:
    """Return a legacy record's canonical source path and recorded digest, when both are usable."""
    local_path = raw_payload.get("local_path")
    content_sha256 = raw_payload.get("content_sha256")
    if not isinstance(local_path, str) or not isinstance(content_sha256, str) or not content_sha256:
        return None
    source_path = Path(local_path)
    if not source_path.is_absolute():
        return None
    return source_path, content_sha256


@contextmanager
def open_legacy_attachment_source(source_path: Path) -> Iterator[int]:
    """Open a legacy source without following a link anywhere on its recorded path.

    Legacy writers stored ``Path.resolve()`` output, so a link on this path was
    planted after registration and must not redirect adoption.
    """
    with open_regular_file_within_root(Path(source_path.anchor), source_path.relative_to(source_path.anchor)) as fd:
        yield fd
