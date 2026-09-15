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
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(("architecture", "python_arch"), [("arm64", "aarch64"), ("x86_64", "x86_64")])
def test_helper_build_uses_matching_python_and_wheels(tmp_path: Path, architecture: str, python_arch: str) -> None:
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
: > "$ARGUMENT_LOG.$1"
log="$ARGUMENT_LOG.$1"
distpath=""
previous=""
for argument in "$@"; do
    printf '%s\\n' "$argument" >> "$log"
    if [ "$previous" = "--distpath" ]; then distpath="$argument"; fi
    previous="$argument"
done
[ -n "$distpath" ] || exit 0
mkdir -p "$distpath/MindRoom Desktop Helper.app/Contents/MacOS"
: > "$distpath/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
chmod +x "$distpath/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper"
""",
    )
    fake_uv.chmod(0o755)
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"

    subprocess.run(
        [str(repository / "macos" / "build-desktop-helper.sh"), "--output", str(output), "--arch", architecture],
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

    sync_arguments = argument_log.with_suffix(".sync").read_text().splitlines()
    assert sync_arguments == [
        "sync",
        "--locked",
        "--project",
        str(repository),
        "--only-group",
        "desktop-helper",
        "--python",
        f"cpython-3.13-macos-{python_arch}-none",
        "--python-platform",
        f"{python_arch}-apple-darwin",
    ]
    assert argument_log.with_suffix(".pip").read_text().splitlines() == [
        "pip",
        "install",
        "--python",
        str(output / "environment/bin/python"),
        "--no-deps",
        "--editable",
        str(repository),
    ]
    arguments = argument_log.with_suffix(".run").read_text().splitlines()
    assert arguments[:10] == [
        "run",
        "--no-project",
        "--python",
        str(output / "environment/bin/python"),
        "/usr/bin/arch",
        f"-{architecture}",
        "python",
        "-m",
        "PyInstaller",
        "--clean",
    ]
    assert arguments[-1] == str(repository / "macos" / "MindRoomDesktopHelper.spec")


def test_helper_spec_supplies_build_version_for_plist_stamping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute the spec and require the build-version key consumed by PlistBuddy Set."""
    spec = Path(__file__).resolve().parents[1] / "macos" / "MindRoomDesktopHelper.spec"
    hooks = SimpleNamespace(
        collect_all=lambda _name: ([], [], []),
        collect_submodules=lambda _name: [],
        copy_metadata=lambda _name: [],
    )
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
