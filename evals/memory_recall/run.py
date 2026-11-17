"""Replay logged turns through each recall arm, judge the hits, and write aggregate metrics.

    python -m evals.memory_recall.run --turns T.jsonl --memory-dir D --config ~/.mindroom-chat/config.yaml
        --arms A0,A1,A2,A3,A4,shipped,shipped_default,A5 --out DIR [--key-env ENV] [--limit N]
        [--thresholds 0.3,0.5,0.7,0.8] [--split-seed N] [--judge-model M] [--latency-turns N]

Writes `DIR/results.json` and `DIR/results.md` (numbers only, never memory text) for all turns and for a seeded
tuning half and its held-out other half, caches each arm's index under `DIR/index/`, the judge's verdicts in
`DIR/judge.sqlite` and reranker scores in `DIR/rerank.sqlite`. A rerank arm gives one row per threshold, `A5@0.5`;
with `--latency-turns N` it also times N uncached reranker calls, one at a time, in a separate latency table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
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
from .rerank_arms import RERANKERS, LatencyCall, TurnRerank, measure_latency, rerank_turns, select_reranked
from .rerank_cache import CachedReranker, RerankCache
from .turns import read_turns

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.memory._shared import MemoryResult

    from .arms import Arm
    from .turns import Turn

#: One results row: its name, each turn's injected hits (None for a failed search) and reranker calls, if any.
type _Row = tuple[str, list[list[MemoryResult] | None], list[TurnRerank | None] | None]

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
    "rerank_errors",
    "usd_per_turn_estimate",
)
_LATENCY_COLUMNS = ("latency_turns", "latency_added_p50_ms", "latency_added_p95_ms", "latency_fallback_rate")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.memory_recall.run",
        description=(__doc__ or "").split("\n")[0],
    )
    parser.add_argument("--turns", type=Path, required=True, help="JSONL written by turns.write_turns")
    parser.add_argument("--memory-dir", type=Path, required=True, help="The agent's memory/ directory")
    parser.add_argument("--config", type=Path, required=True, help="MindRoom config.yaml with memory.embedder")
    parser.add_argument(
        "--arms",
        default=",".join(name for name, arm in ARMS.items() if arm.reranker is None),
        help="Comma-separated arm names (default: every arm without a reranker)",
    )
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY", help="Env var holding the judge's API key")
    parser.add_argument("--limit", type=int, default=None, help="Replay only the first N turns")
    parser.add_argument(
        "--thresholds",
        default="0.3,0.5,0.7,0.8",
        help="Comma-separated minimum reranker scores each rerank arm is measured at, from one set of scores",
    )
    parser.add_argument("--split-seed", type=int, default=0, help="Seed of the tuning and held-out halves")
    parser.add_argument(
        "--judge-model",
        default=JUDGE_MODEL,
        help=f"OpenRouter model that judges the hits (default: {JUDGE_MODEL}); verdicts are cached per model",
    )
    parser.add_argument(
        "--latency-turns",
        type=int,
        default=0,
        help="Also time this many uncached reranker calls of each rerank arm, one turn at a time (default: none)",
    )
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
    # Every token count and budget is exact: `select_recall_hits` estimates tokens until this encoder has loaded.
    load_o200k()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    index_dir = out / "index"
    runtime_paths = resolve_runtime_paths(config_path=args.config, storage_path=index_dir)
    config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    thresholds = [float(value) for value in args.thresholds.split(",")]
    turns = read_turns(args.turns)[: args.limit]
    template = config.get_prompt("MEMORY_CONTEXT_PROMPT_TEMPLATE")
    tuning, held_out = split_turns(len(turns), seed=args.split_seed)
    splits = {"all": list(range(len(turns))), "tuning": tuning, "held_out": held_out}

    results: dict[str, dict[str, dict[str, float]]] = {split: {} for split in splits}
    latency: dict[str, dict[str, float | None]] = {}
    with JudgeCache(out / "judge.sqlite") as cache, RerankCache(out / "rerank.sqlite") as rerank_cache:
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
                rows, calls = await _arm_rows(
                    name,
                    ARMS[name],
                    turns,
                    replayed,
                    thresholds=thresholds,
                    rerank_cache=rerank_cache,
                    runtime_paths=runtime_paths,
                    latency_turns=args.latency_turns,
                )
                if calls is not None:
                    latency[name] = latency_metrics(calls)
                for row, selected, reranked in rows:
                    # A turn whose search failed is left out of every metric but `search_errors`.
                    hits = {index: turn_hits for index, turn_hits in enumerate(selected) if turn_hits is not None}
                    verdicts = await _judge_all(
                        [turns[index] for index in hits],
                        list(hits.values()),
                        cache=cache,
                        client=client,
                        model=args.judge_model,
                        api_key=api_key,
                    )
                    judged = dict(zip(hits, verdicts, strict=True))
                    tokens = {
                        index: _count(format_memories_as_context(turn_hits, _CONTEXT, prompt_template=template))
                        for index, turn_hits in hits.items()
                    }
                    hit_tokens = {
                        index: [_count(f"- {hit.get('memory', '')}\n") for hit in turn_hits]
                        for index, turn_hits in hits.items()
                    }
                    for split, indices in splits.items():
                        kept = [index for index in indices if index in hits]
                        results[split][row] = arm_metrics(
                            [judged[index] for index in kept],
                            tokens=[tokens[index] for index in kept],
                            hit_tokens=[hit_tokens[index] for index in kept],
                            search_errors=len(indices) - len(kept),
                        ) | rerank_metrics([reranked[index] for index in indices] if reranked else [])
    report: dict[str, object] = {"judge_model": args.judge_model, "split_seed": args.split_seed, "splits": results}
    sections = [f"## {split}\n\n{_markdown(rows, _METRIC_COLUMNS)}" for split, rows in results.items()]
    if latency:
        report["latency"] = latency
        sections.append(f"## latency (uncached, one turn at a time)\n\n{_markdown(latency, _LATENCY_COLUMNS)}")
    (out / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "results.md").write_text(
        f"Judge: `{args.judge_model}`, split seed {args.split_seed}\n\n" + "\n".join(sections),
    )


def split_turns(count: int, *, seed: int) -> tuple[list[int], list[int]]:
    """Return the turn indices of a seeded tuning half and of the held-out rest, each in order."""
    indices = list(range(count))
    random.Random(seed).shuffle(indices)  # noqa: S311 - a reproducible split, not a secret
    return sorted(indices[: count // 2]), sorted(indices[count // 2 :])


async def _arm_rows(
    name: str,
    arm: Arm,
    turns: Sequence[Turn],
    replayed: list[list[MemoryResult] | None],
    *,
    thresholds: Sequence[float],
    rerank_cache: RerankCache,
    runtime_paths: RuntimePaths,
    latency_turns: int,
) -> tuple[list[_Row], list[LatencyCall] | None]:
    """Return the arm's rows and, for a rerank arm with `latency_turns`, its timed uncached calls.

    The rows are the arm itself, or for a rerank arm one row per threshold, all from one scoring of the turns.
    """
    if arm.reranker is None:
        return [(name, replayed, None)], None
    spec = RERANKERS[arm.reranker]
    inner = spec.create(runtime_paths)
    reranker = CachedReranker(
        inner,
        rerank_cache,
        provider=spec.provider,
        model=spec.model,
        rubric_version=spec.rubric_version,
    )
    reranked = await rerank_turns(turns, replayed, reranker, spec)
    calls = await measure_latency(inner, turns, replayed, count=latency_turns) if latency_turns else None
    rows: list[_Row] = [
        (
            f"{name}@{threshold:g}",
            [
                None
                if candidates is None
                else select_reranked(candidates, None if call is None else call.scores, threshold=threshold)
                for candidates, call in zip(replayed, reranked, strict=True)
            ],
            reranked,
        )
        for threshold in thresholds
    ]
    return rows, calls


def _count(text: str) -> int:
    return len(load_o200k().encode_ordinary(text))


async def _judge_all(
    turns: Sequence[Turn],
    hits: Sequence[Sequence[MemoryResult]],
    *,
    cache: JudgeCache,
    client: httpx.AsyncClient,
    model: str,
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
                model=model,
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


def rerank_metrics(reranked: Sequence[TurnRerank | None]) -> dict[str, float]:
    """Return the turns whose reranker call failed (they kept the hybrid selection) and the mean estimated price.

    The price counts only turns that called the reranker; both are 0 for an arm without one.
    """
    calls = [call for call in reranked if call is not None]
    return {
        "rerank_errors": sum(call.scores is None for call in calls),
        "usd_per_turn_estimate": sum(call.usd for call in calls) / len(calls) if calls else 0.0,
    }


def latency_metrics(calls: Sequence[LatencyCall]) -> dict[str, float | None]:
    """Return the timed calls' count, the p50 and p95 wait (ms) they add to a turn, and their fallback share.

    Without any timed call the latency and fallback values are None (missing), never 0.
    """
    added = [call.added_seconds * 1000 for call in calls]
    return {
        "latency_turns": len(calls),
        "latency_added_p50_ms": _nearest_rank(added, 0.5) if added else None,
        "latency_added_p95_ms": _nearest_rank(added, 0.95) if added else None,
        "latency_fallback_rate": sum(call.fallback for call in calls) / len(calls) if calls else None,
    }


def _share(turns: Sequence[Sequence[Verdict]], predicate: Callable[[Sequence[Verdict]], bool]) -> float:
    return sum(bool(predicate(turn)) for turn in turns) / len(turns) if turns else 0.0


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    """Return the nearest-rank `quantile` of `values`, or 0 for none."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(math.ceil(quantile * len(ordered)), 1) - 1]


def _markdown(results: Mapping[str, Mapping[str, float | None]], columns: Sequence[str]) -> str:
    """Return the metrics as a Markdown table, one row per arm; a missing value shows as `n/a`."""
    lines = [
        "| arm | " + " | ".join(columns) + " |",
        "|---|" + "---:|" * len(columns),
    ]
    for name, metrics in results.items():
        cells = [_cell(metrics[column]) for column in columns]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _cell(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


if __name__ == "__main__":
    main()
