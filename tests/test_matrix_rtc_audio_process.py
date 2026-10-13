"""Tests for running GPT-Live call audio in its own process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_AGENT_STACK = ("agno", "nio", "mindroom.agents", "mindroom.matrix_rtc.call_manager", "mindroom.memory")


def _loaded_agent_stack(*modules: str) -> list[str]:
    """Import ``modules`` in a fresh interpreter and return the agent-stack modules that came with them."""
    script = (
        "import importlib, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        f"print(','.join(name for name in {_AGENT_STACK!r} if name in sys.modules))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True)
    return [name for name in result.stdout.strip().split(",") if name]


def test_live_audio_modules_import_without_the_agent_stack() -> None:
    """The call audio child imports these modules, so they must not pull in agents, Matrix, or the call manager."""
    assert _loaded_agent_stack("mindroom.matrix_rtc.live_voice_agent", "mindroom.matrix_rtc.focus") == []
