#!/usr/bin/env python3
"""Smoke-test runtime tool dependency auto-installation in an isolated environment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = Path(__file__).resolve()
REPO_VENV = (PROJECT_ROOT / ".venv").resolve()


@dataclass(slots=True)
class ToolCheckResult:
    tool: str
    status: str
    dependencies: list[str]
    had_all_dependencies_before: bool
    has_all_dependencies_after: bool
    error: str | None = None


def _run_checked(cmd: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(cmd, check=False, text=True, capture_output=True, cwd=cwd)
    if result.returncode == 0:
        return
    print(f"Command failed: {' '.join(cmd)}", file=sys.stderr)
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    msg = f"Failed command: {' '.join(cmd)}"
    raise RuntimeError(msg)


def _venv_python(venv_dir: Path) -> Path:
    posix = venv_dir / "bin" / "python"
    windows = venv_dir / "Scripts" / "python.exe"
    if posix.exists():
        return posix
    if windows.exists():
        return windows
    msg = f"Could not locate virtualenv python in {venv_dir}"
    raise FileNotFoundError(msg)


def _create_isolated_environment(venv_dir: Path, python: str) -> Path:
    if shutil.which("uv"):
        _run_checked(["uv", "venv", str(venv_dir), "--python", python], cwd=PROJECT_ROOT)
        python_path = _venv_python(venv_dir)
        _run_checked(["uv", "pip", "install", "--python", str(python_path), "-e", "."], cwd=PROJECT_ROOT)
        return python_path

    _run_checked([sys.executable, "-m", "venv", str(venv_dir)], cwd=PROJECT_ROOT)
    python_path = _venv_python(venv_dir)
    _run_checked([str(python_path), "-m", "pip", "install", "--upgrade", "pip"], cwd=PROJECT_ROOT)
    _run_checked([str(python_path), "-m", "pip", "install", "-e", "."], cwd=PROJECT_ROOT)
    return python_path


def _dependencies_installed(checker: Callable[[list[str]], bool], dependencies: list[str]) -> bool:
    if not dependencies:
        return True
    try:
        return checker(dependencies)
    except ModuleNotFoundError:
        return False


def _run_worker(*, tools: set[str] | None, json_output: bool) -> int:
    current_prefix = Path(sys.prefix).resolve()
    parent_prefix = os.environ.get("MINDROOM_PARENT_PREFIX")

    if current_prefix == REPO_VENV:
        print(
            "Worker is running in repository .venv; this smoke test must run in a fresh environment.", file=sys.stderr
        )
        return 2
    if parent_prefix and current_prefix == Path(parent_prefix).resolve():
        print("Worker reused the parent interpreter environment; isolation check failed.", file=sys.stderr)
        return 2

    from mindroom.tool_dependencies import check_deps_installed
    from mindroom.tools_metadata import (
        TOOL_METADATA,
        TOOL_REGISTRY,
        ToolStatus,
        ensure_tool_registry_loaded,
        get_tool_by_name,
    )

    ensure_tool_registry_loaded()
    available_tools = sorted(TOOL_REGISTRY)
    if tools is None:
        selected_tools = available_tools
    else:
        unknown = sorted(tools - set(available_tools))
        if unknown:
            print(f"Unknown tools requested: {', '.join(unknown)}", file=sys.stderr)
            return 2
        selected_tools = [tool_name for tool_name in available_tools if tool_name in tools]

    results: list[ToolCheckResult] = []

    for tool_name in selected_tools:
        metadata = TOOL_METADATA[tool_name]
        deps = list(metadata.dependencies or [])
        before = _dependencies_installed(check_deps_installed, deps)

        status = "ok"
        error: str | None = None
        try:
            toolkit = get_tool_by_name(tool_name, disable_sandbox_proxy=True)
            _ = getattr(toolkit, "name", tool_name)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

        after = _dependencies_installed(check_deps_installed, deps)
        if deps and not after:
            status = "failed"
            missing_msg = "dependencies still missing after tool load"
            error = f"{error}; {missing_msg}" if error else missing_msg
        elif status == "failed" and metadata.status == ToolStatus.REQUIRES_CONFIG:
            status = "config_required"

        results.append(
            ToolCheckResult(
                tool=tool_name,
                status=status,
                dependencies=deps,
                had_all_dependencies_before=before,
                has_all_dependencies_after=after,
                error=error,
            ),
        )

    failed = [result for result in results if result.status == "failed"]
    config_required = [result for result in results if result.status == "config_required"]
    succeeded = [result for result in results if result.status == "ok"]

    payload = {
        "python_executable": sys.executable,
        "sys_prefix": sys.prefix,
        "summary": {
            "total": len(results),
            "ok": len(succeeded),
            "config_required": len(config_required),
            "failed": len(failed),
        },
        "results": [asdict(result) for result in results],
    }

    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Python executable: {sys.executable}")
        print(f"Environment prefix: {sys.prefix}")
        for result in results:
            before_after = (
                f"deps_before={result.had_all_dependencies_before} deps_after={result.has_all_dependencies_after}"
            )
            if result.error:
                print(f"[{result.status}] {result.tool} ({before_after}) -> {result.error}")
            else:
                print(f"[{result.status}] {result.tool} ({before_after})")
        summary = payload["summary"]
        print(
            "\nSummary: "
            f"total={summary['total']} ok={summary['ok']} "
            f"config_required={summary['config_required']} failed={summary['failed']}",
        )

    return 1 if failed else 0


def _run_host(args: argparse.Namespace) -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="mindroom-tool-auto-install-"))
    venv_dir = temp_dir / "venv"
    keep_env = args.keep_env

    print(f"Creating isolated environment at {venv_dir}", flush=True)
    try:
        worker_python = _create_isolated_environment(venv_dir, args.python)
        cmd = [str(worker_python), str(SCRIPT_PATH), "--worker"]
        if args.json:
            cmd.append("--json")
        for tool_name in args.tool:
            cmd.extend(["--tool", tool_name])

        env = os.environ.copy()
        env["MINDROOM_PARENT_PREFIX"] = sys.prefix
        result = subprocess.run(cmd, check=False, cwd=PROJECT_ROOT, env=env)
        return result.returncode
    finally:
        if keep_env:
            print(f"Kept environment at {venv_dir}", flush=True)
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    parser.add_argument("--tool", action="append", default=[], help="Only run this tool (repeatable)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--keep-env", action="store_true", help="Do not delete the temporary environment")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.worker:
        selected = set(args.tool) if args.tool else None
        return _run_worker(tools=selected, json_output=args.json)
    return _run_host(args)


if __name__ == "__main__":
    raise SystemExit(main())
