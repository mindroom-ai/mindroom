"""Replay logged turns through each recall arm, judge the hits, and write aggregate metrics.

    python -m evals.memory_recall.run --turns T.jsonl --memory-dir D --config ~/.mindroom-chat/config.yaml
        --arms A0,A1,A2,A3,A4 --out DIR [--key-env ENV] [--limit N]

Writes `DIR/results.json` and `DIR/results.md` (numbers only, never memory text), caches each arm's index under
`DIR/index/` and the judge's verdicts in `DIR/judge.sqlite`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.memory._prompting import format_memories_as_context
from mindroom.token_counting.families import load_o200k

from .arms import ARMS, replay
from .judge import JudgeCache, Verdict, judge_hits
from .turns import read_turns

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.memory._shared import MemoryResult

    from .turns import Turn

JUDGE_MODEL = "deepseek/deepseek-v4.1-flash-1017"
_JUDGE_CONCURRENCY = 16
_CONTEXT = FileMemoryBackend.context_label
_METRIC_COLUMNS = (
    "turns",
    "mean_tokens",
    "p95_tokens",
    "relevant_per_1k_tokens",
    "turns_with_relevant",
    "unrelated_only_turns",
    "abstained_turns",
    "judge_errors",
    "search_errors",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.memory_recall.run",
        description=(__doc__ or "").split("\n")[0],
    )
    parser.add_argument("--turns", type=Path, required=True, help="JSONL written by turns.write_turns")
    parser.add_argument("--memory-dir", type=Path, required=True, help="The agent's memory/ directory")
    parser.add_argument("--config", type=Path, required=True, help="MindRoom config.yaml with memory.embedder")
    parser.add_argument("--arms", default=",".join(ARMS), help="Comma-separated arm names (default: all)")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY", help="Env var holding the judge's API key")
    parser.add_argument("--limit", type=int, default=None, help="Replay only the first N turns")
    return parser


def main() -> None:
    """Run the benchmark from the command line."""
    asyncio.run(run_cli(sys.argv[1:]))


async def run_cli(argv: Sequence[str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Run the benchmark for `argv`; `transport` replaces the judge's network transport (tests)."""
    parser = _parser()
    args = parser.parse_args(argv)
    unknown = [name for name in args.arms.split(",") if name not in ARMS]
    if unknown:
        parser.error(f"unknown arms {unknown}; choose from {list(ARMS)}")
    api_key = os.environ.get(args.key_env, "")
    if not api_key:
        parser.error(f"set the judge's API key in ${args.key_env}")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    index_dir = out / "index"
    runtime_paths = resolve_runtime_paths(config_path=args.config, storage_path=index_dir)
    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    turns = read_turns(args.turns)[: args.limit]
    template = config.get_prompt("MEMORY_CONTEXT_PROMPT_TEMPLATE")

    results: dict[str, dict[str, float]] = {}
    with JudgeCache(out / "judge.sqlite") as cache:
        async with httpx.AsyncClient(transport=transport) as client:
            for name in args.arms.split(","):
                replayed = await replay(
                    turns,
                    ARMS[name],
                    memory_dir=args.memory_dir,
                    config=config,
                    runtime_paths=runtime_paths,
                    index_dir=index_dir,
                )
                # A turn whose search failed is left out of every metric but `search_errors`.
                searched = [(turn, hits) for turn, hits in zip(turns, replayed, strict=True) if hits is not None]
                hits = [turn_hits for _turn, turn_hits in searched]
                verdicts = await _judge_all(
                    [turn for turn, _hits in searched],
                    hits,
                    cache=cache,
                    client=client,
                    api_key=api_key,
                )
                results[name] = arm_metrics(
                    verdicts,
                    tokens=[
                        _count(format_memories_as_context(turn_hits, _CONTEXT, prompt_template=template))
                        for turn_hits in hits
                    ],
                    hit_tokens=[[_count(f"- {hit.get('memory', '')}\n") for hit in turn_hits] for turn_hits in hits],
                    search_errors=len(turns) - len(searched),
                )
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out / "results.md").write_text(_markdown(results))


def _count(text: str) -> int:
    return len(load_o200k().encode_ordinary(text))


async def _judge_all(
    turns: Sequence[Turn],
    hits: Sequence[Sequence[MemoryResult]],
    *,
    cache: JudgeCache,
    client: httpx.AsyncClient,
    api_key: str,
) -> list[list[Verdict | None]]:
    """Return the judge's verdict on each injected hit, turn by turn; turns that injected nothing need no request."""
    semaphore = asyncio.Semaphore(_JUDGE_CONCURRENCY)

    async def judge_turn(turn: Turn, turn_hits: Sequence[MemoryResult]) -> list[Verdict | None]:
        if not turn_hits:
            return []
        async with semaphore:
            return await judge_hits(
                turn.query,
                [hit.get("memory", "") for hit in turn_hits],
                cache=cache,
                client=client,
                model=JUDGE_MODEL,
                api_key=api_key,
            )

    return list(
        await asyncio.gather(*(judge_turn(turn, turn_hits) for turn, turn_hits in zip(turns, hits, strict=True))),
    )


def arm_metrics(
    verdicts: Sequence[Sequence[Verdict | None]],
    *,
    tokens: Sequence[int],
    hit_tokens: Sequence[Sequence[int]],
    search_errors: int = 0,
) -> dict[str, float]:
    """Return one arm's metrics from each searched turn's verdicts, injected o200k tokens, and per-hit tokens.

    `search_errors` counts the turns whose search failed; they are not among the turns given, so no other metric
    sees them.

    A None verdict (the judge failed) counts only in `judge_errors`: its hit's tokens leave the relevance-per-token
    denominator, and a turn whose every hit errored leaves the turn shares, since nothing about it was judged.
    """
    turns = len(verdicts)
    errors = sum(verdict is None for turn in verdicts for verdict in turn)
    relevant = sum(verdict is not None and verdict.relevance == 2 for turn in verdicts for verdict in turn)
    errored_tokens = sum(
        count
        for turn, counts in zip(verdicts, hit_tokens, strict=True)
        for verdict, count in zip(turn, counts, strict=True)
        if verdict is None
    )
    judged_tokens = sum(tokens) - errored_tokens
    judged = [[verdict for verdict in turn if verdict is not None] for turn in verdicts]
    # Turns that injected nothing, or got at least one verdict.
    rated = [turn_judged for turn, turn_judged in zip(verdicts, judged, strict=True) if not turn or turn_judged]
    return {
        "turns": turns,
        "mean_tokens": sum(tokens) / turns if turns else 0.0,
        "p95_tokens": _nearest_rank(tokens, 0.95),
        "relevant_per_1k_tokens": relevant / (judged_tokens / 1000) if judged_tokens else 0.0,
        "turns_with_relevant": _share(rated, lambda turn: any(verdict.relevance == 2 for verdict in turn)),
        "unrelated_only_turns": _share(
            rated,
            lambda turn: bool(turn) and all(verdict.relevance == 0 for verdict in turn),
        ),
        "abstained_turns": sum(not turn for turn in verdicts) / turns if turns else 0.0,
        "judge_errors": errors,
        "search_errors": search_errors,
    }


def _share(turns: Sequence[Sequence[Verdict]], predicate: Callable[[Sequence[Verdict]], bool]) -> float:
    return sum(bool(predicate(turn)) for turn in turns) / len(turns) if turns else 0.0


def _nearest_rank(values: Sequence[int], quantile: float) -> int:
    """Return the nearest-rank `quantile` of `values`, or 0 for none."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(math.ceil(quantile * len(ordered)), 1) - 1]


def _markdown(results: dict[str, dict[str, float]]) -> str:
    """Return the metrics as a Markdown table, one row per arm."""
    lines = [
        "| arm | " + " | ".join(_METRIC_COLUMNS) + " |",
        "|---|" + "---:|" * len(_METRIC_COLUMNS),
    ]
    for name, metrics in results.items():
        cells = [
            f"{metrics[column]:.3f}" if isinstance(metrics[column], float) else str(metrics[column])
            for column in _METRIC_COLUMNS
        ]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
