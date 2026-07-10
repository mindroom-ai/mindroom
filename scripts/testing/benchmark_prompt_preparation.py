"""Benchmark pre-model prompt preparation with deterministic branch delays."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import structlog
from agno.models.message import Message

import mindroom.ai as ai_module
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import PreparedHistoryState
from mindroom.memory import MemoryPromptParts
from mindroom.model_defaults import CONFIG_INIT_MODEL_PRESETS
from mindroom.response_turn import ResponseTurnContext

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.constants import RuntimePaths


def _nearest_rank(sorted_samples: Sequence[float], percentile: float) -> float:
    index = max(math.ceil((percentile / 100) * len(sorted_samples)) - 1, 0)
    return sorted_samples[min(index, len(sorted_samples) - 1)]


def _summarize_samples(samples: Sequence[float]) -> dict[str, float | int]:
    sorted_samples = sorted(samples)
    return {
        "count": len(samples),
        "mean_ms": round(sum(samples) / len(samples), 3),
        "p50_ms": round(_nearest_rank(sorted_samples, 50), 3),
        "p95_ms": round(_nearest_rank(sorted_samples, 95), 3),
        "max_ms": round(sorted_samples[-1], 3),
    }


async def _run_sample(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    memory_delay_s: float,
    agent_delay_s: float,
    history_delay_s: float,
) -> None:
    async def _prepare_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        await asyncio.sleep(memory_delay_s)
        return MemoryPromptParts(session_preamble="stable memory", turn_context="turn memory")

    def _build_agent(*_args: object, **_kwargs: object) -> SimpleNamespace:
        time.sleep(agent_delay_s)
        return SimpleNamespace(additional_context="")

    async def _prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(history_delay_s)
        return SimpleNamespace(
            prepared_history=PreparedHistoryState(),
            replay_plan=None,
            unseen_event_ids=[],
            messages=(Message(role="user", content=cast("str", kwargs["prompt"])),),
        )

    ctx = ResponseTurnContext(
        entity_label="benchmark",
        session_id="benchmark-session",
        run_id=None,
        correlation_id="benchmark-correlation",
        reply_to_event_id=None,
        room_id=None,
        thread_id=None,
        requester_id=None,
        matrix_run_metadata=None,
    )
    with (
        patch.object(ai_module, "build_memory_prompt_parts", _prepare_memory),
        patch.object(ai_module, "create_agent", _build_agent),
        patch.object(ai_module, "prepare_agent_execution_context", _prepare_history),
    ):
        prepared = await ai_module._prepare_agent_and_prompt(
            ctx,
            prompt="fixed prompt",
            runtime_paths=runtime_paths,
            config=config,
            model_prompt="fixed metadata",
            current_timestamp_ms=1_700_000_000_000.0,
        )
    expected_prompt = "fixed prompt\n\nturn memory\n\nfixed metadata"
    if prepared.prompt_text != expected_prompt:
        msg = f"unexpected prepared prompt: {prepared.prompt_text!r}"
        raise AssertionError(msg)


async def _benchmark(args: argparse.Namespace) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="mindroom-prompt-preparation-benchmark-") as tmp:
        root = Path(tmp)
        runtime_paths = resolve_runtime_paths(
            config_path=root / "config.yaml",
            storage_path=root / "storage",
            process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
        )
        default_model = CONFIG_INIT_MODEL_PRESETS["openai"]
        config = Config.model_validate(
            {
                "models": {"default": default_model.to_config_dict()},
                "agents": {"benchmark": {"display_name": "Benchmark", "role": "Benchmark prompt preparation"}},
            },
        )
        delays = {
            "memory_delay_ms": args.memory_ms,
            "agent_delay_ms": args.agent_ms,
            "history_delay_ms": args.history_ms,
        }
        sample_kwargs = {
            "config": config,
            "runtime_paths": runtime_paths,
            "memory_delay_s": args.memory_ms / 1000,
            "agent_delay_s": args.agent_ms / 1000,
            "history_delay_s": args.history_ms / 1000,
        }
        for _ in range(args.warmup):
            await _run_sample(**sample_kwargs)

        samples: list[float] = []
        for _ in range(args.iterations):
            started_at = time.perf_counter()
            await _run_sample(**sample_kwargs)
            samples.append((time.perf_counter() - started_at) * 1000)

    return {
        "case": "pre_model_prompt_preparation",
        "warmup": args.warmup,
        **delays,
        **_summarize_samples(samples),
    }


def main() -> None:
    """Run benchmark and print one JSON summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--memory-ms", type=float, default=700.0)
    parser.add_argument("--agent-ms", type=float, default=1000.0)
    parser.add_argument("--history-ms", type=float, default=600.0)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")
    if min(args.memory_ms, args.agent_ms, args.history_ms) < 0:
        parser.error("branch delays must be >= 0")

    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("mindroom").setLevel(logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        cache_logger_on_first_use=False,
    )
    print(json.dumps(asyncio.run(_benchmark(args)), sort_keys=True))


if __name__ == "__main__":
    main()
