"""Unified smoke tests for tool dependency installation in isolated environments.

This script combines two checks:
1. ``runtime-auto-install``: start from a fresh env with base MindRoom install and
   verify ``get_tool_by_name`` auto-installs tool dependencies on demand.
2. ``extra-install``: create a fresh env per tool, install ``mindroom[<tool>]``,
   and verify declared dependencies import and the tool factory loads.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from mindroom.tool_system.dependencies import _pip_name_to_import, check_deps_installed
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    TOOL_REGISTRY,
    ToolStatus,
    ensure_tool_registry_loaded,
    get_tool_by_name,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = Path(__file__).resolve()
REPO_VENV = (PROJECT_ROOT / ".venv").resolve()
IGNORED_OPTIONAL_DEP_GROUPS = {"supabase"}
METADATA_ONLY_TOOLS = {"memory"}


@dataclass(slots=True)
class RuntimeToolCheckResult:
    """Result for one tool in runtime auto-install mode."""

    tool: str
    status: str
    dependencies: list[str]
    had_all_dependencies_before: bool
    has_all_dependencies_after: bool
    error: str | None = None


@dataclass(slots=True)
class ExtraToolCheckResult:
    """Result for one tool in per-extra install mode."""

    tool: str
    status: str
    dependencies: list[str]
    has_all_dependencies: bool
    error: str | None = None


@dataclass(slots=True)
class ExtraHostResult:
    """Result for a host-side per-tool extra run."""

    tool: str
    status: str
    phase: str
    message: str


def _run_checked(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(cmd, check=False, text=True, capture_output=True, cwd=cwd, env=env)
    if result.returncode == 0:
        return
    print(f"Command failed: {' '.join(cmd)}", file=sys.stderr)
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    msg = f"Failed command: {' '.join(cmd)}"
    raise RuntimeError(msg)


def _trim_output(*, stdout: str, stderr: str, max_chars: int = 400) -> str:
    combined = (stdout.strip() + " " + stderr.strip()).strip()
    if len(combined) <= max_chars:
        return combined
    return f"...{combined[-max_chars:]}"


def _venv_python(venv_dir: Path) -> Path:
    posix = venv_dir / "bin" / "python"
    windows = venv_dir / "Scripts" / "python.exe"
    if posix.exists():
        return posix
    if windows.exists():
        return windows
    msg = f"Could not locate virtualenv python in {venv_dir}"
    raise FileNotFoundError(msg)


def _create_isolated_environment(venv_dir: Path, python: str, *, install_editable: bool) -> Path:
    if shutil.which("uv"):
        _run_checked(["uv", "venv", str(venv_dir), "--python", python], cwd=PROJECT_ROOT)
        python_path = _venv_python(venv_dir)
        if install_editable:
            _run_checked(["uv", "pip", "install", "--python", str(python_path), "-e", "."], cwd=PROJECT_ROOT)
        return python_path

    _run_checked([sys.executable, "-m", "venv", str(venv_dir)], cwd=PROJECT_ROOT)
    python_path = _venv_python(venv_dir)
    _run_checked([str(python_path), "-m", "pip", "install", "--upgrade", "pip"], cwd=PROJECT_ROOT)
    if install_editable:
        _run_checked([str(python_path), "-m", "pip", "install", "-e", "."], cwd=PROJECT_ROOT)
    return python_path


def _dependencies_installed(dependencies: list[str]) -> bool:
    if not dependencies:
        return True
    try:
        return check_deps_installed(dependencies)
    except ModuleNotFoundError:
        return False


def _worker_isolation_error() -> str | None:
    current_prefix = Path(sys.prefix).resolve()
    parent_prefix = os.environ.get("MINDROOM_PARENT_PREFIX")

    if current_prefix == REPO_VENV:
        return "Worker is running in repository .venv; this smoke test must run in a fresh environment."
    if parent_prefix and current_prefix == Path(parent_prefix).resolve():
        return "Worker reused the parent interpreter environment; isolation check failed."
    return None


def _available_tool_extras() -> set[str]:
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    optional = data.get("project", {}).get("optional-dependencies", {})
    return {
        group_name
        for group_name in optional
        if group_name not in IGNORED_OPTIONAL_DEP_GROUPS and group_name not in METADATA_ONLY_TOOLS
    }


def _select_tools(
    *,
    requested_tools: set[str] | None,
    available_tools: list[str],
) -> tuple[list[str], list[str]]:
    if requested_tools is None:
        return available_tools, []
    unknown = sorted(requested_tools - set(available_tools))
    selected = [tool_name for tool_name in available_tools if tool_name in requested_tools]
    return selected, unknown


def _runtime_check_tool(tool_name: str) -> RuntimeToolCheckResult:
    metadata = TOOL_METADATA[tool_name]
    dependencies = list(metadata.dependencies or [])
    before = _dependencies_installed(dependencies)

    status = "ok"
    error: str | None = None
    try:
        toolkit = get_tool_by_name(tool_name, disable_sandbox_proxy=True)
        _ = getattr(toolkit, "name", tool_name)
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    after = _dependencies_installed(dependencies)
    if dependencies and not after:
        status = "failed"
        missing_msg = "dependencies still missing after tool load"
        error = f"{error}; {missing_msg}" if error else missing_msg
    elif status == "failed" and metadata.status == ToolStatus.REQUIRES_CONFIG:
        status = "config_required"

    return RuntimeToolCheckResult(
        tool=tool_name,
        status=status,
        dependencies=dependencies,
        had_all_dependencies_before=before,
        has_all_dependencies_after=after,
        error=error,
    )


def _emit_runtime_results(results: list[RuntimeToolCheckResult], *, json_output: bool) -> int:
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


def _missing_dependencies(dependencies: list[str]) -> list[str]:
    missing: list[str] = []
    for dep in dependencies:
        module_name = _pip_name_to_import(dep)
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            missing.append(dep)
    return missing


def _extra_check_tool(tool_name: str) -> ExtraToolCheckResult:
    if tool_name not in TOOL_REGISTRY:
        return ExtraToolCheckResult(
            tool=tool_name,
            status="failed",
            dependencies=[],
            has_all_dependencies=False,
            error="tool is not registered in TOOL_REGISTRY",
        )

    metadata = TOOL_METADATA.get(tool_name)
    dependencies = list((metadata.dependencies if metadata else []) or [])
    has_all_dependencies = _dependencies_installed(dependencies)
    if not has_all_dependencies:
        missing = ", ".join(_missing_dependencies(dependencies))
        return ExtraToolCheckResult(
            tool=tool_name,
            status="failed",
            dependencies=dependencies,
            has_all_dependencies=False,
            error=f"dependencies missing after extra install: {missing}",
        )

    factory = TOOL_REGISTRY[tool_name]
    try:
        _ = factory()
        return ExtraToolCheckResult(
            tool=tool_name,
            status="ok",
            dependencies=dependencies,
            has_all_dependencies=True,
        )
    except ImportError as exc:
        return ExtraToolCheckResult(
            tool=tool_name,
            status="failed",
            dependencies=dependencies,
            has_all_dependencies=True,
            error=f"ImportError: {exc}",
        )
    except Exception as exc:
        status = "ok"
        if metadata and metadata.status == ToolStatus.REQUIRES_CONFIG:
            status = "config_required"
        return ExtraToolCheckResult(
            tool=tool_name,
            status=status,
            dependencies=dependencies,
            has_all_dependencies=True,
            error=f"{type(exc).__name__}: {exc}",
        )


def _emit_extra_results(results: list[ExtraToolCheckResult], *, json_output: bool) -> int:
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
            dep_status = f"deps_installed={result.has_all_dependencies}"
            if result.error:
                print(f"[{result.status}] {result.tool} ({dep_status}) -> {result.error}")
            else:
                print(f"[{result.status}] {result.tool} ({dep_status})")
        summary = payload["summary"]
        print(
            "\nSummary: "
            f"total={summary['total']} ok={summary['ok']} "
            f"config_required={summary['config_required']} failed={summary['failed']}",
        )

    return 1 if failed else 0


def _run_runtime_worker(*, tools: set[str] | None, json_output: bool) -> int:
    isolation_error = _worker_isolation_error()
    if isolation_error is not None:
        print(isolation_error, file=sys.stderr)
        return 2

    ensure_tool_registry_loaded()
    selected_tools, unknown = _select_tools(requested_tools=tools, available_tools=sorted(TOOL_REGISTRY))
    if unknown:
        print(f"Unknown tools requested: {', '.join(unknown)}", file=sys.stderr)
        return 2

    results = [_runtime_check_tool(tool_name) for tool_name in selected_tools]
    return _emit_runtime_results(results, json_output=json_output)


def _run_extra_worker(*, tools: set[str] | None, json_output: bool) -> int:
    isolation_error = _worker_isolation_error()
    if isolation_error is not None:
        print(isolation_error, file=sys.stderr)
        return 2

    ensure_tool_registry_loaded()
    selected_tools, unknown = _select_tools(requested_tools=tools, available_tools=sorted(_available_tool_extras()))
    if unknown:
        print(f"Unknown tools requested: {', '.join(unknown)}", file=sys.stderr)
        return 2

    results = [_extra_check_tool(tool_name) for tool_name in selected_tools]
    return _emit_extra_results(results, json_output=json_output)


def _run_runtime_host(args: argparse.Namespace) -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="mindroom-tool-auto-install-"))
    venv_dir = temp_dir / "venv"

    print(f"Creating isolated environment at {venv_dir}", flush=True)
    try:
        worker_python = _create_isolated_environment(venv_dir, args.python, install_editable=True)
        cmd = [str(worker_python), str(SCRIPT_PATH), "--worker", "--mode", "runtime-auto-install"]
        if args.json:
            cmd.append("--json")
        for tool_name in args.tool:
            cmd.extend(["--tool", tool_name])

        env = os.environ.copy()
        env["MINDROOM_PARENT_PREFIX"] = sys.prefix
        result = subprocess.run(cmd, check=False, cwd=PROJECT_ROOT, env=env)
        return result.returncode
    finally:
        if args.keep_env:
            print(f"Kept environment at {venv_dir}", flush=True)
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _build_wheel_for_extras(wheel_dir: Path) -> Path:
    cmd = ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)]
    result = subprocess.run(cmd, check=False, text=True, capture_output=True, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print("Failed to build wheel for extra-install mode.", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        msg = "uv build failed"
        raise RuntimeError(msg)

    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        msg = f"No wheel produced in {wheel_dir}"
        raise RuntimeError(msg)
    return wheels[0]


def _install_wheel_extra(*, worker_python: Path, wheel_path: Path, tool_name: str) -> subprocess.CompletedProcess[str]:
    package_spec = f"{wheel_path}[{tool_name}]"
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", str(worker_python), package_spec]
    else:
        cmd = [str(worker_python), "-m", "pip", "install", package_spec]
    return subprocess.run(cmd, check=False, text=True, capture_output=True, cwd=PROJECT_ROOT)


def _run_extra_host_for_tool(*, tool_name: str, wheel_path: Path, python: str, keep_env: bool) -> ExtraHostResult:
    temp_dir = Path(tempfile.mkdtemp(prefix=f"mindroom-extra-{tool_name}-"))
    venv_dir = temp_dir / "venv"

    try:
        worker_python = _create_isolated_environment(venv_dir, python, install_editable=False)
    except Exception as exc:
        if not keep_env:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return ExtraHostResult(tool=tool_name, status="failed", phase="venv", message=f"{type(exc).__name__}: {exc}")

    install_result = _install_wheel_extra(worker_python=worker_python, wheel_path=wheel_path, tool_name=tool_name)
    if install_result.returncode != 0:
        if not keep_env:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return ExtraHostResult(
            tool=tool_name,
            status="failed",
            phase="install",
            message=_trim_output(stdout=install_result.stdout, stderr=install_result.stderr),
        )

    cmd = [
        str(worker_python),
        str(SCRIPT_PATH),
        "--worker",
        "--mode",
        "extra-install",
        "--tool",
        tool_name,
    ]
    env = os.environ.copy()
    env["MINDROOM_PARENT_PREFIX"] = sys.prefix
    worker_result = subprocess.run(cmd, check=False, text=True, capture_output=True, cwd=PROJECT_ROOT, env=env)

    if keep_env:
        keep_note = f" (kept env: {venv_dir})"
    else:
        keep_note = ""
        shutil.rmtree(temp_dir, ignore_errors=True)

    status = "ok" if worker_result.returncode == 0 else "failed"
    message = _trim_output(stdout=worker_result.stdout, stderr=worker_result.stderr)
    if keep_note:
        message = f"{message}{keep_note}"
    return ExtraHostResult(tool=tool_name, status=status, phase="load", message=message)


def _emit_extra_host_summary(results: list[ExtraHostResult], *, json_output: bool) -> int:
    failed = [result for result in results if result.status != "ok"]
    payload = {
        "summary": {
            "total": len(results),
            "ok": len(results) - len(failed),
            "failed": len(failed),
        },
        "results": [asdict(result) for result in results],
    }

    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"\nSummary: total={payload['summary']['total']} "
            f"ok={payload['summary']['ok']} failed={payload['summary']['failed']}",
        )
        if failed:
            print("\nFailed tools:")
            for result in sorted(failed, key=lambda item: item.tool):
                print(f"- {result.tool} [{result.phase}] {result.message}")

    return 1 if failed else 0


def _run_extra_host(args: argparse.Namespace) -> int:
    if not shutil.which("uv"):
        print("extra-install mode requires 'uv' to build and install wheel extras.", file=sys.stderr)
        return 2

    requested = set(args.tool) if args.tool else None
    selected_tools, unknown = _select_tools(requested_tools=requested, available_tools=sorted(_available_tool_extras()))
    if unknown:
        print(f"Unknown tools requested: {', '.join(unknown)}", file=sys.stderr)
        return 2

    wheel_dir = Path(tempfile.mkdtemp(prefix="mindroom-tool-wheel-"))
    try:
        wheel_path = _build_wheel_for_extras(wheel_dir)
        if not args.json:
            print(f"Built wheel: {wheel_path.name}")
            print(f"Testing {len(selected_tools)} tool extras using {args.workers} worker(s)")

        results: list[ExtraHostResult] = []
        if args.workers > 1 and len(selected_tools) > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        _run_extra_host_for_tool,
                        tool_name=tool_name,
                        wheel_path=wheel_path,
                        python=args.python,
                        keep_env=args.keep_env,
                    ): tool_name
                    for tool_name in selected_tools
                }
                for index, future in enumerate(as_completed(futures), start=1):
                    result = future.result()
                    results.append(result)
                    if not args.json:
                        print(
                            f"[{index}/{len(selected_tools)}] {result.tool}: {result.status.upper()} ({result.phase})",
                        )
        else:
            for index, tool_name in enumerate(selected_tools, start=1):
                result = _run_extra_host_for_tool(
                    tool_name=tool_name,
                    wheel_path=wheel_path,
                    python=args.python,
                    keep_env=args.keep_env,
                )
                results.append(result)
                if not args.json:
                    print(f"[{index}/{len(selected_tools)}] {result.tool}: {result.status.upper()} ({result.phase})")

        return _emit_extra_host_summary(results, json_output=args.json)
    finally:
        shutil.rmtree(wheel_dir, ignore_errors=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["runtime-auto-install", "extra-install"],
        default="runtime-auto-install",
        help="Which dependency validation mode to run.",
    )
    parser.add_argument("--python", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    parser.add_argument("--tool", action="append", default=[], help="Only run this tool (repeatable)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (extra-install host mode only)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--keep-env", action="store_true", help="Do not delete the temporary environment")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    """Run the selected dependency validation mode."""
    args = _parse_args()
    if args.worker:
        selected = set(args.tool) if args.tool else None
        if args.mode == "runtime-auto-install":
            return _run_runtime_worker(tools=selected, json_output=args.json)
        return _run_extra_worker(tools=selected, json_output=args.json)

    if args.mode == "runtime-auto-install":
        return _run_runtime_host(args)
    return _run_extra_host(args)


if __name__ == "__main__":
    raise SystemExit(main())
