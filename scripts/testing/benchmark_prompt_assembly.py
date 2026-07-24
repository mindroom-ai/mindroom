"""Benchmark repeated warm agent preparation and per-turn tool-surface work.

Measures wall time plus call counts for plugin loads, skill scans, skill-cache
clears, tool-schema preparations, Function deep copies, and payload
serializations across three phases:

- ``agent_build``: repeated warm ``create_agent`` calls for one config.
- ``tool_surface``: one turn's static token budgeting (execution preparation
  plus the history-runtime re-estimate) and run-metadata payload assembly on
  one fresh agent instance.
- ``prompt_branches``: controlled file-memory and agent-build delays executed
  sequentially (before) and concurrently (after).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog
from agno.tools.function import Function

from mindroom import agents as agents_module
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.agents import create_agent
from mindroom.config.main import ResolvedRuntimeModel, load_config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.history import prompt_tokens as prompt_tokens_module
from mindroom.history.prompt_tokens import (
    agent_static_token_estimator,
    agent_tool_definition_payloads_for_logging,
    estimate_agent_static_tokens,
)
from mindroom.memory import MemoryPromptParts
from mindroom.model_defaults import CONFIG_INIT_MODEL_PRESETS
from mindroom.pre_model_preparation import prepare_prompt_branches
from mindroom.tool_system import skills as skills_module
from mindroom.tool_system.plugins import load_plugins

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.agent import Agent
    from agno.skills.skill import Skill

_COUNTERS: dict[str, int] = {}


def _count(name: str) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + 1


def _counting[**P, T](name: str, func: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        _count(name)
        return func(*args, **kwargs)

    return wrapper


def _install_counters() -> None:
    agents_module.load_plugins = _counting("plugin_loads", load_plugins)
    skills_module.clear_skill_cache = _counting("skill_cache_clears", skills_module.clear_skill_cache)

    original_local_skills = skills_module.LocalSkills

    class _CountingLocalSkills(original_local_skills):  # type: ignore[misc, valid-type]
        def load(self) -> list[Skill]:
            _count("skill_scans")
            return super().load()

    skills_module.LocalSkills = _CountingLocalSkills

    KnowledgeToolDescribingAgent.get_tools = _counting(
        "agent_get_tools_calls",
        KnowledgeToolDescribingAgent.get_tools,
    )
    prompt_tokens_module.stable_serialize = _counting(
        "payload_serializations",
        prompt_tokens_module.stable_serialize,
    )

    original_model_copy = Function.model_copy

    def _counting_model_copy(self: Function, *, deep: bool = False) -> Function:
        if deep:
            _count("function_deep_copies")
        return original_model_copy(self, deep=deep)

    Function.model_copy = _counting_model_copy  # type: ignore[method-assign]


def _write_fixture(root: Path) -> Path:
    plugin_root = root / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "tools_module": "tools.py", "skills": ["skills"]}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[self.echo])\n"
        "\n"
        "    def echo(self, text: str) -> str:\n"
        '        """Echo the provided text back."""\n'
        "        return text\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='demo_plugin',\n"
        "    display_name='Demo Plugin',\n"
        "    description='Demo plugin tool',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )
    for index in (1, 2):
        skill_dir = plugin_root / "skills" / f"demo-skill-{index}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: demo-skill-{index}\ndescription: Demo skill {index}\n---\n\n# Demo {index}\n",
            encoding="utf-8",
        )

    default_model = CONFIG_INIT_MODEL_PRESETS["openai"]
    config_path = root / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        f"    provider: {default_model.provider}\n"
        f"    id: {default_model.id}\n"
        "router:\n"
        "  model: default\n"
        "plugins:\n"
        "  - ./plugins/demo\n"
        "agents:\n"
        "  helper:\n"
        "    display_name: Helper\n"
        "    role: Benchmark helper agent\n"
        "    tools: [calculator, file, demo_plugin]\n"
        "    skills: [demo-skill-1, demo-skill-2]\n",
        encoding="utf-8",
    )
    return config_path


def _phase[T](
    label: str,
    iterations: int,
    run_iteration: Callable[[T], None],
    prepare_iteration: Callable[[], T],
) -> dict[str, object]:
    counters_before = dict(_COUNTERS)
    samples: list[float] = []
    for _ in range(iterations):
        prepared = prepare_iteration()
        started_at = time.perf_counter()
        run_iteration(prepared)
        samples.append((time.perf_counter() - started_at) * 1000)
    counters = {
        name: count - counters_before.get(name, 0)
        for name, count in _COUNTERS.items()
        if name not in counters_before or count != counters_before[name]
    }
    return {
        "phase": label,
        "iterations": iterations,
        "total_ms": round(sum(samples), 3),
        "mean_ms": round(sum(samples) / len(samples), 3),
        "counters": counters,
    }


def _sample_summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    midpoint = len(ordered) // 2
    median = ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    return {
        "total_ms": round(sum(samples), 3),
        "mean_ms": round(sum(samples) / len(samples), 3),
        "p50_ms": round(median, 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


async def _benchmark_prompt_branches(
    *,
    runs: int,
    memory_delay_ms: float,
    agent_delay_ms: float,
) -> dict[str, object]:
    """Compare old sequential branch timing with concurrent preparation."""
    runtime_model = ResolvedRuntimeModel(model_name="benchmark", context_window=None)
    memory_delay = memory_delay_ms / 1000
    agent_delay = agent_delay_ms / 1000

    async def prepare_memory() -> MemoryPromptParts:
        await asyncio.to_thread(time.sleep, memory_delay)
        return MemoryPromptParts()

    def build_agent() -> tuple[ResolvedRuntimeModel, Agent]:
        time.sleep(agent_delay)
        return runtime_model, cast("Agent", object())

    async def run_sequential() -> None:
        await prepare_memory()
        await asyncio.to_thread(build_agent)

    async def run_parallel() -> None:
        await prepare_prompt_branches(
            prepare_memory=prepare_memory,
            build_agent=build_agent,
            agent_name="benchmark",
            shared_scope_storage=None,
            pipeline_timing=None,
            cancel_memory_on_agent_failure=True,
        )

    before_samples: list[float] = []
    after_samples: list[float] = []
    for _ in range(runs):
        started_at = time.perf_counter()
        await run_sequential()
        before_samples.append((time.perf_counter() - started_at) * 1000)

        started_at = time.perf_counter()
        await run_parallel()
        after_samples.append((time.perf_counter() - started_at) * 1000)

    before = _sample_summary(before_samples)
    after = _sample_summary(after_samples)
    return {
        "phase": "prompt_branches",
        "iterations": runs,
        "configured_branch_ms": {
            "file_memory": memory_delay_ms,
            "agent_build": agent_delay_ms,
        },
        "before_sequential": before,
        "after_parallel": after,
        "mean_saved_ms": round(before["mean_ms"] - after["mean_ms"], 3),
    }


def main() -> None:
    """Run the prompt-assembly benchmark and print JSON results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builds", type=int, default=25)
    parser.add_argument("--turns", type=int, default=25)
    parser.add_argument("--branch-runs", type=int, default=10)
    parser.add_argument("--branch-memory-ms", type=float, default=20.0)
    parser.add_argument("--branch-agent-ms", type=float, default=40.0)
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.ERROR)
    logging.getLogger("mindroom").setLevel(logging.ERROR)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR),
        cache_logger_on_first_use=False,
    )

    with tempfile.TemporaryDirectory(prefix="mindroom-prompt-assembly-benchmark-") as tmp:
        root = Path(tmp)
        config_path = _write_fixture(root)
        runtime_paths = resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=root / "storage",
            process_env={"OPENAI_API_KEY": "sk-benchmark"},
        )
        config = load_config(runtime_paths)

        def _build_agent() -> KnowledgeToolDescribingAgent:
            return create_agent(
                "helper",
                config,
                runtime_paths,
                execution_identity=None,
                session_id="benchmark-session",
                include_openai_compat_guidance=True,
            )

        _build_agent()  # Warm plugin/module/tool-registry caches once.
        _install_counters()

        results = [
            _phase(
                "agent_build",
                args.builds,
                lambda _prepared: _build_agent(),
                prepare_iteration=lambda: None,
            ),
        ]

        prompt = "Please summarize the latest benchmark results."

        def _run_turn_surface(agent: KnowledgeToolDescribingAgent) -> None:
            # Execution preparation: static budgeting with a request-local estimator.
            agent_static_token_estimator(agent).estimate(prompt)
            # History runtime: an independent re-estimate for replay planning.
            estimate_agent_static_tokens(agent, prompt)
            # Run metadata: model-visible tool schema payloads.
            agent_tool_definition_payloads_for_logging(agent)

        # A fresh agent per iteration mirrors production, where every turn
        # rebuilds the agent instance before budgeting and metadata assembly.
        results.append(
            _phase(
                "tool_surface_per_turn",
                args.turns,
                _run_turn_surface,
                prepare_iteration=_build_agent,
            ),
        )

        from mindroom.tool_schema_cache import _cached_processed_function_schema  # noqa: PLC0415

        cache_info = _cached_processed_function_schema.cache_info()
        results.append(
            {
                "phase": "tool_schema_lru",
                "hits": cache_info.hits,
                "misses": cache_info.misses,
                "size": cache_info.currsize,
            },
        )
        results.append(
            asyncio.run(
                _benchmark_prompt_branches(
                    runs=args.branch_runs,
                    memory_delay_ms=args.branch_memory_ms,
                    agent_delay_ms=args.branch_agent_ms,
                ),
            ),
        )

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
