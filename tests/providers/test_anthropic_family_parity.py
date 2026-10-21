"""The owned Claude adapters reproduce every C1 provider golden of the Anthropic wire family.

Each golden pins the request Agno 3.0.9 plus MindRoom's patches sent for ``anthropic``, ``vertexai_claude``,
or ``bedrock_claude`` and the assistant message they parsed from the canned reply; the owned adapter must
produce the same request (method, URL, kept headers, JSON body) and the same message, metrics, and stop reason.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.providers.anthropic_family_support import (
    FAMILIES,
    FIXTURE_ROOT,
    expected_parsed,
    fixture_id,
    fixture_paths,
    run_golden,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURES = fixture_paths()


def test_every_claude_route_has_goldens() -> None:
    """The fixture tree still holds the three Claude routes this suite covers."""
    assert {path.parent.name for path in _FIXTURES} == set(FAMILIES)
    assert all((FIXTURE_ROOT / family).is_dir() for family in FAMILIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _FIXTURES, ids=fixture_id)
async def test_owned_adapter_reproduces_golden(path: Path, tmp_path: Path) -> None:
    """Replaying a golden's canned reply yields its exact request and parsed reply."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    produced = await run_golden(fixture, tmp_path)
    assert produced["request"] == fixture["request"]
    assert produced["parsed"] == expected_parsed(fixture)
