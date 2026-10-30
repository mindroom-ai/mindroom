"""A failed row v2026.10.627 stored without an error hands over its content, credentials redacted.

``v2026.10.627_rows.json`` holds the run dicts that release's agent loop and team runtime stored, unchanged; the
paused ones are read by ``tests/session_storage/test_legacy_paused_runs.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mindroom.llm.runs import RunRecord
from tests.llm.agno_dict_helpers import expected_run_dict

_ROWS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "legacy_run_content" / "v2026.10.627_rows.json").read_text(),
)


def test_a_failed_row_an_earlier_release_stored_hands_over_its_content_redacted() -> None:
    """A failed row stored without an error hands over its content, credentials redacted, and writes back as stored."""
    stored = _ROWS["agent_failed"]
    assert (stored["content"], "error" in stored) == ("provider down: api_key=unit-test-secret", False)

    run = RunRecord.from_dict(stored)

    assert run.handover_content == "provider down: api_key=***redacted***"
    assert run.to_dict() == expected_run_dict(stored)
