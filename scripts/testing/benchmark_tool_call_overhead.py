"""Benchmark MindRoom hooks, local shell calls, and sandbox dispatch overhead."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import secrets
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import structlog

from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


async def _noop_async(value: int) -> int:
    return value


def _noop_sync(value: int) -> int:
    return value


@hook(EVENT_TOOL_BEFORE_CALL)
async def _benchmark_before_hook(ctx: object) -> None:
    assert isinstance(ctx, ToolBeforeCallContext)
    if ctx.tool_name != "noop":
        msg = f"unexpected benchmark tool: {ctx.tool_name}"
        raise AssertionError(msg)


@hook(EVENT_TOOL_AFTER_CALL)
async def _benchmark_after_hook(ctx: object) -> None:
    assert isinstance(ctx, ToolAfterCallContext)
    if ctx.tool_name != "noop":
        msg = f"unexpected benchmark tool: {ctx.tool_name}"
        raise AssertionError(msg)


def _registry_with_hooks() -> HookRegistry:
    return HookRegistry.from_plugins(
        [
            SimpleNamespace(
                name="tool-call-benchmark",
                discovered_hooks=(_benchmark_before_hook, _benchmark_after_hook),
                entry_config=PluginEntryConfig(path="./scripts/testing/benchmark_tool_call_overhead.py", settings={}),
                plugin_order=0,
            ),
        ],
    )


def _nearest_rank(sorted_samples: Sequence[float], percentile: float) -> float:
    if not sorted_samples:
        return 0.0
    index = max(math.ceil((percentile / 100) * len(sorted_samples)) - 1, 0)
    return sorted_samples[min(index, len(sorted_samples) - 1)]


def summarize_samples(samples: Sequence[float]) -> dict[str, float | int]:
    """Return deterministic summary stats for elapsed millisecond samples."""
    if not samples:
        return {"count": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    sorted_samples = sorted(samples)
    return {
        "count": len(samples),
        "mean_ms": round(sum(samples) / len(samples), 3),
        "p50_ms": round(_nearest_rank(sorted_samples, 50), 3),
        "p95_ms": round(_nearest_rank(sorted_samples, 95), 3),
        "max_ms": round(sorted_samples[-1], 3),
    }


async def _run_case(
    *,
    label: str,
    func: Callable[..., Any],
    hook_registry: HookRegistry,
    iterations: int,
    warmup: int,
) -> dict[str, object]:
    bridge = build_tool_hook_bridge(hook_registry, agent_name="benchmark")
    for _ in range(warmup):
        await bridge("noop", func, {"value": 1})

    samples: list[float] = []
    for _ in range(iterations):
        started_at = time.perf_counter()
        await bridge("noop", func, {"value": 1})
        samples.append((time.perf_counter() - started_at) * 1000)
    return {"case": label, **summarize_samples(samples)}


async def _run_benchmark(iterations: int, warmup: int) -> list[dict[str, object]]:
    return [
        await _run_case(
            label="async_no_hooks",
            func=_noop_async,
            hook_registry=HookRegistry.empty(),
            iterations=iterations,
            warmup=warmup,
        ),
        await _run_case(
            label="sync_no_hooks",
            func=_noop_sync,
            hook_registry=HookRegistry.empty(),
            iterations=iterations,
            warmup=warmup,
        ),
        await _run_case(
            label="async_with_hooks",
            func=_noop_async,
            hook_registry=_registry_with_hooks(),
            iterations=iterations,
            warmup=warmup,
        ),
        await _run_case(
            label="sync_with_hooks",
            func=_noop_sync,
            hook_registry=_registry_with_hooks(),
            iterations=iterations,
            warmup=warmup,
        ),
    ]


def _write_shell_benchmark_config(root: Path) -> Path:
    """Create an isolated config that cannot call a provider or access real agent state."""
    path = root / "config.yaml"
    path.write_text(
        "models:\n  default:\n    provider: openai\n    id: unused-benchmark-model\n"
        "agents:\n  benchmark:\n    display_name: Benchmark\n    model: default\n    tools: [shell]\n"
        "router:\n  model: default\nmemory:\n  backend: file\n",
        encoding="utf-8",
    )
    return path


async def _run_shell_benchmark(iterations: int, warmup: int) -> list[dict[str, object]]:
    """Measure the real local shell toolkit with argv and command-string inputs."""
    from mindroom.constants import resolve_runtime_paths  # noqa: PLC0415
    from mindroom.tools.shell import shell_tools  # noqa: PLC0415

    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="mindroom-shell-benchmark-") as tmp:
        root = Path(tmp)
        runtime = resolve_runtime_paths(
            config_path=_write_shell_benchmark_config(root),
            storage_path=root / "storage",
            process_env={},
        )
        toolkit = shell_tools()(runtime_paths=runtime)
        for label, command in (("shell_argv", ["printf", "benchmark-ok"]), ("shell_string", "printf benchmark-ok")):
            samples: list[float] = []
            for index in range(warmup + iterations):
                started = time.perf_counter()
                result = await toolkit.run_shell_command(command)
                elapsed = (time.perf_counter() - started) * 1000
                if result != "benchmark-ok":
                    msg = f"Shell benchmark returned unexpected output: {result}"
                    raise RuntimeError(msg)
                if index >= warmup:
                    samples.append(elapsed)
            results.append({"case": label, **summarize_samples(samples)})
    return results


def _run_docker_shell_benchmark(image: str, iterations: int, warmup: int) -> list[dict[str, object]]:
    """Measure a disposable Docker worker with empty credentials and isolated storage."""
    from mindroom.constants import resolve_runtime_paths  # noqa: PLC0415
    from mindroom.tool_system.worker_proxy_client import (  # noqa: PLC0415
        WorkerProxyClientConfig,
        execute_worker_proxy_request,
    )
    from mindroom.workers.backends.docker import DockerWorkerBackend  # noqa: PLC0415
    from mindroom.workers.models import WorkerSpec  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="mindroom-docker-benchmark-") as tmp:
        root = Path(tmp)
        runtime = resolve_runtime_paths(
            config_path=_write_shell_benchmark_config(root),
            storage_path=root / "storage",
            process_env={
                "MINDROOM_WORKER_BACKEND": "docker",
                "MINDROOM_DOCKER_WORKER_IMAGE": image,
                "MINDROOM_DOCKER_WORKER_NAME_PREFIX": "mindroom-tool-benchmark",
                "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "forkserver",
            },
        )
        manager = DockerWorkerBackend.from_runtime(
            runtime,
            auth_token=secrets.token_urlsafe(32),
            worker_grantable_credentials=frozenset(),
        )
        spec = WorkerSpec("v1:default:unscoped:benchmark", mirrored_credential_services=frozenset())
        proxy = WorkerProxyClientConfig(
            proxy_url=None,
            proxy_token=None,
            proxy_timeout_seconds=120,
            credential_lease_ttl_seconds=60,
            credential_policy={},
        )
        samples: dict[str, list[float]] = {"docker_ensure": [], "docker_execute": [], "docker_total": []}
        try:
            started = time.perf_counter()
            manager.ensure_worker(spec)
            cold_ready_ms = (time.perf_counter() - started) * 1000
            for index in range(warmup + iterations):
                started = time.perf_counter()
                handle = manager.ensure_worker(spec)
                ready = time.perf_counter()
                result = execute_worker_proxy_request(
                    config=proxy,
                    payload={
                        "tool_name": "shell",
                        "function_name": "run_shell_command",
                        "kwargs": {"args": ["printf", "benchmark-ok"]},
                        "worker_key": spec.worker_key,
                        "routing_agent_name": "benchmark",
                    },
                    credentials_manager=None,
                    tool_name="shell",
                    function_name="run_shell_command",
                    worker_target=None,
                    worker_handle=handle,
                    worker_manager=manager,
                )
                finished = time.perf_counter()
                if not isinstance(result, str) or result.splitlines()[-1:] != ["benchmark-ok"]:
                    msg = f"Docker shell benchmark returned unexpected output: {result}"
                    raise RuntimeError(msg)
                if index >= warmup:
                    samples["docker_ensure"].append((ready - started) * 1000)
                    samples["docker_execute"].append((finished - ready) * 1000)
                    samples["docker_total"].append((finished - started) * 1000)
            results: list[dict[str, object]] = [
                {"case": "docker_cold_ready", "elapsed_ms": round(cold_ready_ms, 3)},
            ]
            for name, values in samples.items():
                results.append({"case": name, **summarize_samples(values)})
            return results
        finally:
            manager.shutdown()


def _run_sandbox_dispatch_case(mode: str, iterations: int, warmup: int) -> dict[str, object]:
    """Benchmark one worker-routed sandbox dispatch mode with a trivial tool call."""
    # Imported here so the lightweight hook benchmark does not pay the full
    # runtime import that this case exists to measure.
    from mindroom.api import sandbox_runner  # noqa: PLC0415
    from mindroom.constants import resolve_primary_runtime_paths  # noqa: PLC0415
    from mindroom.model_defaults import CONFIG_INIT_MODEL_PRESETS  # noqa: PLC0415

    default_model = CONFIG_INIT_MODEL_PRESETS["openai"]
    with tempfile.TemporaryDirectory(prefix="mindroom-dispatch-benchmark-") as tmp:
        config_path = Path(tmp) / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n"
            f"    provider: {default_model.provider}\n"
            f"    id: {default_model.id}\n"
            "agents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        runtime_paths = resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=Path(tmp) / "storage",
            process_env={"MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": mode},
        )
        config = sandbox_runner._runtime_config_or_empty(runtime_paths)

        def _call() -> None:
            response = sandbox_runner._execute_request_subprocess_sync(
                sandbox_runner.SandboxRunnerExecuteRequest(
                    tool_name="calculator",
                    function_name="add",
                    args=[1, 2],
                    kwargs={},
                ),
                runtime_paths,
                config,
            )
            if not response.ok:
                msg = f"sandbox dispatch failed: {response.error}"
                raise RuntimeError(msg)

        for _ in range(warmup):
            _call()
        samples: list[float] = []
        for _ in range(iterations):
            started_at = time.perf_counter()
            _call()
            samples.append((time.perf_counter() - started_at) * 1000)
    return {"case": f"sandbox_dispatch_{mode}", **summarize_samples(samples)}


def main() -> None:
    """Run the command-line benchmark and print JSON results."""
    parser = argparse.ArgumentParser(description="Benchmark MindRoom tool-call bridge and sandbox dispatch overhead.")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=50)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--shell",
        action="store_true",
        help="Benchmark real local shell calls (argv and command strings).",
    )
    modes.add_argument(
        "--docker-image",
        help="Benchmark shell calls in an isolated Docker worker using this local image.",
    )
    modes.add_argument(
        "--sandbox-dispatch",
        nargs="+",
        choices=["forkserver", "subprocess"],
        default=None,
        help=(
            "Benchmark worker-routed sandbox dispatch for the given execution modes "
            "instead of the hook bridge. Each spawn-per-call iteration pays the full "
            "runtime import, so pass a small --iterations (e.g. 5) and --warmup 1."
        ),
    )
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")

    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("mindroom").setLevel(logging.WARNING)
    logging.getLogger("mindroom.hooks").disabled = True
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        cache_logger_on_first_use=False,
    )
    if args.docker_image:
        results = _run_docker_shell_benchmark(args.docker_image, args.iterations, args.warmup)
    elif args.shell:
        results = asyncio.run(_run_shell_benchmark(args.iterations, args.warmup))
    elif args.sandbox_dispatch:
        results = [_run_sandbox_dispatch_case(mode, args.iterations, args.warmup) for mode in args.sandbox_dispatch]
    else:
        results = asyncio.run(_run_benchmark(iterations=args.iterations, warmup=args.warmup))
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
