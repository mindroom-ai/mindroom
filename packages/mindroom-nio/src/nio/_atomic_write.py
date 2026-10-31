# Copyright © 2026 The mindroom-nio authors
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

"""Minimal stdlib replacement for the unmaintained ``atomicwrites`` package.

Only the subset of the old ``atomicwrites.atomic_write`` API that nio used is
provided: text-mode writes with an ``overwrite`` flag.
"""

from __future__ import annotations

import errno
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO


def _create_at(tmp_path: str, path: str) -> None:
    """Move ``tmp_path`` to ``path``, raising ``FileExistsError`` if it exists.

    Hard-linking raises ``FileExistsError`` atomically when the destination
    exists, unlike ``os.replace()`` which would silently overwrite it. Some
    filesystems (FAT32/exFAT, certain network or FUSE mounts) do not support
    hard links at all; for those, fall back to an existence check followed by
    ``os.replace()``, which is still atomic but has a small window in which a
    concurrently created destination would be overwritten.
    """
    try:
        os.link(tmp_path, path)
    except FileExistsError:
        raise
    except OSError:
        if os.path.exists(path):
            raise FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), path
            ) from None
        os.replace(tmp_path, path)
    else:
        os.unlink(tmp_path)


@contextmanager
def atomic_write(
    path: str | os.PathLike[str], overwrite: bool = False
) -> Iterator[IO[str]]:
    """Write to ``path`` atomically via a temporary file in the same directory.

    The temporary file is only moved into place after it has been fully
    written and flushed to disk, so readers never observe a partially
    written file.

    The file is created with owner-only (0600) permissions, as
    ``tempfile.mkstemp`` does; the old ``atomicwrites`` package behaved the
    same way, and the callers in nio write key material where restrictive
    permissions are desirable.

    Args:
        path: The destination file path.
        overwrite: If ``False`` (the default), raise ``FileExistsError``
            when the destination already exists, matching the behavior of
            ``atomicwrites.atomic_write``.
    """
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix="~")
    try:
        with os.fdopen(fd, "w") as file:
            yield file
            file.flush()
            os.fsync(file.fileno())

        if overwrite:
            os.replace(tmp_path, path)
        else:
            _create_at(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
