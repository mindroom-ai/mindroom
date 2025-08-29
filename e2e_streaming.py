"""E2E streaming script for agents and teams (uses .env credentials).

Examples:
  - Single agent streaming:
      python e2e_streaming.py agent-stream --agent general --message "Hi there"

  - Team streaming (uses Agno Team streaming):
      python e2e_streaming.py team-stream --agents calculator,general --message "Summarize the repo in 5 bullets"

Notes:
  - Credentials are read from your environment. If you have a .env file in the repo
    root, it will be loaded automatically (e.g., OPENROUTER_API_KEY, OPENAI_API_KEY, etc.).
  - Agents and models come from your Mindroom config.yaml.

"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mindroom.agents import create_agent
from mindroom.ai import ai_response_streaming
from mindroom.config import Config
from mindroom.teams import TeamMode, create_team_response_streaming


def _load_dotenv_if_present() -> None:
    """Load environment variables from .env if present.

    Uses python-dotenv if available; otherwise a minimal parser for KEY=VALUE lines.
    """
    env_path = Path(".env")
    if not env_path.exists():
        return
    # Try python-dotenv first (optional dependency)
    # Defer module resolution within function to keep dependency optional
    try:
        module = importlib.import_module("dotenv")
        if hasattr(module, "load_dotenv"):
            module.load_dotenv(dotenv_path=env_path)  # type: ignore[attr-defined]
            return
    except Exception as e:  # pragma: no cover
        print(f"[e2e] Warning: could not load .env via python-dotenv: {e}")

    # Minimal fallback: parse KEY=VALUE lines (ignores exports/comments)
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = line.removeprefix("export ")
        if "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


@dataclass
class _DummyAgentBot:
    agent: Any


@dataclass
class _DummyOrchestrator:
    config: Config
    agent_bots: dict[str, _DummyAgentBot]


async def run_agent_stream(agent_name: str, message: str) -> None:
    """Stream a single agent's response to stdout."""
    config = Config.from_yaml()
    # Use a local folder for e2e storage
    storage_path = Path(".e2e_sessions")
    storage_path.mkdir(parents=True, exist_ok=True)

    print(f"\n🧠 Agent: {agent_name}\n---\n", flush=True)
    full = ""
    async for chunk in ai_response_streaming(
        agent_name=agent_name,
        prompt=message,
        session_id="e2e",
        storage_path=storage_path,
        config=config,
        thread_history=None,
        room_id=None,
    ):
        full += chunk
        print(chunk, end="", flush=True)
    print("\n\n[done]", flush=True)


async def run_team_stream(agents: list[str], message: str, model_name: str | None) -> None:
    """Stream a team response to stdout using Agno Team streaming."""
    config = Config.from_yaml()

    # Build orchestrator-like wrapper with real Agent objects
    agent_bots: dict[str, _DummyAgentBot] = {}
    for name in agents:
        agent_bots[name] = _DummyAgentBot(agent=create_agent(name, config))

    orchestrator = _DummyOrchestrator(config=config, agent_bots=agent_bots)

    print(f"\n🤝 Team: {', '.join(agents)}\n---\n", flush=True)
    # Stream content; the function includes a graceful fallback
    async for chunk in create_team_response_streaming(
        agent_names=agents,
        mode=TeamMode.COORDINATE,
        message=message,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        thread_history=None,
        model_name=model_name,
    ):
        print(chunk, end="", flush=True)
    print("\n\n[done]", flush=True)


def main() -> None:
    """CLI entry point for streaming e2e checks."""
    _load_dotenv_if_present()

    parser = argparse.ArgumentParser(description="Mindroom streaming E2E utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_agent = sub.add_parser("agent-stream", help="Stream a single agent response")
    p_agent.add_argument("--agent", required=True, help="Agent name (e.g. general)")
    p_agent.add_argument("--message", required=True, help="Prompt to send")

    p_team = sub.add_parser("team-stream", help="Stream a team response using Agno Team")
    p_team.add_argument("--agents", required=True, help="Comma-separated agent names, e.g. general,calculator")
    p_team.add_argument("--message", required=True, help="User message to send to the team")
    p_team.add_argument("--model", default=None, help="Optional model override name (must exist in config.models)")

    args = parser.parse_args()

    if args.cmd == "agent-stream":
        asyncio.run(run_agent_stream(args.agent, args.message))
    elif args.cmd == "team-stream":
        agents = [x.strip() for x in args.agents.split(",") if x.strip()]
        asyncio.run(run_team_stream(agents, args.message, args.model))


if __name__ == "__main__":
    main()
