"""Immutable reference to one internal stored tool-job outcome."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolJobCompletion:
    """Exact job generation carried by the internal response owner."""

    job_id: str
    generation: int
