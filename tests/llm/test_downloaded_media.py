"""The type a media item read from its URL or file keeps: one rule for the pipeline's downloads and worker media."""

from __future__ import annotations

import pytest

from mindroom.llm.media import downloaded_media_fields, url_file_name


@pytest.mark.parametrize(
    ("kind", "url", "declared", "own", "expected"),
    [
        ("audio", "https://media.test/speech", "audio/mpeg", {}, {"mime_type": "audio/mpeg", "format": "mp3"}),
        ("audio", "https://media.test/song.mp3", None, {}, {"mime_type": None, "format": "mp3"}),
        ("audio", "https://media.test/clip", "audio/mpeg", {"media_format": "wav"}, {}),
        ("video", "https://media.test/clip.webm", "video/webm", {}, {"mime_type": "video/webm", "format": "webm"}),
        ("video", "https://media.test/clip.mp4", "application/octet-stream", {}, {"mime_type": None, "format": "mp4"}),
        ("image", "https://media.test/chart.png", None, {}, {"mime_type": "image/png"}),
        ("image", "https://media.test/photo", "image/webp", {"mime_type": "image/png"}, {}),
        (
            "file",
            "https://media.test/files/report%20q3.pdf",
            "application/octet-stream",
            {},
            {"filename": "report q3.pdf", "mime_type": "application/pdf"},
        ),
        (
            "file",
            "https://media.test/download?id=7",
            "text/csv",
            {"filename": "rows.txt"},
            {"filename": "rows.txt", "mime_type": "text/csv"},
        ),
    ],
)
def test_downloaded_media_keeps_the_type_its_source_names(
    kind: str,
    url: str,
    declared: str | None,
    own: dict[str, str],
    expected: dict[str, str | None],
) -> None:
    """The item's own type stands; else the declared type of its kind, then the URL's name, type it."""
    fields = {"mime_type": None, "media_format": None, **own}

    assert downloaded_media_fields(kind, url_file_name(url), declared, **fields) == expected


def test_worker_file_types_limit_what_a_file_is_typed_as() -> None:
    """A declared type outside ``file_types`` leaves the file's name to type it."""
    fields = downloaded_media_fields(
        "file",
        "notes.txt",
        "image/png",
        mime_type=None,
        media_format=None,
        file_types=frozenset({"text/plain"}),
    )

    assert fields == {"filename": "notes.txt", "mime_type": "text/plain"}
