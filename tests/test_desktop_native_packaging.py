"""Native desktop helper packaging contract tests."""

# ruff: noqa: D103

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
    assert arguments[:7] == [
        "run",
        "--isolated",
        "--python",
        "3.13",
        "--with",
        f"{repository}[desktop]",
        "--with",
    ]
    assert "pyinstaller==6.16.0" in arguments
    assert arguments[-1] == str(repository / "macos" / "MindRoomDesktopHelper.spec")
