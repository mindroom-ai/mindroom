"""Each reply and approval resume publishes the ``io.mindroom.ai_run`` metadata of its golden run.

``tests.ai_run_golden_helpers`` replays every golden run through the entry point that published it, or resumes it from
the store the capture dumped just before its resume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.ai_run_golden_helpers import GOLDEN_RUN_METADATA, published_as_compared, published_metadata

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", sorted(GOLDEN_RUN_METADATA))
async def test_a_reply_publishes_the_golden_run_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """A text, tool, media, failed, compacted, or team run's reply, or an approval resume, publishes its metadata.

    A resumed agent or team answers its resumed requests as its stored run's last assistant message did.
    """
    published = await published_metadata(run_id, tmp_path, monkeypatch)

    assert published_as_compared(published) == GOLDEN_RUN_METADATA[run_id]
