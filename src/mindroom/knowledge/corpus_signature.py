"""What a knowledge base's corpus is at one moment: each listed file's signature, and their hash.

A file's signature is its status-change stamp, size, and content digest. A scan reuses a signature it already knows
while the file's stamp and size still match it, as git does, so an unchanged corpus is mostly not read again.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import time
from contextlib import nullcontext
from pathlib import Path
from typing import IO, TYPE_CHECKING

from mindroom.knowledge.file_listing import (
    git_tracked_relative_paths_from_checkout,
    knowledge_files_from_relative_paths,
    list_knowledge_files,
    open_knowledge_file,
    pinned_directories,
)
from mindroom.path_confinement import MAX_READ_BYTES, open_regular_file_at

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from mindroom.config.main import Config

#: ``(stamp, size, content digest)`` for one source file. The stamp is the file's status-change time
#: (``st_ctime_ns``), or 0 for a file read while it was changing. The content digest is what makes resume safe across
#: Git checkouts, which rewrite the stamp without changing content.
FileSignature = tuple[int, int, str]

#: Source files whose signatures are computed per thread hop, so a huge corpus
#: still yields to the event loop and to cancellation while it is scanned.
_SIGNATURE_SCAN_CHUNK = 512
#: A file changed this recently may change again within one timestamp tick, keeping its stamp and size, so its
#: signature records a stamp no stat matches, and the next scan reads it again instead of trusting its stat.
#: Comparing the server-assigned ctime with this host's clock assumes synchronized clocks, as ``make`` does.
_RACY_WINDOW_NS = 2_000_000_000


def _descriptor_signature(descriptor: int, file_path: Path, output: IO[bytes] | None = None) -> FileSignature:
    """Stat and hash one open knowledge file, copying its bytes to ``output`` when given.

    The signature's stamp is the status-change time, not the mtime: every write, mtime restore, and rename moves it,
    and no caller can set it, so a stat-first scan cannot mistake a timestamp-preserving replacement for the file it
    replaced. A file that changed within ``_RACY_WINDOW_NS`` of the read, or while it was read, records stamp 0.
    """
    status = os.fstat(descriptor)
    if status.st_size > MAX_READ_BYTES:
        msg = f"Knowledge file exceeds its size limit: {file_path}"
        raise ValueError(msg)
    started_ns = time.time_ns()
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    after = os.fstat(descriptor)
    racy = started_ns - status.st_ctime_ns < _RACY_WINDOW_NS or (after.st_ctime_ns, after.st_size) != (
        status.st_ctime_ns,
        status.st_size,
    )
    return 0 if racy else status.st_ctime_ns, status.st_size, digest.hexdigest()


def file_signature(file_path: Path, snapshot: Path | None = None) -> FileSignature:
    """Stat and hash one listed file through one no-follow descriptor, copying it to ``snapshot`` when given."""
    with open_knowledge_file(file_path) as descriptor, snapshot.open("xb") if snapshot else nullcontext() as output:
        return _descriptor_signature(descriptor, file_path, output)


def _scan_file_signatures(
    files: Iterable[tuple[str, Path]],
    known: Mapping[str, FileSignature],
) -> Iterator[tuple[str, FileSignature, Path]]:
    """Sign listed files, reusing a ``known`` signature while the file's ctime and size still match it.

    As git does, an unchanged stat counts as unchanged bytes, so most files of a large corpus are not read again;
    ``_RACY_WINDOW_NS`` keeps a change within one timestamp tick from passing for unchanged. Each parent is
    pinned once by the no-follow walk from the filesystem root that ``open_knowledge_file`` repeats per file, and
    its entries are stat-ed and opened without following links. Files that vanished or are not regular are skipped.
    """
    with pinned_directories(Path("/")) as pin:
        for relative_path, path in files:
            try:
                parent_fd = pin(path.parent.relative_to(path.anchor))
                status = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                recorded = known.get(relative_path)
                if (
                    recorded is not None
                    and stat.S_ISREG(status.st_mode)
                    and recorded[:2] == (status.st_ctime_ns, status.st_size)
                ):
                    yield relative_path, recorded, path
                    continue
                descriptor = open_regular_file_at(parent_fd, path.name)
                try:
                    signature = _descriptor_signature(descriptor, path)
                finally:
                    os.close(descriptor)
            except (OSError, ValueError):
                continue
            yield relative_path, signature, path


def scan_corpus(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    *,
    git_dir: Path,
    tracked_relative_paths: Iterable[str] | None = None,
    known: Mapping[str, FileSignature] | None = None,
) -> dict[str, tuple[FileSignature, Path]]:
    """Sign every file the base currently manages, by relative path; one whose stat matches ``known`` is not read."""
    base_config = config.get_knowledge_base_config(base_id)
    if base_config.git is None:
        files = list_knowledge_files(config, base_id, knowledge_root)
    else:
        tracked_paths = (
            set(tracked_relative_paths)
            if tracked_relative_paths is not None
            else git_tracked_relative_paths_from_checkout(config, base_id, knowledge_root, git_dir)
        )
        files = knowledge_files_from_relative_paths(config, base_id, knowledge_root, tracked_paths)
    listed = sorted((path.relative_to(knowledge_root).as_posix(), path) for path in files)
    return {
        relative_path: (signature, path)
        for relative_path, signature, path in _scan_file_signatures(listed, known or {})
    }


def corpus_signature(file_signatures: Mapping[str, FileSignature]) -> str:
    """Return the hash of a corpus's relative paths and file signatures, which a publication records."""
    digest = hashlib.sha256()
    for relative_path, (stamp, size, content_digest) in sorted(file_signatures.items()):
        for part in (relative_path.encode("utf-8"), str(stamp).encode("ascii"), str(size).encode("ascii")):
            digest.update(part)
            digest.update(b"\0")
        digest.update(content_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


async def scan_file_signatures_async(
    files: Sequence[tuple[str, Path]],
    known: Mapping[str, FileSignature],
) -> dict[str, tuple[FileSignature, Path]]:
    """Sign listed files a chunk per thread hop, so a huge corpus still yields to the loop and to cancellation."""
    signatures: dict[str, tuple[FileSignature, Path]] = {}
    for start in range(0, len(files), _SIGNATURE_SCAN_CHUNK):
        batch = files[start : start + _SIGNATURE_SCAN_CHUNK]
        scanned = await asyncio.to_thread(lambda batch=batch: list(_scan_file_signatures(batch, known)))
        for relative_path, signature, file_path in scanned:
            signatures[relative_path] = (signature, file_path)
    return signatures
