"""Committed backlog consumption must remain visible to receive health."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from tests.test_bot_ready_hook import _agent_bot

if TYPE_CHECKING:
    from pathlib import Path


def test_acknowledged_backlog_advances_health_without_source_publication(tmp_path: Path) -> None:
    """Acknowledgements advance health while the source high-water mark is fixed."""
    bot = _agent_bot(tmp_path)
    session = MagicMock(spec=["progress_generation"])
    session.progress_generation = 7
    bot._ingestion_session = session

    assert bot.durable_ingestion_progress_generation() == 7
    bot._on_ingestion_batch_acknowledged()
    assert bot._ingestion_admission_progress.is_set()
    assert bot.durable_ingestion_progress_generation() == 8
    bot._on_ingestion_batch_acknowledged()
    assert bot.durable_ingestion_progress_generation() == 9
    session.progress_generation = 8
    assert bot.durable_ingestion_progress_generation() == 10
