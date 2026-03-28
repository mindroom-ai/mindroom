#!/usr/bin/env python3
# ruff: noqa: D103 PLC0415
"""Live test: verify compaction token budget against a real session DB.

Usage:
    uv run python scripts/test_034b_compaction.py

Reads a real session from disk, computes the compaction budget, calls
_build_summary_input() with the budget, and asserts the output fits.
Does NOT mutate the database.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.compaction import (
    _build_summary_input,
    _estimate_serialized_run_tokens,
    estimate_text_tokens,
)
from mindroom.token_budget import compute_compaction_input_budget

DB_PATH = Path("/home/basnijholt/.mindroom-chat/mindroom_data/agents/openclaw/sessions/openclaw.db")
CONTEXT_WINDOW = 1_000_000  # Claude's context window
RESERVE_TOKENS = 16384  # default CompactionConfig.reserve_tokens


def main() -> None:
    if not DB_PATH.exists():
        print(f"Session DB not found: {DB_PATH}")
        sys.exit(1)

    storage = SqliteDb(session_table="openclaw_sessions", db_file=str(DB_PATH))

    # Load largest session directly — get_sessions() can fail on legacy rows
    import sqlite3

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT session_id, length(runs) as runs_len FROM openclaw_sessions ORDER BY runs_len DESC LIMIT 1",
    ).fetchall()
    if not rows:
        print("No sessions found in DB")
        sys.exit(1)

    session_id = rows[0]["session_id"]
    conn.close()
    from agno.db.base import SessionType

    session = storage.get_session(session_id, SessionType.AGENT)
    if session is None:
        print(f"Session {session_id} could not be loaded")
        sys.exit(1)
    all_runs = [r for r in (session.runs or []) if isinstance(r, (RunOutput, TeamRunOutput))]

    print(f"Session ID:     {session.session_id}")
    print(f"Total runs:     {len(all_runs)}")

    total_tokens = sum(_estimate_serialized_run_tokens(r) for r in all_runs)
    print(f"Total tokens:   {total_tokens:,}")

    summary_tokens = 0
    if session.summary:
        summary_tokens = estimate_text_tokens(session.summary.summary)
    print(f"Summary tokens: {summary_tokens:,}")

    budget = compute_compaction_input_budget(
        CONTEXT_WINDOW,
        reserve_tokens=RESERVE_TOKENS,
    )
    print(f"Context window: {CONTEXT_WINDOW:,}")
    print(f"Input budget:   {budget:,}")

    # Use half the runs as "compacted" candidates (simulate a cut)
    cut = max(1, len(all_runs) // 2)
    candidate_runs = all_runs[:cut]
    print(f"\nCandidate runs: {cut} (first half)")

    summary_input, included_runs = _build_summary_input(
        previous_summary=session.summary,
        compacted_runs=candidate_runs,
        max_input_tokens=budget,
    )

    input_tokens = estimate_text_tokens(summary_input)
    skipped = len(candidate_runs) - len(included_runs)

    print(f"Included runs:  {len(included_runs)}")
    print(f"Skipped runs:   {skipped}")
    print(f"Input tokens:   {input_tokens:,}")
    print(f"Within budget:  {input_tokens <= budget}")

    assert input_tokens <= budget, f"Input {input_tokens:,} exceeds budget {budget:,}"
    assert input_tokens < CONTEXT_WINDOW, f"Input {input_tokens:,} exceeds context window {CONTEXT_WINDOW:,}"
    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
