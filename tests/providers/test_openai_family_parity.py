"""The owned OpenAI-family adapters send the requests and parse the replies the Agno-backed layer did."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.providers.golden_support import expected_stop_reason, golden_id, golden_paths, load_golden
from tests.providers.openai_family_support import OPENAI_FAMILIES, replay_golden

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = golden_paths(OPENAI_FAMILIES)


def test_every_openai_family_golden_is_replayed() -> None:
    """The parity suite covers every golden of the families the owned adapters serve."""
    assert {path.parent.name for path in GOLDENS} == set(OPENAI_FAMILIES)
    assert len(GOLDENS) == 255


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GOLDENS, ids=golden_id)
async def test_owned_adapter_reproduces_golden(path: Path, tmp_path: Path) -> None:
    """Request method, URL, kept headers, and body match, and the parsed reply matches field for field."""
    golden = load_golden(path)
    replay = await replay_golden(golden, tmp_path)
    assert replay.request == golden["request"]
    assert replay.parsed == golden["parsed"]
    assert replay.stop_reason == expected_stop_reason(golden)
