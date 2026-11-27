"""CLI runner for MindRoom's full-surface tool-calling evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.config.main import Config, load_config
from mindroom.config.models import DebugConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.logging_config import use_library_logging
from mindroom.providers.registry import build_model_provider

from .cases import CASES, CASES_BY_ID
from .harness import CaseResult, evaluate_model

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.llm.provider import ModelProvider

_DEFAULT_CONFIG = Path.home() / ".mindroom-chat" / "config.yaml"
_DEFAULT_BASE_URL = "http://llama.local/v1"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals.tool_calling.run", description="MindRoom tool-calling eval.")
    p.add_argument("--model", action="append", required=True, help="Config model name, openrouter:<id>, or local:<id>.")
    p.add_argument("--config", type=Path, default=_DEFAULT_CONFIG if _DEFAULT_CONFIG.is_file() else None)
    p.add_argument("--base-url", default=_DEFAULT_BASE_URL, help="OpenAI-compatible base URL for local:<id>.")
    p.add_argument("--cases", default=None, help="Optional comma-separated case IDs.")
    p.add_argument("--max-turns", type=int, default=5, help="Max model requests per case.")
    p.add_argument("--concurrency", type=int, default=1, help="Concurrent cases per model.")
    p.add_argument("--out", type=Path, default=None, help="Optional JSON output path.")
    p.add_argument("-v", "--verbose", action="store_true", help="Show MindRoom INFO/DEBUG logs on stderr.")
    return p


def _configure_cli_logging(*, verbose: bool) -> None:
    """Route MindRoom logs to stderr at WARNING by default (DEBUG with ``--verbose``)."""
    use_library_logging()
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s", force=True)
    logging.getLogger("mindroom").setLevel(level)


def resolve_provider(model_spec: str, *, config_path: Path | None, base_url: str) -> ModelProvider:
    """Build a MindRoom ``ModelProvider`` from ``local:<id>``, ``openrouter:<id>``, or a config model name."""
    if model_spec.startswith(("local:", "openrouter:")):
        is_local = model_spec.startswith("local:")
        model_dict: dict[str, Any] = (
            {
                "provider": "openai",
                "id": model_spec.removeprefix("local:"),
                "extra_kwargs": {"base_url": base_url, "api_key": "sk-no-key-required"},
            }
            if is_local
            else {"provider": "openrouter", "id": model_spec.removeprefix("openrouter:")}
        )
        cfg_file = None if is_local else (config_path if config_path and config_path.is_file() else None)
        tmp = Path(tempfile.gettempdir()) / "mindroom-tool-calling-eval"
        rp = (
            resolve_runtime_paths(config_path=cfg_file)
            if cfg_file
            else resolve_runtime_paths(config_path=tmp / "c.yaml", storage_path=tmp / "s")
        )
        authored = {
            "defaults": {"tools": []},
            "agents": {"eval_agent": {"display_name": "Eval", "role": "Eval"}},
            "memory": {"backend": "none"},
            "models": {"default": model_dict},
            "timezone": "UTC",
        }
        return build_model_provider(Config.validate_with_runtime(authored, rp), rp, "default")
    if config_path is None or not config_path.is_file():
        msg = f"Configured model {model_spec!r} requires an existing --config file (got {config_path})."
        raise ValueError(msg)
    rp = resolve_runtime_paths(config_path=config_path)
    config = load_config(rp, tolerate_plugin_load_errors=True).model_copy(update={"debug": DebugConfig()})
    return build_model_provider(config, rp, model_spec)


def _summarize(model: str, results: Sequence[CaseResult], elapsed_s: float) -> dict[str, Any]:
    failures = Counter[str]()
    for r in results:
        failures.update(r.failure_counts)
    first_try, passed = sum(r.first_try_passed for r in results), sum(r.passed for r in results)
    return {
        "model": model,
        "first_try_passed": first_try,
        "passed": passed,
        "recovered": passed - first_try,
        "total": len(results),
        "mis_calls": sum(r.mis_calls for r in results),
        "failure_counts": dict(sorted(failures.items())),
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "elapsed_s": round(elapsed_s, 3),
        "cases": [asdict(r) for r in results],
    }


def format_markdown_table(summaries: Sequence[dict[str, Any]]) -> str:
    """Format model summaries into a compact Markdown table."""
    rows = [
        "| Model | First-Try | Recovered | Mis-Calls | Failure Breakdown | Tokens | Elapsed |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in summaries:
        rec = (
            f"{s['passed']}/{s['total']}" if s["recovered"] == 0 else f"{s['passed']}/{s['total']} (+{s['recovered']})"
        )
        brk = (
            ", ".join(f"{k}={v}" for k, v in sorted(s["failure_counts"].items(), key=lambda kv: (-kv[1], kv[0])))
            or "none"
        )
        tok = f"{s['input_tokens'] + s['output_tokens']:,}"
        rows.append(
            f"| {s['model']} | {s['first_try_passed']}/{s['total']} | {rec} | {s['mis_calls']} | {brk} | {tok} | {s['elapsed_s']:.1f}s |",
        )
    return "\n".join(rows)


async def run_cli(
    argv: Sequence[str],
    *,
    provider_factory: Callable[[str], ModelProvider] | None = None,
    configure_logging: bool = False,
) -> dict[str, Any]:
    """Run the eval CLI for ``argv`` and return the JSON-serializable report."""
    parser = _parser()
    args = parser.parse_args(argv)
    if configure_logging:
        _configure_cli_logging(verbose=args.verbose)
    models = list(dict.fromkeys(x.strip() for entry in args.model for x in entry.split(",") if x.strip()))
    if not models:
        parser.error("at least one --model is required")
    if args.cases:
        selected = [x.strip() for x in args.cases.split(",") if x.strip()]
        if unknown := [cid for cid in selected if cid not in CASES_BY_ID]:
            parser.error(f"unknown cases {unknown}; choose from {list(CASES_BY_ID)}")
        cases = tuple(CASES_BY_ID[cid] for cid in selected)
    else:
        cases = CASES

    summaries: list[dict[str, Any]] = []
    for spec in models:
        started = time.monotonic()
        prov = (
            provider_factory(spec)
            if provider_factory
            else resolve_provider(spec, config_path=args.config, base_url=args.base_url)
        )
        results = await evaluate_model(prov, cases, max_turns=args.max_turns, concurrency=args.concurrency)
        summaries.append(_summarize(spec, results, time.monotonic() - started))

    print(format_markdown_table(summaries))
    serialized = (
        json.dumps({"max_turns": args.max_turns, "cases": [c.case_id for c in cases], "models": summaries}, indent=2)
        + "\n"
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    return json.loads(serialized)


def main() -> None:
    """CLI entrypoint."""
    asyncio.run(run_cli(sys.argv[1:], configure_logging=True))


if __name__ == "__main__":
    main()
