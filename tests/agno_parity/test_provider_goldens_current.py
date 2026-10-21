"""The current Agno-backed provider layer reproduces every provider wire golden.

Task B8 deletes this test together with Agno; the owned adapters prove parity against the same fixtures.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.agno_parity.capture_providers import FAMILIES, FIXTURE_ROOT, family_scenarios, fixture_text, run_golden

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURES = sorted(FIXTURE_ROOT.glob("*/*.json"))


def test_every_family_scenario_has_one_golden() -> None:
    """The fixture tree holds exactly the goldens the capture script writes."""
    expected = {f"{family.name}/{name}" for family in FAMILIES for name in family_scenarios(family)}
    assert {f"{path.parent.name}/{path.stem}" for path in _FIXTURES} == expected


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda path: f"{path.parent.name}/{path.stem}")
def test_current_provider_layer_reproduces_golden(path: Path) -> None:
    """Replaying a golden's canned reply yields its exact request and parsed reply."""
    text = path.read_text(encoding="utf-8")
    fixture = json.loads(text)
    assert text == fixture_text(fixture), "golden is not in canonical form; rerun the capture script"
    produced = run_golden(fixture["input"], fixture["response_raw"])
    assert produced["request"] == fixture["request"]
    assert produced["parsed"] == fixture["parsed"]
