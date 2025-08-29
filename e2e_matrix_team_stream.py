"""Send a streaming Team response into a Matrix room using your config and .env.

Example:
  python e2e_matrix_team_stream.py \
    --room-id '!yourroom:server' \
    --agents calculator,general \
    --message "Quick team greeting"

Notes:
  - Uses MATRIX_HOMESERVER from your config/constants.
  - Ensures the sender agent account exists and logs in.
  - Streams the team response as the sender agent into the room.
  - Loads .env automatically (python-dotenv if present, else minimal parser).

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
from mindroom.bot import StreamingResponse
from mindroom.config import Config
from mindroom.constants import MATRIX_HOMESERVER
from mindroom.matrix.client import join_room, send_message
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser, create_agent_user, login_agent_user
from mindroom.streaming import stream_chunks_to_room
from mindroom.teams import TeamMode, create_team_response_streaming


def _load_dotenv_if_present() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        module = importlib.import_module("dotenv")
        if hasattr(module, "load_dotenv"):
            module.load_dotenv(dotenv_path=env_path)  # type: ignore[attr-defined]
            return
    except Exception as e:  # pragma: no cover
        print(f"[matrix-e2e] Warning: could not load .env via python-dotenv: {e}")

    # Minimal fallback
    text = env_path.read_text()
    for raw in text.splitlines():
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


async def run(room_id: str, agents: list[str], message: str, sender: str | None) -> None:
    """Stream a team response into a Matrix room.

    Logs in the sender agent, joins the room, and streams the team content
    using the shared streaming transport helper.
    """
    # Load config and prepare orchestrator-like mapping
    config = Config.from_yaml()
    agent_bots: dict[str, _DummyAgentBot] = {}
    for name in agents:
        agent_bots[name] = _DummyAgentBot(agent=create_agent(name, config))
    orchestrator = _DummyOrchestrator(config=config, agent_bots=agent_bots)

    # Sender: default to first agent
    sender_name = sender or agents[0]
    sender_display = config.agents[sender_name].display_name if sender_name in config.agents else sender_name

    # Ensure and login sender account
    agent_user: AgentMatrixUser = await create_agent_user(MATRIX_HOMESERVER, sender_name, sender_display)
    client = await login_agent_user(MATRIX_HOMESERVER, agent_user)

    # Join room if needed
    await join_room(client, room_id)

    # Compose stream
    stream = create_team_response_streaming(
        agent_names=agents,
        mode=TeamMode.COORDINATE,
        message=message,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        thread_history=None,
        model_name=None,
    )

    # Send header + stream chunks as the sender agent into the room
    header = f"🤝 **Team Response** ({', '.join(agents)}):\n\n"
    event_id, _ = await stream_chunks_to_room(
        client,
        room_id,
        reply_to_event_id=None,
        thread_id=None,
        sender_domain=MatrixID.parse(agent_user.user_id).domain,
        config=config,
        chunk_iter=stream,
        header=header,
        existing_event_id=None,
        streaming_cls=StreamingResponse,
    )
    print(f"[matrix-e2e] Sent streaming team response event: {event_id}")

    # Optional courtesy message
    await send_message(
        client,
        room_id,
        {"msgtype": "m.notice", "body": "[matrix-e2e] Team streaming completed."},
    )

    await client.close()


def main() -> None:
    """CLI entry point."""
    _load_dotenv_if_present()
    parser = argparse.ArgumentParser(description="Stream a team response into a Matrix room")
    parser.add_argument("--room-id", required=True, help="Matrix room ID (e.g. !abc:server)")
    parser.add_argument("--agents", required=True, help="Comma-separated agent names (e.g. general,calculator)")
    parser.add_argument("--message", required=True, help="Message to send")
    parser.add_argument("--sender", default=None, help="Sender agent (defaults to first in --agents)")
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    asyncio.run(run(args.room_id, agents, args.message, args.sender))


if __name__ == "__main__":
    main()
