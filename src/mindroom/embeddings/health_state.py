"""Process-wide embedder request health, scoped to the active embedder generation."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

_failure_lock = Lock()
_current_failure: str | None = None
# Bumped when a reload changes the active embedder or its use, so a slow
# probe holding a pre-reload config snapshot cannot overwrite newer health.
_health_generation = 0


@dataclass(frozen=True)
class EmbedderHealthRecorder:
    """Generation-bound writer for embedding request outcomes."""

    generation: int

    def record(self, error: str | None) -> bool:
        """Record an outcome only while this recorder is current."""
        return _record_embedder_health_for_generation(self.generation, error)

    def is_current(self) -> bool:
        """Return whether this recorder still belongs to the active config."""
        return self.generation == _health_generation_snapshot()


def get_embedder_failure() -> str | None:
    """Return the last recorded embedder failure, or None when healthy."""
    with _failure_lock:
        return _current_failure


def _health_generation_snapshot() -> int:
    with _failure_lock:
        return _health_generation


def capture_embedder_health_recorder() -> EmbedderHealthRecorder:
    """Capture a writer that cannot mutate health after a config reload."""
    return EmbedderHealthRecorder(_health_generation_snapshot())


def _record_embedder_health_for_generation(generation: int, error: str | None) -> bool:
    """Record a probe outcome unless the config generation moved on."""
    global _current_failure
    with _failure_lock:
        if _health_generation != generation:
            return False
        _current_failure = error
        return True


def reset_embedder_health_generation() -> EmbedderHealthRecorder:
    """Clear recorded health and return a writer for the new generation."""
    global _current_failure, _health_generation
    with _failure_lock:
        _health_generation += 1
        _current_failure = None
        return EmbedderHealthRecorder(_health_generation)


__all__ = [
    "EmbedderHealthRecorder",
    "capture_embedder_health_recorder",
    "get_embedder_failure",
    "reset_embedder_health_generation",
]
