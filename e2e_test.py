#!/usr/bin/env python3
"""Simple E2E script to exercise team streaming without Matrix.

Usage examples:
  - Stream a team response using agents from your config.yaml:
      python e2e_test.py team-stream --agents calculator,general --message "Summarize the repo in 5 bullets"

  - Override model (must be configured in config models):
      python e2e_test.py team-stream --agents general,calculator --message "hello" --model default

Requirements:
  - Valid model credentials in your environment (e.g., OPENAI_API_KEY, OPENROUTER_API_KEY, etc.)
  - Agents defined in your Mindroom config (config.yaml)

This does not require a Matrix server; it directly exercises Agno Team streaming.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

from mindroom.agents import create_agent
from mindroom.config import Config
from mindroom.teams import TeamMode, team_response_stream_or_text


@dataclass
class _DummyAgentBot:
    agent: Any


@dataclass
class _DummyOrchestrator:
    config: Config
    agent_bots: dict[str, _DummyAgentBot]


async def run_team_stream(agents: list[str], message: str, model_name: str | None) -> None:
    """Stream a team response to stdout.

    Creates Agents from config.yaml, builds a temporary orchestrator-like
    object, and streams the team response using Agno's streaming API.

    Args:
        agents: List of agent names to include in the team
        message: User message to send
        model_name: Optional model override name

    """
    config = Config.from_yaml()

    # Build orchestrator-like wrapper with real Agent objects
    agent_bots: dict[str, _DummyAgentBot] = {}
    for name in agents:
        agent_bots[name] = _DummyAgentBot(agent=create_agent(name, config))

    orchestrator = _DummyOrchestrator(config=config, agent_bots=agent_bots)

    stream, text = await team_response_stream_or_text(
        agent_names=agents,
        mode=TeamMode.COORDINATE,
        message=message,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        thread_history=None,
        model_name=model_name,
    )

    header = f"\n🤝 Team: {', '.join(agents)}\n---\n"
    print(header, end="", flush=True)

    if stream is not None:
        async for chunk in stream:
            print(chunk, end="", flush=True)
        print("\n\n[done]")
    else:
        print(text or "(no content)")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Mindroom E2E utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_team = sub.add_parser("team-stream", help="Stream a team response without Matrix")
    p_team.add_argument("--agents", required=True, help="Comma-separated agent names, e.g. general,calculator")
    p_team.add_argument("--message", required=True, help="User message to send to the team")
    p_team.add_argument("--model", default=None, help="Optional model override (must exist in config.models)")

    args = parser.parse_args()

    if args.cmd == "team-stream":
        agents = [x.strip() for x in args.agents.split(",") if x.strip()]
        asyncio.run(run_team_stream(agents, args.message, args.model))


if __name__ == "__main__":
    main()
