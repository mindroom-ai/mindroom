"""Synthetic benchmark for MindRoom tool-call bridge overhead."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
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
async def _benchmark_before_hook(ctx: ToolBeforeCallContext) -> None:
    if ctx.tool_name != "noop":
        msg = f"unexpected benchmark tool: {ctx.tool_name}"
        raise AssertionError(msg)


@hook(EVENT_TOOL_AFTER_CALL)
async def _benchmark_after_hook(ctx: ToolAfterCallContext) -> None:
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


def main() -> None:
    """Run the command-line benchmark and print JSON results."""
    parser = argparse.ArgumentParser(description="Benchmark MindRoom tool-call bridge overhead.")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=50)
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
    results = asyncio.run(_run_benchmark(iterations=args.iterations, warmup=args.warmup))
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
