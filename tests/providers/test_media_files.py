"""The one name, bytes, and type rule every wire applies to an attachment file."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.media import File
from mindroom.providers.media_files import file_bytes, file_mime_type, file_name

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (File(filename="report.pdf", name="Report", filepath="/data/att_1.pdf"), "report.pdf"),
        (File(name="Report", filepath="/data/att_1.pdf"), "Report"),
        (File(filepath="/data/att_1.pdf"), "att_1.pdf"),
        (File(content=b"%PDF"), "document"),
    ],
)
def test_a_file_goes_by_its_filename_then_its_name_then_its_stored_file(file: File, expected: str) -> None:
    """The original filename comes first; a file with no name at all takes the caller's fallback."""
    assert file_name(file, "document") == expected


def test_a_file_s_bytes_come_from_its_local_file_or_its_content(tmp_path: Path) -> None:
    """Text content is sent as its UTF-8 bytes, and a local file is read."""
    local = tmp_path / "att_1.txt"
    local.write_bytes(b"from disk")

    assert file_bytes(File(content="Café"), provider="Test") == "Café".encode()
    assert file_bytes(File(content=b"raw"), provider="Test") == b"raw"
    assert file_bytes(File(filepath=str(local)), provider="Test") == b"from disk"


def test_a_filename_that_names_no_type_gives_way_to_the_stored_file() -> None:
    """A filename without an extension types nothing, so the stored file's name types the file."""
    assert file_mime_type(File(filename="report", filepath="/data/att_1.pdf")) == "application/pdf"
