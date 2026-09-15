"""Native desktop helper packaging contract tests."""

# ruff: noqa: D103

from __future__ import annotations

import os
import plistlib
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

if TYPE_CHECKING:
    import pytest


def test_helper_build_passes_exact_uv_arguments(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argument_log = tmp_path / "arguments"
    fake_uname = fake_bin / "uname"
    fake_uname.write_text("#!/bin/sh\necho Darwin\n")
    fake_uname.chmod(0o755)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/bin/sh
set -eu
: > "$ARGUMENT_LOG"
distpath=""
previous=""
for argument in "$@"; do
    printf '%s\\n' "$argument" >> "$ARGUMENT_LOG"
    if [ "$previous" = "--distpath" ]; then distpath="$argument"; fi
    previous="$argument"
done
mkdir -p "$distpath/MindRoom Desktop Helper.app/Contents/MacOS"
: > "$distpath/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
chmod +x "$distpath/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
""",
    )
    fake_uv.chmod(0o755)
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"

    subprocess.run(
        [str(repository / "macos" / "build-desktop-helper.sh"), "--output", str(output)],
        check=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "UV_BINARY": str(fake_uv),
            "ARGUMENT_LOG": str(argument_log),
        },
        capture_output=True,
        text=True,
    )

    arguments = argument_log.read_text().splitlines()
    assert "+" not in arguments
    assert arguments[:12] == [
        "run",
        "--isolated",
        "--locked",
        "--project",
        str(repository),
        "--no-default-groups",
        "--extra",
        "desktop",
        "--python",
        "3.13",
        "--with",
        "pyinstaller==6.16.0",
    ]
    assert "pyinstaller==6.16.0" in arguments
    assert arguments[-1] == str(repository / "macos" / "MindRoomDesktopHelper.spec")


def test_helper_spec_supplies_build_version_for_plist_stamping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute the spec and require the build-version key consumed by PlistBuddy Set."""
    spec = Path(__file__).resolve().parents[1] / "macos" / "MindRoomDesktopHelper.spec"
    hooks = SimpleNamespace(collect_all=lambda _name: ([], [], []), collect_submodules=lambda _name: [])
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    bundle = Mock()
    runpy.run_path(
        str(spec),
        init_globals={
            "SPECPATH": str(spec.parent),
            "Analysis": Mock(return_value=SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])),
            "PYZ": Mock(),
            "EXE": Mock(),
            "COLLECT": Mock(),
            "BUNDLE": bundle,
        },
    )
    # PyInstaller 6.16 supplies the short version itself, but not this build key.
    info = plistlib.loads(plistlib.dumps(bundle.call_args.kwargs["info_plist"]))
    assert isinstance(info["CFBundleVersion"], str)
    assert info["CFBundleVersion"].isdigit()
