#!/usr/bin/env python
"""Test that each tool extra installs and loads correctly in an isolated venv.

Creates a fresh virtual environment per tool, installs mindroom with only
that tool's extra, then verifies the tool's factory function and declared
dependencies work.

Pre-builds a wheel to avoid race conditions during parallel testing and to
speed up repeated installs (uv caches resolved wheels).

Usage:
    python scripts/test_tool_extras_isolation.py              # All tools, serial
    python scripts/test_tool_extras_isolation.py --tool github # Single tool
    python scripts/test_tool_extras_isolation.py --workers 8   # Parallel
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Groups in pyproject.toml that are NOT tool extras
IGNORED_GROUPS = {"supabase"}

# Tools registered only in TOOL_METADATA (no factory in TOOL_REGISTRY).
# These are instantiated with special context in create_agent() and cannot
# be tested generically via get_tool_by_name().
METADATA_ONLY_TOOLS = {"memory"}

PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"

# Script executed inside each isolated venv to test a single tool.
# Passed via ``python -c <script> <tool_name>``.
_TOOL_TEST_SCRIPT = """\
import importlib.util
import sys

tool_name = sys.argv[1]

import mindroom.tools  # noqa: E402
from mindroom.tool_dependencies import _pip_name_to_import, check_deps_installed  # noqa: E402
from mindroom.tools_metadata import TOOL_METADATA, TOOL_REGISTRY  # noqa: E402

if tool_name not in TOOL_REGISTRY:
    print(f"NOT_REGISTERED: {tool_name}")
    sys.exit(2)

# Check 1: all declared dependencies are importable
meta = TOOL_METADATA.get(tool_name)
if meta and meta.dependencies:
    if not check_deps_installed(meta.dependencies):
        missing = [
            d
            for d in meta.dependencies
            if importlib.util.find_spec(_pip_name_to_import(d)) is None
        ]
        print(f"DEPS_MISSING: {missing}")
        sys.exit(1)

# Check 2: factory function succeeds (does the real import)
factory = TOOL_REGISTRY[tool_name]
try:
    tool_class = factory()
    print(f"OK: {tool_class.__name__}")
except ImportError as e:
    print(f"IMPORT_ERROR: {e}")
    sys.exit(1)
except Exception as e:
    # Non-import errors (missing API keys, etc.) are acceptable --
    # we only care that the dependency imports succeeded.
    print(f"OK_NON_IMPORT: {type(e).__name__}")
"""


@dataclass
class ToolResult:
    name: str
    passed: bool
    message: str
    phase: str = ""  # "build", "install", or "load"


@dataclass
class _SharedContext:
    """Immutable context passed to each worker (must be picklable)."""

    wheel_path: str
    python_version: str


# Module-level context set by main() before spawning workers.
_ctx: _SharedContext | None = None


def get_tool_extras() -> dict[str, list[str]]:
    """Parse pyproject.toml and return tool extras with their dependencies."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    optional = data.get("project", {}).get("optional-dependencies", {})
    return {k: v for k, v in sorted(optional.items()) if k not in IGNORED_GROUPS and k not in METADATA_ONLY_TOOLS}


def _build_wheel(tmpdir: Path) -> Path:
    """Build a wheel once so parallel installs don't race on the source tree."""
    r = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmpdir)],
        check=False,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    if r.returncode != 0:
        print(f"ERROR: wheel build failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)
    wheels = list(tmpdir.glob("*.whl"))
    if not wheels:
        print("ERROR: no wheel produced", file=sys.stderr)
        sys.exit(1)
    return wheels[0]


def _init_worker(ctx: _SharedContext) -> None:
    """Initializer for each worker process — stores the shared context."""
    global _ctx
    _ctx = ctx


def test_tool(tool_name: str) -> ToolResult:
    """Test a single tool extra in an isolated virtual environment."""
    assert _ctx is not None
    with tempfile.TemporaryDirectory(prefix=f"mr_{tool_name}_") as tmpdir:
        venv_dir = Path(tmpdir) / "venv"

        # Step 1: create venv
        r = subprocess.run(
            ["uv", "venv", str(venv_dir), "--python", _ctx.python_version, "-q"],
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return ToolResult(tool_name, False, f"venv creation failed: {r.stderr.strip()[:300]}", "install")

        python = str(venv_dir / "bin" / "python")

        # Step 2: install wheel + this extra (no editable, no build race)
        spec = f"{_ctx.wheel_path}[{tool_name}]"
        r = subprocess.run(
            ["uv", "pip", "install", "--python", python, spec],
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            stderr = r.stderr.strip()
            if len(stderr) > 400:
                stderr = f"...{stderr[-400:]}"
            return ToolResult(tool_name, False, f"install failed: {stderr}", "install")

        # Step 3: run the tool test inside the isolated venv
        r = subprocess.run(
            [python, "-c", _TOOL_TEST_SCRIPT, tool_name],
            check=False,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )

        output = (r.stdout.strip() + " " + r.stderr.strip()).strip()
        if len(output) > 400:
            output = f"...{output[-400:]}"

        if r.returncode == 0:
            return ToolResult(tool_name, True, output, "load")
        return ToolResult(tool_name, False, output, "load")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test tool extras in isolated environments")
    parser.add_argument("--tool", help="Test a specific tool only")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel workers (default: 1)")
    args = parser.parse_args()

    if not shutil.which("uv"):
        print("ERROR: 'uv' is required but not found in PATH")
        return 1

    extras = get_tool_extras()

    if args.tool:
        if args.tool not in extras:
            print(f"ERROR: Unknown tool extra '{args.tool}'")
            print(f"Available: {', '.join(sorted(extras))}")
            return 1
        extras = {args.tool: extras[args.tool]}

    total = len(extras)

    # Pre-build a wheel so parallel installs don't race on the source.
    wheel_dir = tempfile.mkdtemp(prefix="mindroom_wheel_")
    wheel_dir_path = Path(wheel_dir)
    try:
        print("Building wheel...")
        wheel_path = _build_wheel(wheel_dir_path)
        print(f"Built: {wheel_path.name}")
        print(f"Testing {total} tool extras in isolated environments...")
        print(f"Python: {PYTHON_VERSION}, Workers: {args.workers}")
        print()

        ctx = _SharedContext(wheel_path=str(wheel_path), python_version=PYTHON_VERSION)
        # Set context for the main process too (used in serial mode).
        global _ctx
        _ctx = ctx

        results: list[ToolResult] = []

        if args.workers > 1:
            with ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=_init_worker,
                initargs=(ctx,),
            ) as executor:
                futures = {executor.submit(test_tool, name): name for name in extras}
                for i, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    status = "PASS" if result.passed else "FAIL"
                    print(f"[{i}/{total}] {result.name}: {status} — {result.message}")
                    results.append(result)
        else:
            for i, tool_name in enumerate(sorted(extras), 1):
                result = test_tool(tool_name)
                status = "PASS" if result.passed else "FAIL"
                print(f"[{i}/{total}] {result.name}: {status} — {result.message}")
                results.append(result)

        # Summary
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]

        print()
        print("=" * 60)
        print(f"Results: {len(passed)}/{total} passed, {len(failed)} failed")

        if failed:
            print()
            print("Failed tools:")
            for r in sorted(failed, key=lambda x: x.name):
                print(f"  {r.name} [{r.phase}]: {r.message}")
            return 1

        print()
        print("All tools passed!")
        return 0

    finally:
        shutil.rmtree(wheel_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
