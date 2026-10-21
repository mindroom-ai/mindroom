"""Capture the model requests MindRoom builds with Agno 3.0.9 as prompt goldens.

The scenarios live in `tests.agno_parity.prompt_scenarios`; this script runs them and writes their goldens.
Run ``.venv/bin/python -m tests.agno_parity.capture_prompts`` from the repository root to rewrite the fixtures.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from tests.agno_parity.prompt_scenarios import FIXTURE_DIR, SCENARIOS, Recorder, call_dict, normalize


async def capture(name: str, root: Path) -> dict[str, Any]:
    """Run one scenario under ``root`` and return its normalized golden."""
    scenario = SCENARIOS[name]
    recorder = Recorder(responder=scenario.responder)
    with recorder.installed():
        output = await scenario.run(root)
    golden = {
        "scenario": name,
        "description": scenario.description,
        "input": output.input,
        "requests": [call_dict(call) for call in recorder.ordered_calls()],
    }
    if output.exported is not None:
        golden["exported"] = output.exported
    return normalize(golden, root)


def write_fixture(name: str, golden: dict[str, Any]) -> None:
    """Write one golden as stable, reviewable JSON."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (FIXTURE_DIR / f"{name}.json").write_text(text, encoding="utf-8")


async def _capture_all(names: list[str]) -> None:
    for name in names:
        with tempfile.TemporaryDirectory(prefix="prompt-golden-") as root:
            write_fixture(name, await capture(name, Path(root)))


def main() -> None:
    """Capture the named scenarios, or all of them."""
    asyncio.run(_capture_all(sys.argv[1:] or sorted(SCENARIOS)))


if __name__ == "__main__":
    main()
