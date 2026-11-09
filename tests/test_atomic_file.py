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


@pytest.mark.parametrize("taken", ["file", "dangling-link"])
def test_publishing_without_replace_refuses_a_taken_name(tmp_path: Path, taken: str) -> None:
    """Without ``replace`` a taken name, even a link leading nowhere, keeps what it has, and no temporary file stays."""
    path = tmp_path / "artifact"
    if taken == "file":
        path.write_bytes(b"old")
    else:
        path.symlink_to("missing")
    with (
        pytest.raises(FileExistsError),
        open_directory_within_root(tmp_path) as directory,
        atomic_write_file_at(directory, "artifact", replace=False) as output,
    ):
        output.write(b"new")
    if taken == "file":
        assert path.read_bytes() == b"old"
    else:
        assert str(path.readlink()) == "missing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifact"]
    assert not (tmp_path / "missing").exists()


def test_publishing_without_replace_makes_a_free_name_whole(tmp_path: Path) -> None:
    """Without ``replace`` a free name gets the whole content and no second link to it."""
    with (
        open_directory_within_root(tmp_path) as directory,
        atomic_write_file_at(directory, "artifact", replace=False) as output,
    ):
        output.write(b"new")
        assert not (tmp_path / "artifact").exists()
    assert (tmp_path / "artifact").read_bytes() == b"new"
    assert (tmp_path / "artifact").stat().st_nlink == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifact"]


def test_publishing_without_replace_never_links_what_a_swapped_in_link_leads_to(tmp_path: Path) -> None:
    """A temporary file swapped for a link meanwhile is published as that link, never as the file it leads to."""
    folder = tmp_path / "folder"
    folder.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    with (
        open_directory_within_root(folder) as directory,
        atomic_write_file_at(directory, "artifact", replace=False) as output,
    ):
        output.write(b"new")
        (temp,) = folder.iterdir()
        temp.unlink()
        temp.symlink_to(secret)
    assert (folder / "artifact").lstat().st_ino != secret.stat().st_ino
    assert secret.stat().st_nlink == 1
