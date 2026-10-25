"""Each reply and approval resume publishes the ``io.mindroom.ai_run`` metadata of its golden run.

``tests.ai_run_golden_helpers`` replays every golden run through the entry point that published it, or resumes it from
the store the capture dumped just before its resume, and ``golden_metadata`` names where the expected metadata departs
from the capture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.ai_run_golden_helpers import (
    GOLDEN_RUN_METADATA,
    RUNS,
    WITHOUT_RECORD,
    golden_metadata,
    published_as_compared,
    published_metadata,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_only_the_cut_run_has_no_stored_record() -> None:
    """Every golden run but the one an edit cut removed has a stored record to replay it from."""
    assert {run_id for run_id in GOLDEN_RUN_METADATA if run_id not in RUNS} == WITHOUT_RECORD


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", sorted(set(GOLDEN_RUN_METADATA) - WITHOUT_RECORD))
async def test_a_reply_publishes_the_golden_run_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """A text, tool, media, failed, compacted, or team run's reply, or an approval resume, publishes its metadata.

    A resumed agent or team answers its resumed requests as its stored run's last assistant message did.
    """
    published = await published_metadata(run_id, tmp_path, monkeypatch)

    assert published_as_compared(published) == golden_metadata(run_id)
