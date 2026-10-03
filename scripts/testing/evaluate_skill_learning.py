"""Compare learned skills with no memory and a verbatim correction on fresh tasks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom import model_loading
from mindroom.agent_storage import create_session_storage
from mindroom.claude_prompt_cache import aclose_anthropic_async_client
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.skill_learning.reviewer import review_conversation
from mindroom.skill_learning.tools import ReviewProgress
from mindroom.tool_system.skills import build_agent_skills
from mindroom.usage_stats import collect_admin_usage

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

ARMS = ("no_memory", "raw_correction", "learned_skills")
CORRECTION = (
    "For sensor exports, return only CSV with header sensor,fahrenheit. "
    "Input temperatures are Celsius: convert with F = C * 9 / 5 + 32. "
    "Sort sensor names alphabetically and print exactly one decimal place. "
    "No Markdown fences or prose. This convention applies only to sensor exports."
)


@dataclass(frozen=True)
class Case:
    """One held-out input with an independently authored exact-output oracle."""

    name: str
    prompt: str
    expected: str

    def accepts(self, output: str) -> bool:
        """Score output while ignoring line endings and a trailing newline."""
        return output.replace("\r\n", "\n").removesuffix("\n") == self.expected


CASES = (
    Case("cold", "Prepare a sensor export: cedar: 37; birch: -40.", "sensor,fahrenheit\nbirch,-40.0\ncedar,98.6"),
    Case("warm", "Prepare a sensor export: maple: 25; ash: -10.", "sensor,fahrenheit\nash,14.0\nmaple,77.0"),
    Case("control", "What is 6 times 7? Reply with only the integer.", "42"),
)


@dataclass
class Trial:
    """One fresh model run, including its exact output and reported usage."""

    case: str
    arm: str
    repeat: int
    output: str
    passed: bool
    metrics: dict[str, Any]
    elapsed_seconds: float
    tool_calls: list[str]


@dataclass
class Report:
    """Evidence for one learned artifact, not a claim of general learning quality."""

    provider: str
    model: str
    repeats: int
    completed: bool = False
    scenario_version: int = 1
    learned_skills: list[str] = field(default_factory=list)
    review_tokens: dict[str, int] = field(default_factory=dict)
    review_seconds: float = 0
    trials: list[Trial] = field(default_factory=list)

    def write(self, output_dir: Path) -> None:
        """Checkpoint completed trials so a failed provider call cannot erase earlier results."""
        payload = asdict(self)
        payload["summary"] = {
            arm: {
                "passed": sum(trial.passed for trial in self.trials if trial.arm == arm),
                "trials": sum(trial.arm == arm for trial in self.trials),
                "transfer_passed": sum(
                    trial.passed for trial in self.trials if trial.arm == arm and trial.case != "control"
                ),
                "control_passed": sum(
                    trial.passed for trial in self.trials if trial.arm == arm and trial.case == "control"
                ),
            }
            for arm in ARMS
        }
        (output_dir / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _seed_conversation(config: Config, paths: RuntimePaths) -> None:
    """Store the fixed training example; held-out inputs and answers never enter review."""
    messages = [
        Message(role="user", content="Prepare a sensor export: pine: 100; oak: 0."),
        Message(role="assistant", content="Pine is 100 degrees Celsius and oak is 0 degrees Celsius."),
        Message(role="user", content=CORRECTION),
        Message(role="assistant", content="sensor,fahrenheit\noak,32.0\npine,212.0"),
        Message(role="user", content="Correct. Use that procedure for future sensor exports."),
    ]
    storage = create_session_storage("learner", config, paths, execution_identity=None)
    try:
        storage.upsert_session(AgentSession(session_id="training", agent_id="learner"))
        storage.upsert_run(
            run=RunOutput(run_id="example", agent_id="learner", session_id="training", messages=messages),
            session_id="training",
        )
    finally:
        storage.close()


async def _trial(
    *,
    case: Case,
    arm: str,
    repeat: int,
    directory: Path,
    skills_root: Path,
    config: Config,
    paths: RuntimePaths,
) -> Trial:
    """Transfer only skill files; never reuse an Agent, model, session, or writable workspace."""
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    if arm == "learned_skills" and skills_root.exists():
        shutil.copytree(skills_root, workspace / "skills")
    trial_paths = replace(paths, storage_root=directory / "storage", control_state_root=directory / "control")
    skills = build_agent_skills("learner", config, trial_paths, workspace_root=workspace)
    model = model_loading.get_model_instance(config, trial_paths)
    agent = Agent(
        model=model,
        skills=skills,
        instructions=[CORRECTION] if arm == "raw_correction" else [],
        markdown=False,
        tool_call_limit=8,
        telemetry=False,
    )
    started = time.perf_counter()
    try:
        response = await agent.arun(case.prompt)
    finally:
        await aclose_anthropic_async_client(model)
    output = response.content if isinstance(response.content, str) else str(response.content)
    return Trial(
        case=case.name,
        arm=arm,
        repeat=repeat,
        output=output,
        passed=case.accepts(output),
        metrics=response.metrics.to_dict() if response.metrics is not None else {},
        elapsed_seconds=time.perf_counter() - started,
        tool_calls=[tool.tool_name for tool in response.tools or [] if tool.tool_name is not None],
    )


async def evaluate(
    *,
    output_dir: Path,
    model_config: ModelConfig,
    repeats: int = 3,
    timeout_seconds: int = 120,
) -> Report:
    """Run one digest review, then paired held-out trials; fail fast on provider errors."""
    if repeats < 1 or timeout_seconds < 1:
        msg = "repeats and timeout_seconds must be positive"
        raise ValueError(msg)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    learning_dir = output_dir / "learning"
    paths = replace(
        resolve_runtime_paths(
            config_path=output_dir / "config.yaml",
            storage_path=learning_dir / "storage",
            # Keep provider authentication, but never inherit a live runtime's state redirects.
            process_env={name: value for name, value in os.environ.items() if not name.startswith("MINDROOM_")},
        ),
        control_state_root=learning_dir / "control",
    )
    config = Config(
        agents={"learner": AgentConfig(display_name="Learner", include_default_tools=False)},
        models={"default": model_config},
    )
    config.agents["learner"].skill_learning.enabled = True
    skills_root = learning_dir / "workspace/skills"
    skills_root.mkdir(parents=True)
    _seed_conversation(config, paths)
    report = Report(provider=model_config.provider, model=model_config.id, repeats=repeats)
    progress = ReviewProgress()
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            await review_conversation(
                config=config,
                runtime_paths=paths,
                agent_name="learner",
                session_id="training",
                identity=None,
                skills_root=skills_root,
                captured=None,
                progress=progress,
                skill_roots=[output_dir / "catalog"],
            )
    finally:
        report.review_seconds = time.perf_counter() - started
        report.learned_skills = sorted(progress.changes)
        report.review_tokens = collect_admin_usage(config=config, runtime_paths=paths).totals.to_dict()
        report.write(output_dir)
    for repeat in range(repeats):
        for index, case in enumerate(CASES):
            offset = (repeat + index) % len(ARMS)
            for arm in ARMS[offset:] + ARMS[:offset]:
                async with asyncio.timeout(timeout_seconds):
                    result = await _trial(
                        case=case,
                        arm=arm,
                        repeat=repeat,
                        directory=output_dir / "trials" / f"{repeat}-{case.name}-{arm}",
                        skills_root=skills_root,
                        config=config,
                        paths=paths,
                    )
                report.trials.append(result)
                report.write(output_dir)
    report.completed = True
    report.write(output_dir)
    return report


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def main() -> None:
    """Run explicitly against an environment-authenticated model, without loading a live config."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", required=True, help="Provider model ID (same model for review and all arms)")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory for retained evidence")
    parser.add_argument("--repeats", type=_positive_int, default=3)
    parser.add_argument("--timeout-seconds", type=_positive_int, default=120, help="Per review or trial")
    args = parser.parse_args()
    asyncio.run(
        evaluate(
            output_dir=args.output_dir,
            model_config=ModelConfig(provider=args.provider, id=args.model),
            repeats=args.repeats,
            timeout_seconds=args.timeout_seconds,
        ),
    )
    print(f"Report: {args.output_dir / 'report.json'}")


if __name__ == "__main__":
    main()
