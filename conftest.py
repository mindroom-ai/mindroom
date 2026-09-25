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
# device: summed test time on a 32-worker NVMe machine fell from 5236 s to 1426 s,
# and the GitHub Actions job from 16.5 to 12 minutes.
#
# No test can observe the difference. Durability tests simulate a crashed process,
# and a crashed process never needed its writes to leave the page cache.
#
# This is only a default. An exported temp root wins, and a /dev/shm that is small
# or mounted noexec (Docker gives containers 64 MiB, noexec) is left alone rather
# than failing mid-run or refusing the scripts tests mark executable. A full run
# peaks at about 3 GiB there, mostly worker virtualenvs.
_TMPFS = Path("/dev/shm")  # noqa: S108
_TMPFS_MIN_FREE_BYTES = 4 << 30
_TMPFS_BASETEMP = pytest.StashKey[Path]()
if (
    not any(name in os.environ for name in ("TMPDIR", "TEMP", "TMP"))
    and sys.platform == "linux"
    and _TMPFS.is_dir()
    and os.access(_TMPFS, os.W_OK)
    and not os.statvfs(_TMPFS).f_flag & os.ST_NOEXEC
    and shutil.disk_usage(_TMPFS).free >= _TMPFS_MIN_FREE_BYTES
):
    # tiktoken caches the encodings it downloads under the temp root, which the
    # fixture below makes per-run. Pin the cache where it has always been so a
    # local run neither downloads it again nor needs the network.
    if "TIKTOKEN_CACHE_DIR" not in os.environ and "DATA_GYM_CACHE_DIR" not in os.environ:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    os.environ["TMPDIR"] = str(_TMPFS)
    tempfile.tempdir = None


@pytest.fixture(scope="session", autouse=True)
def _contain_temporary_files_on_tmpfs(
    pytestconfig: pytest.Config,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep bare `tempfile` output inside pytest's base directory on tmpfs.

    Tests and the code they drive call `tempfile.mkdtemp()` without removing the
    result, over a thousand times per run. On disk that only clutters `/tmp`, but
    on tmpfs it would hold memory until reboot, so it joins the `tmp_path`
    directories that `pytest_sessionfinish` frees.

    xdist workers inherit TMPDIR from the controller, so this checks where the
    temp root is rather than whether this process moved it.
    """
    if Path(tempfile.gettempdir()) != _TMPFS:
        yield
        return
    basetemp = tmp_path_factory.getbasetemp()
    # xdist hands every worker its directory as `--basetemp`, so where it lives is
    # the only sign that it is pytest's own tree on tmpfs rather than a disk path.
    if basetemp.is_relative_to(_TMPFS):
        pytestconfig.stash[_TMPFS_BASETEMP] = basetemp
    contained = basetemp / "tmp"
    contained.mkdir()
    os.environ["TMPDIR"] = str(contained)
    tempfile.tempdir = str(contained)
    yield
    os.environ["TMPDIR"] = str(_TMPFS)
    tempfile.tempdir = None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Free a passing session's tmpfs base directory instead of holding it in memory.

    pytest keeps the last three runs' directories, which on tmpfs would pin about
    8 GiB of memory. Each xdist worker owns its own base directory, so a failing
    run keeps only the workers that saw a failure. Deleting per test instead is
    not an option: pytest then reuses the freed names, and caches keyed by path
    hand the next test the previous one's state.
    """
    basetemp = session.config.stash.get(_TMPFS_BASETEMP, None)
    if basetemp is not None and exitstatus == 0:
        shutil.rmtree(basetemp, ignore_errors=True)
