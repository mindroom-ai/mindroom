"""The owned Claude adapters reproduce every C1 provider golden of the Anthropic wire family.

Each golden pins the request Agno 3.0.9 plus MindRoom's patches sent for ``anthropic``, ``vertexai_claude``,
or ``bedrock_claude`` and the assistant message they parsed from the canned reply; the owned adapter must
produce the same request (method, URL, kept headers, JSON body) and the same message, metrics, and stop reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.providers.anthropic_family_support import FAMILIES, run_golden
from tests.providers.golden_support import (
    FIXTURE_ROOT,
    expected_parsed,
    expected_stop_reason,
    golden_id,
    golden_paths,
    load_golden,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURES = golden_paths(FAMILIES)


def test_every_claude_route_has_goldens() -> None:
    """The fixture tree still holds the three Claude routes this suite covers."""
    assert {path.parent.name for path in _FIXTURES} == set(FAMILIES)
    assert all((FIXTURE_ROOT / family).is_dir() for family in FAMILIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _FIXTURES, ids=golden_id)
async def test_owned_adapter_reproduces_golden(path: Path, tmp_path: Path) -> None:
    """Replaying a golden's canned reply yields its exact request and parsed reply."""
    fixture = load_golden(path)
    replay = await run_golden(fixture, tmp_path)
    assert replay.request == fixture["request"]
    assert replay.parsed == expected_parsed(fixture)
    assert replay.stop_reason == expected_stop_reason(fixture)
