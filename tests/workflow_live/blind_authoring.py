"""Blind-authoring check: ask fresh models to write workflows from `start_workflow`'s description alone and run them.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    uv run python -m tests.workflow_live.blind_authoring --model haiku --model opus --model sol [--slot N]

For each configured model name and each of three tasks (scatter-and-join, bounded check loop, two named agents
that negotiate directly), this script:
1. Builds the live `start_workflow` tool description from the slot's config (and passes ONLY that description plus
   the task sentence to the fresh model: no spec, no docs, no examples beyond the description itself).
2. Saves the model's script into the Helper's workspace and runs it on the live backend with `start_workflow`.
3. Prints PASS or FAIL per `(model, task)` together with the authored script and run outcome, exiting 0 only when
   every pair completes successfully.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, TurnCompleted
from mindroom.providers.registry import create_model_provider
from mindroom.turn_model import ModelScope, resolve_turn_model
from tests.workflow_live.kill_after_write import Deployment, LiveError, add_send_options, say, until

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuthoringTask:
    """One blind-authoring prompt and the JSON args used to execute the script the model writes."""

    name: str
    prompt: str
    args: dict[str, object]


TASKS: tuple[AuthoringTask, ...] = (
    AuthoringTask(
        name="scatter_join",
        prompt=(
            "Write a workflow script for start_workflow that takes args['topics'] (a list of short topics), "
            "researches every topic in parallel with an agent, keeps the surviving notes if any topic fails, "
            "and has a final agent synthesize those notes into a concise report string."
        ),
        args={"topics": ["SQLite WAL", "Unix domain sockets", "Python dataclasses"]},
    ),
    AuthoringTask(
        name="bounded_check",
        prompt=(
            "Write a workflow script for start_workflow that takes args['prompt'], drafts an answer with one agent, "
            "then runs at most 3 rounds of an independent checker agent with a JSON schema "
            "{ok: boolean, issues: array of strings} and a fixer agent when ok is false, returning the final draft."
        ),
        args={"prompt": "Summarize three key guarantees of TCP in three short bullet points."},
    ),
    AuthoringTask(
        name="negotiate",
        prompt=(
            "Write a workflow script for start_workflow that takes args['feature'], runs two named agents in parallel "
            "('backend' and 'frontend') that negotiate a JSON API contract directly using peer messages and save the "
            "agreed contract in shared state under 'contract', then has an integrator agent read the contract from "
            "shared state and summarize how backend and frontend fit together."
        ),
        args={"feature": "User avatar upload endpoint"},
    ),
)

_SYSTEM_PROMPT = (
    "You are authoring a Python workflow script for MindRoom's `start_workflow` tool. "
    "You have ONLY the tool description below. Return ONLY valid Python code for the workflow file "
    "(no prose outside a single ```python ... ``` block)."
)


def _extract_python(text: str) -> str:
    match = re.search(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip() + "\n"


def _build_start_workflow_description(deployment: Deployment) -> tuple[Config, RuntimePaths, str]:
    config_path = deployment.config_path
    runtime_paths = RuntimePaths(
        config_path=config_path,
        config_dir=config_path.parent,
        env_path=deployment.directory / ".env",
        storage_root=deployment.directory / "storage",
        control_state_root=deployment.directory / "control",
    )
    config = Config.from_yaml(config_path)
    tools = WorkflowTools(runtime_config=config, agent_name=deployment.helper, runtime_paths=runtime_paths)
    description = tools.start_workflow.__doc__ or ""
    return config, runtime_paths, description


async def _author_script(
    config: Config,
    runtime_paths: RuntimePaths,
    description: str,
    model_name: str,
    task: AuthoringTask,
) -> str:
    turn_model = resolve_turn_model(config, runtime_paths, ModelScope(entity=None, explicit=model_name), None)
    provider = create_model_provider(config, runtime_paths, turn_model.model_name)
    user_prompt = (
        f"Tool description for `start_workflow`:\n\n{description}\n\n"
        f"Task:\n{task.prompt}\n\n"
        f"Your script will be called with args = {json.dumps(task.args)}."
    )
    request = ModelRequest(
        messages=(
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ),
    )
    reply_text = ""
    async for event in provider.stream(request):
        if isinstance(event, TurnCompleted) and event.message.content:
            reply_text = str(event.message.content)
    return _extract_python(reply_text)


def _start_with_args(
    deployment: Deployment,
    source: str,
    task_args: dict[str, object],
    token: str,
    dev: Path | None,
) -> dict[str, Any]:
    name = f"blind_{token}.py"
    deployment.workspace.mkdir(parents=True, exist_ok=True)
    (deployment.workspace / name).write_text(source, encoding="utf-8")
    deployment.ask(
        f'Call the tool start_workflow with path="{name}" and args={json.dumps(task_args)}. '
        "Call it once and only once. Reply with the run ID it returns.",
        dev,
    )
    run = until(f"workflow run for {name}", lambda: deployment.run(token), 120, 1)
    return until(
        f"completion of {run['run_id']}",
        lambda: (
            cur
            if (cur := deployment.run(token)) is not None
            and cur["state"] in {"completed", "failed", "cancelled", "interrupted"}
            else None
        ),
        300,
        2,
    )


def main() -> int:
    """Author and run the three blind-authoring tasks across the requested models."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Configured model name to test (repeatable, e.g. --model haiku --model opus --model sol)",
    )
    parser.add_argument("--slot", type=int, help="The live deployment slot (default: the only ready one)")
    add_send_options(parser)
    args = parser.parse_args()
    dev = args.dev if args.send == "ask" else None

    deployment = Deployment.find(args.slot)
    backend = deployment.backend()
    if deployment.give_workflow_toolkit(room_agent=True):
        backend.stop()
        backend.start()

    config, runtime_paths, description = _build_start_workflow_description(deployment)
    models = args.models or [next(iter(config.models))]

    all_passed = True
    for model_name in models:
        for task in TASKS:
            token = f"blind_{task.name}_{secrets.token_hex(4)}"
            say(f"authoring model={model_name} task={task.name}")
            try:
                script = asyncio.run(_author_script(config, runtime_paths, description, model_name, task))
                ended = _start_with_args(deployment, script, task.args, token, dev)
                passed = ended["state"] == "completed"
                status = "PASS" if passed else "FAIL"
                if not passed:
                    all_passed = False
                print(f"=== {status}: model={model_name} task={task.name} state={ended['state']} ===")
                print(script.rstrip())
                if ended.get("error"):
                    print(f"--- error ---\n{ended['error']}")
                else:
                    preview = json.dumps(ended.get("result"))[:300]
                    print(f"--- result ---\n{preview}")
            except (LiveError, Exception) as exc:
                all_passed = False
                print(f"=== FAIL: model={model_name} task={task.name} error={exc} ===")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
