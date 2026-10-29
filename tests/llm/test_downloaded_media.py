"""The type a media item read from its URL or file keeps: one rule for the pipeline's downloads and worker media."""

from __future__ import annotations

import pytest

from mindroom.llm.media import Audio, File, Image, Video, named_by_resource, url_file_name


@pytest.mark.parametrize(
    ("media", "declared", "expected"),
    [
        (Audio(url="https://media.test/speech"), "audio/mpeg", ("audio/mpeg", "mp3")),
        (Audio(url="https://media.test/song.mp3"), None, (None, "mp3")),
        (Audio(url="https://media.test/clip", format="wav"), "audio/mpeg", (None, "wav")),
        (Video(url="https://media.test/clip.webm"), "video/webm", ("video/webm", "webm")),
        (Video(url="https://media.test/clip.mp4"), "application/octet-stream", (None, "mp4")),
        (Image(url="https://media.test/chart.png"), None, ("image/png", None)),
        (Image(url="https://media.test/photo", mime_type="image/png"), "image/webp", ("image/png", None)),
    ],
)
def test_downloaded_media_keeps_the_type_its_source_names(
    media: Image | Audio | Video,
    declared: str | None,
    expected: tuple[str | None, str | None],
) -> None:
    """The item's own type or format stands; else the declared type of its kind, then its URL's name, type it."""
    assert media.url is not None
    named = named_by_resource(media, url_file_name(media.url), declared)

    assert (named.mime_type, named.format) == expected


@pytest.mark.parametrize(
    ("file", "declared", "expected"),
    [
        (
            File(url="https://media.test/files/report%20q3.pdf"),
            "application/octet-stream",
            ("report q3.pdf", "application/pdf"),
        ),
        (File(url="https://media.test/download?id=7", filename="rows.txt"), "text/csv", ("rows.txt", "text/csv")),
        (File(url="https://media.test/export/notes.txt"), "image/png", ("notes.txt", "text/plain")),
        (File(url="https://media.test/export/report.csv.gz"), None, ("report.csv.gz", None)),
    ],
)
def test_a_downloaded_file_takes_a_document_type_and_its_url_s_name(
    file: File,
    declared: str | None,
    expected: tuple[str | None, str | None],
) -> None:
    """``application/octet-stream`` and a type no document has give way to the file's or the URL's name.

    A compressed name such as ``report.csv.gz`` names no type: its bytes are not CSV.
    """
    assert file.url is not None
    named = named_by_resource(file, url_file_name(file.url), declared)

    assert (named.filename, named.mime_type) == expected
