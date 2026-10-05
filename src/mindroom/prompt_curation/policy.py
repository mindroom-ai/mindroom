"""When a curation pass is due, how far it may cut, and whether its result is kept.

Agents told to "clean up" tend to cut most of a file, so the bounds live here, in code, not in the prompt alone:
each pass is asked for a concrete band, and a result outside the guards is discarded.
All sizes are ``estimate_text_tokens`` estimates, the measure behind ``static_prompt_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.prompt_curation import PromptCurationConfig

MEMORY_DIR_PREFIX = "memory/"


@dataclass(frozen=True)
class PassBounds:
    """The measured size of the curatable files and the band one pass must land in."""

    measured_tokens: int
    trigger_tokens: int
    stop_tokens: int
    upper_tokens: int
    floor_tokens: int


@dataclass(frozen=True)
class PassMeasurement:
    """Sizes a pass is judged by: each curatable file, all memory content, and which paths changed."""

    curated: Mapping[str, int]
    total_memory_tokens: int
    changed_paths: frozenset[str]

    @property
    def curated_tokens(self) -> int:
        """Return the curatable files' total."""
        return sum(self.curated.values())


def _effective_trigger_tokens(settings: PromptCurationConfig, context_window: int | None) -> int:
    """Return the trigger, lowered to the configured share of a known context window."""
    if settings.trigger_context_fraction is None or context_window is None:
        return settings.trigger_tokens
    return min(settings.trigger_tokens, round(settings.trigger_context_fraction * context_window))


def _stop_tokens(settings: PromptCurationConfig, trigger_tokens: int) -> int:
    return round(settings.target_ratio * trigger_tokens)


def next_active(
    measured_tokens: int,
    settings: PromptCurationConfig,
    context_window: int | None,
    *,
    active: bool,
) -> bool:
    """Return whether curation stays due: it starts above the trigger and stops once under the target."""
    trigger_tokens = _effective_trigger_tokens(settings, context_window)
    if measured_tokens > trigger_tokens:
        return True
    if measured_tokens <= _stop_tokens(settings, trigger_tokens):
        return False
    return active


def plan_pass(
    measured_tokens: int,
    settings: PromptCurationConfig,
    context_window: int | None,
    *,
    active: bool,
) -> PassBounds | None:
    """Return the band the next pass must land in, or None when no pass is due.

    The pass is asked to cut at least ``min_reduction_per_pass``, or only down to the stop target when that is
    closer, and never more than ``max_reduction_per_pass``.
    """
    if not next_active(measured_tokens, settings, context_window, active=active):
        return None
    trigger_tokens = _effective_trigger_tokens(settings, context_window)
    stop_tokens = _stop_tokens(settings, trigger_tokens)
    upper_tokens = max(round(measured_tokens * (1 - settings.min_reduction_per_pass)), stop_tokens)
    slack_tokens = round(measured_tokens * (settings.max_reduction_per_pass - settings.min_reduction_per_pass))
    return PassBounds(
        measured_tokens=measured_tokens,
        trigger_tokens=trigger_tokens,
        stop_tokens=stop_tokens,
        upper_tokens=upper_tokens,
        floor_tokens=upper_tokens - slack_tokens,
    )


def max_content_loss_tokens(before: PassMeasurement, settings: PromptCurationConfig) -> int:
    """Return how many tokens of memory content a pass may drop net, relative to the curatable files."""
    return round(settings.max_content_loss * before.curated_tokens)


def file_shrink_violation(
    path: str,
    before_tokens: int,
    after_tokens: int,
    settings: PromptCurationConfig,
) -> str | None:
    """Describe a curatable file that shrank more than one pass allows, or return None."""
    if before_tokens <= 0 or after_tokens >= before_tokens * (1 - settings.max_file_shrink):
        return None
    shrink_percent = round(100 * (before_tokens - after_tokens) / before_tokens)
    return f"{path} shrank {shrink_percent}% (more than {round(100 * settings.max_file_shrink)}%)"


def floor_violation(curated_tokens: int, bounds: PassBounds) -> str | None:
    """Describe a curated total below the pass's floor, or return None."""
    if curated_tokens >= bounds.floor_tokens:
        return None
    return f"curated files total {curated_tokens} tokens, below the floor of {bounds.floor_tokens}"


def validate_pass(
    before: PassMeasurement,
    after: PassMeasurement,
    bounds: PassBounds,
    settings: PromptCurationConfig,
) -> list[str]:
    """Return every reason to discard a pass's result; an empty list keeps it."""
    violations = [
        violation
        for path, before_tokens in before.curated.items()
        if (violation := file_shrink_violation(path, before_tokens, after.curated.get(path, 0), settings))
    ]
    if violation := floor_violation(after.curated_tokens, bounds):
        violations.append(violation)
    changed = sorted(after.changed_paths)
    violations.extend(f"protected file {path} changed" for path in changed if path in settings.protected_files)
    violations.extend(
        f"{path} is neither curatable nor under memory/"
        for path in changed
        if path not in settings.files
        and path not in settings.protected_files
        and not path.startswith(MEMORY_DIR_PREFIX)
    )
    if after.curated_tokens >= before.curated_tokens:
        violations.append(f"curated files did not shrink ({after.curated_tokens} tokens)")
    lost_tokens = before.total_memory_tokens - after.total_memory_tokens
    if lost_tokens > (max_loss := max_content_loss_tokens(before, settings)):
        violations.append(
            f"memory content dropped {lost_tokens} tokens net (more than {max_loss}); "
            "move detail to memory/ instead of deleting it",
        )
    return violations
