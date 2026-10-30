"""The rendered summaries earlier releases stored in place of a stopped or failed turn's own record."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.llm.runs import AnyRunRecord


# LEGACY_COMPAT: Stopped and failed turns stored as a completed run holding a rendered summary.
# Legacy format: a top-level ``COMPLETED`` run whose metadata holds ``mindroom_replay_state: "interrupted"`` and
# ``mindroom_original_status``, saved over the stopped, failed, or unspoken run's own record: the turn's user message,
# then one assistant message of the partial text, a "(turn stopped before completion ...)" status line, and the
# finished calls' redacted previews.
# Last legacy release: v2026.10.739; replacement: the next release, which keeps the run's own record under the status
# ``INTERRUPTED``.
# Handling: such a run replays as stored, its summary included, as before the upgrade; only ``utterances`` reads the
# flag, leaving the summary, which can quote tool previews, out of what background readers take as the agent's words.
# Coverage: tests/test_interrupted_replay.py::test_a_stored_rendered_summary_still_replays_as_it_did,
# tests/test_interrupted_replay.py::test_neither_a_stopped_reply_nor_a_rendered_summary_is_the_agents_words.
def is_rendered_interruption_summary(run: AnyRunRecord) -> bool:
    """Return whether a stored run is an earlier release's rendered summary of a stopped or failed turn."""
    return (run.metadata or {}).get("mindroom_replay_state") == "interrupted"


__all__ = ["is_rendered_interruption_summary"]
