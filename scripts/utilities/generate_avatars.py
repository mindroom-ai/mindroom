"""Generate and set avatars for all agents, teams, and rooms."""

import asyncio
import sys

from mindroom.avatar_generation import run_avatar_generation

if __name__ == "__main__":
    asyncio.run(run_avatar_generation(set_only="--set-only" in sys.argv))
