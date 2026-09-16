"""Immutable transport reference for a trusted tool-job completion event."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TOOL_JOB_COMPLETION_KEY = "org.mindroom.tool_job_completion"


@dataclass(frozen=True)
class ToolJobCompletion:
    """Exact outcome and frozen delivery transaction carried by trusted ingress."""

    job_id: str
    generation: int
    transaction_id: str
    recipient_user_id: str


def parse_tool_job_completion(source: dict[str, Any]) -> ToolJobCompletion | None:
    """Parse structured content only after the sender trust boundary has passed."""
    content = source.get("content", {})
    if not isinstance(content, dict):
        return None
    metadata = content.get(TOOL_JOB_COMPLETION_KEY)
    mentions = content.get("m.mentions", {})
    recipients = mentions.get("user_ids") if isinstance(mentions, dict) else None
    if not isinstance(metadata, dict) or not isinstance(recipients, list) or len(recipients) != 1:
        return None
    job_id, generation, transaction_id = (
        metadata.get("job_id"),
        metadata.get("generation"),
        metadata.get("transaction_id"),
    )
    if (
        not isinstance(job_id, str)
        or not job_id
        or type(generation) is not int
        or generation < 0
        or not isinstance(transaction_id, str)
        or not transaction_id
        or not isinstance(recipients[0], str)
    ):
        return None
    return ToolJobCompletion(job_id, generation, transaction_id, recipients[0])
