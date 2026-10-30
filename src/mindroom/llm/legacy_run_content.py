"""The content a failed run older releases stored: the failure's text in place of the model's."""

from __future__ import annotations

from mindroom.redaction import redact_sensitive_text

__all__ = ["legacy_failure_text"]


# LEGACY_COMPAT: Failed runs that store their failure text as their content.
# Legacy format: an ``ERROR`` run dict without an ``error`` key. The loop's ``_store_interrupted``, which also ended
# the run of a team member that could not be built (``fail_unbuilt_run``), stored the unredacted text of the failure
# as ``content`` when the model wrote nothing, else the model's text and no reason;
# delegation recovery's ``interrupt_child`` stored its reason as ``content`` in place of whatever the child had written:
# a fixed sentence, the child's failure text with credentials redacted, or, for a delegation it refused, the
# authorization result.
# Last legacy release: v2026.10.661, whose ``_store_interrupted`` (``agent_loop/run.py``) and ``interrupt_child``
# (``delegation/recovery.py``) wrote it, as the Agno runtime did before them; replacement: the next release, which
# stores the redacted failure text under ``error`` and keeps ``content`` for the model's text.
# Handling: such a run hands over its content, credentials redacted, as its failure, which is what those releases
# handed over for it, so a delegation settled from such a row still reports why the run failed.
# Coverage:
#   tests/llm/test_legacy_run_content.py::test_a_failed_row_an_earlier_release_stored_hands_over_its_content_redacted,
#   tests/test_delegation_audit.py::test_a_child_row_an_earlier_recovery_stopped_still_settles_with_its_reason
def legacy_failure_text(content: str | None) -> str | None:
    """Return what a failed run stored without an ``error`` hands over: its content, credentials redacted."""
    return None if content is None else redact_sensitive_text(content)
