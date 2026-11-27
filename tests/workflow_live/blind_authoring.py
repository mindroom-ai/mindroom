"""Blind-authoring check for workflow scripts across configured models (Plan Task 24).

Usage:
  uv run python -m tests.workflow_live.blind_authoring --model haiku --model opus --model sol
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, TextDelta, TurnCompleted
from mindroom.providers.registry import build_model_provider
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.turn_model import ModelScope, resolve_turn_model
from tests.workflow_live.kill_after_write import Deployment, LiveError, add_send_options, say, until

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


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
            "Write a workflow script for workflow(action='start') that takes args['topics'] (a list of short topics), "
            "researches every topic in parallel with an agent, keeps the surviving notes if any topic fails, "
            "and has a final agent synthesize those notes into a concise report string."
        ),
        args={"topics": ["SQLite WAL", "Unix domain sockets", "Python dataclasses"]},
    ),
    AuthoringTask(
        name="bounded_check",
        prompt=(
            "Write a workflow script for workflow(action='start') that takes args['prompt'], drafts an answer with one agent, "
            "then runs at most 3 rounds of an independent checker agent with a JSON schema "
            "{ok: boolean, issues: array of strings} and a fixer agent when ok is false, returning the final draft."
        ),
        args={"prompt": "Summarize three key guarantees of TCP in three short bullet points."},
    ),
    AuthoringTask(
        name="negotiate",
        prompt=(
            "Write a workflow script for workflow(action='start') that takes args['feature'], runs two named agents in parallel "
            "('backend' and 'frontend') that negotiate a JSON API contract directly using peer messages and save the "
            "agreed contract in shared state under 'contract', then has an integrator agent read the contract from "
            "shared state and summarize how backend and frontend fit together."
        ),
        args={"feature": "User avatar upload endpoint"},
    ),
)

_SYSTEM_PROMPT = (
    "You are authoring a Python workflow script for MindRoom's `workflow` tool. "
    "You have ONLY the tool description below. Return ONLY valid Python code for the workflow file "
    "(no prose outside a single ```python ... ``` block)."
)


def _extract_python(text: str) -> str:
    match = re.search(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip() + "\n"


def _build_start_workflow_description(deployment: Deployment) -> tuple[Config, RuntimePaths, str]:
    runtime_paths = resolve_runtime_paths(
        config_path=deployment.directory / "config.yaml",
        storage_path=deployment.directory / "storage",
    )
    config = load_config(runtime_paths)
    agent_name = "helper" if "helper" in config.agents else next(iter(config.agents))
    room_id = str(deployment.live["lobby"]["roomId"])
    toolkit = WorkflowTools(config, agent_name, room_id, runtime_paths)
    functions = {function.name: function for function in adapt_toolkit(toolkit, key="workflow").functions}
    description = functions["workflow"].description or ""
    return config, runtime_paths, description


async def _author_script(
    config: Config,
    runtime_paths: RuntimePaths,
    description: str,
    model_name: str,
    task: AuthoringTask,
) -> str:
    turn_model = resolve_turn_model(config, runtime_paths, ModelScope(entity=None, explicit=model_name), None)
    provider = build_model_provider(config, runtime_paths, turn_model.model_name)
    user_prompt = (
        f"Tool description for `workflow`:\n\n{description}\n\n"
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
    deltas: list[str] = []
    async for event in provider.stream(request):
        if isinstance(event, TextDelta):
            deltas.append(event.text)
        elif isinstance(event, TurnCompleted) and event.message.content:
            reply_text = str(event.message.content)
    return _extract_python(reply_text or "".join(deltas))


def _peer_activity(deployment: Deployment, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    found = sorted((deployment.directory / "storage").glob("**/workflow_runs/workflow_runs.sqlite3"))
    if not found:
        return [], {}
    with contextlib.closing(sqlite3.connect(f"file:{found[0]}?mode=ro", uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        msg_rows = connection.execute(
            "SELECT seq, sender_name, recipient_name, text FROM workflow_messages "
            "WHERE run_id = ? AND withdrawn = 0 ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
        state_rows = connection.execute(
            "SELECT key, value_json FROM workflow_state WHERE run_id = ? ORDER BY key ASC",
            (run_id,),
        ).fetchall()
    messages = [dict(row) for row in msg_rows]
    state = {str(row["key"]): json.loads(row["value_json"]) for row in state_rows}
    return messages, state


def _has_opposite_direction_exchange(messages: list[dict[str, Any]]) -> bool:
    peer_msgs = [
        (str(m.get("sender_name") or ""), str(m.get("recipient_name") or ""))
        for m in messages
        if m.get("sender_name") and m.get("sender_name") != "script"
    ]
    senders = {sender for sender, _ in peer_msgs}
    for sender_a in senders:
        for sender_b in senders:
            if sender_a >= sender_b:
                continue
            a_to_b = any(s == sender_a and r in (sender_b, "*") for s, r in peer_msgs)
            b_to_a = any(s == sender_b and r in (sender_a, "*") for s, r in peer_msgs)
            if a_to_b and b_to_a:
                return True
    return False


_MIN_AGENTS = {"scatter_join": 3, "bounded_check": 2, "negotiate": 3}


def _verify_task(
    task: AuthoringTask,
    run: dict[str, Any],
    nodes: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[bool, str]:
    completed_agents = [
        n for n in nodes if not n.get("stale") and n.get("kind") in ("agent", "turn") and n.get("state") == "completed"
    ]
    min_agents = _MIN_AGENTS.get(task.name, 1)
    if run.get("state") != "completed":
        return False, f"run ended in state={run.get('state')}"
    if len(completed_agents) < min_agents:
        return False, f"expected at least {min_agents} completed agent nodes, got {len(completed_agents)}"
    if task.name == "negotiate" and not _has_opposite_direction_exchange(messages):
        return False, f"expected opposite-direction peer messages, got {len(messages)} message(s)"
    if task.name == "negotiate" and state.get("contract") is None:
        return False, f"expected shared state key 'contract', got keys {sorted(state)}"
    return True, "ok"


def _start_with_args(
    deployment: Deployment,
    source: str,
    task_args: dict[str, object],
    token: str,
    dev: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = f"blind_{token}.py"
    deployment.workspace.mkdir(parents=True, exist_ok=True)
    marked_source = f"# blind_token={token}\n{source}"
    (deployment.workspace / name).write_text(marked_source, encoding="utf-8")
    deployment.ask(
        f'Call the tool workflow with action="start", path="{name}", and args={json.dumps(task_args)}. '
        "Call it once and only once. Reply with the run ID it returns.",
        dev,
    )
    until(f"workflow run for {name}", lambda: deployment.run(token), 120, 1)
    return deployment.ended_run(token, 300)


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
                run, nodes = _start_with_args(deployment, script, task.args, token, dev)
                messages, state = _peer_activity(deployment, str(run["run_id"]))
                passed, reason = _verify_task(task, run, nodes, messages, state)
                status = "PASS" if passed else "FAIL"
                if not passed:
                    all_passed = False
                print(
                    f"=== {status}: model={model_name} task={task.name} state={run['state']} "
                    f"agents={len(nodes)} messages={len(messages)} reason={reason} ===",
                )
                print(script.rstrip())
                if run.get("error"):
                    print(f"--- error ---\n{run['error']}")
                else:
                    preview = str(run.get("result_json") or "")[:300]
                    print(f"--- result ---\n{preview}")
            except (LiveError, Exception) as exc:
                all_passed = False
                print(f"=== FAIL: model={model_name} task={task.name} error={exc} ===")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
