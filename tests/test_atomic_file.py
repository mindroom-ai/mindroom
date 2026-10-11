"""Streaming and byte publication share one descriptor-bound transaction."""

from pathlib import Path

import pytest

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import open_directory_within_root


def test_stream_is_published_only_after_success(tmp_path: Path) -> None:
    """Readers retain the old artifact until a complete streamed replacement is ready."""
    path = tmp_path / "artifact"
    path.write_bytes(b"old")
    with open_directory_within_root(tmp_path) as directory, atomic_write_file_at(directory, "artifact") as output:
        output.write(b"first")
        output.write(b"second")
        assert path.read_bytes() == b"old"
    assert path.read_bytes() == b"firstsecond"
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifact"]


def test_interrupted_stream_preserves_previous_artifact(tmp_path: Path) -> None:
    """Failed streaming leaves no partial destination or temporary file."""
    path = tmp_path / "artifact"
    path.write_bytes(b"old")
    with (  # noqa: PT012 - interrupt the transaction after a partial write
        pytest.raises(RuntimeError, match="download interrupted"),
        open_directory_within_root(tmp_path) as directory,
        atomic_write_file_at(directory, "artifact") as output,
    ):
        output.write(b"partial")
        message = "download interrupted"
        raise RuntimeError(message)
    assert path.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifact"]
