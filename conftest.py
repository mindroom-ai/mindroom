"""Root pytest configuration.

This file exists at the repository root so pytest imports it before
``tests/conftest.py`` and before any test module imports the MindRoom CLI.
"""

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Rich resolves color support once, when a `Console` is constructed, and the CLI builds
# its consoles at import time (`mindroom.cli.config`, `.desktop`, `.service`), so no
# fixture can influence them. A shell that exports `FORCE_COLOR` (Claude Code, Codex and
# several terminals do) makes Rich force a color terminal even though
# `typer.testing.CliRunner` captures to a non-tty buffer, baking ANSI escapes into output
# that tests assert on and parse as YAML.
#
# Pinning a dumb terminal is the one switch that settles it, because Rich gates all three
# sources of noise on `Console.is_dumb_terminal`: color (`_detect_color_system` returns
# `None`), control codes (`Console.control` writes nothing) and width (`Console.size`
# returns a fixed 80x25 instead of the invoking terminal's size). It wins over
# `FORCE_COLOR` and over Typer's own `force_terminal` consoles. Setting `NO_COLOR`
# instead would not do: it strips color but leaves bold and other SGR codes.
os.environ["TERM"] = "dumb"

# Most of this suite's time on a real disk is spent in fsync. Event journals, turn
# ledgers and atomic JSON stores make every commit durable, and the tests that walk
# their bounds commit thousands of times. tmpfs completes fsync without touching a
# device: on a 32-worker run, summed test time fell from 6531 s to 1763 s.
#
# No test can observe the difference. Durability tests simulate a crashed process,
# and a crashed process never needed its writes to leave the page cache.
#
# This is only a default. An exported TMPDIR wins, and a /dev/shm without room
# (Docker gives containers 64 MiB) is left alone rather than failing mid-run.
_TMPFS = Path("/dev/shm")  # noqa: S108
_TMPFS_MIN_FREE_BYTES = 1 << 30
if (
    "TMPDIR" not in os.environ
    and sys.platform == "linux"
    and _TMPFS.is_dir()
    and os.access(_TMPFS, os.W_OK)
    and shutil.disk_usage(_TMPFS).free >= _TMPFS_MIN_FREE_BYTES
):
    # tiktoken caches the encodings it downloads under the temp root. Pin the
    # cache where it has always been so a local run keeps working offline.
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(tempfile.gettempdir()) / "data-gym-cache"))
    os.environ["TMPDIR"] = str(_TMPFS)
    tempfile.tempdir = None


@pytest.fixture(scope="session", autouse=True)
def _contain_temporary_files_on_tmpfs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep bare `tempfile` output inside pytest's pruned base directory on tmpfs.

    Tests and the code they drive call `tempfile.mkdtemp()` without removing the
    result, over a thousand times per run. On disk that only clutters `/tmp`, but
    on tmpfs it would hold memory until reboot. Pointing the temp root into the
    base directory hands it to pytest's own retention, which keeps three runs.

    xdist workers inherit TMPDIR from the controller, so this checks where the
    temp root is rather than whether this process moved it.
    """
    if Path(tempfile.gettempdir()) != _TMPFS:
        yield
        return
    contained = tmp_path_factory.getbasetemp() / "tmp"
    contained.mkdir()
    os.environ["TMPDIR"] = str(contained)
    tempfile.tempdir = str(contained)
    try:
        yield
    finally:
        os.environ["TMPDIR"] = str(_TMPFS)
        tempfile.tempdir = None
