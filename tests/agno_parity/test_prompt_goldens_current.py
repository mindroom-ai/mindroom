"""The Agno-backed runtime still builds exactly the model requests its prompt goldens pin."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.agno_parity.capture_prompts import FIXTURE_DIR, SCENARIOS, capture

if TYPE_CHECKING:
    from pathlib import Path


def test_every_prompt_golden_has_a_scenario() -> None:
    """A fixture without a scenario could never be checked, and a scenario without a fixture pins nothing."""
    assert {path.stem for path in FIXTURE_DIR.glob("*.json")} == set(SCENARIOS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_prompt_golden_matches_current_runtime(name: str, tmp_path: Path) -> None:
    """Every request a scenario sends matches its golden: messages, tools, tool choice, and response format."""
    golden = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))

    assert await capture(name, tmp_path) == golden
