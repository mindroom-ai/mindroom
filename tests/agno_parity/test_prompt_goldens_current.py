"""The Agno-backed runtime still builds exactly the model requests its prompt goldens pin."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.agno_parity.capture_prompts import capture
from tests.agno_parity.prompt_scenarios import FIXTURE_DIR, SCENARIOS, load_golden

if TYPE_CHECKING:
    from pathlib import Path


def test_every_prompt_golden_has_a_scenario() -> None:
    """A fixture without a scenario could never be checked, and a scenario without a fixture pins nothing."""
    assert {path.stem for path in FIXTURE_DIR.glob("*.json")} == set(SCENARIOS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_prompt_golden_matches_current_runtime(name: str, tmp_path: Path) -> None:
    """Every request a scenario sends matches its golden: messages, tools, tool choice, and response format."""
    assert await capture(name, tmp_path) == load_golden(name)
