"""The ``io.mindroom.ai_run`` metadata each captured run published under Agno 3.0.9, as the golden tests compare it.

``tests/fixtures/agno_parity/events/ai_run.json`` holds the metadata, keyed by run id. A run's duration is a clock
reading, so the comparison sees only its presence.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.constants import AI_RUN_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping

GOLDEN_RUN_METADATA: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "agno_parity" / "events" / "ai_run.json").read_text(encoding="utf-8"),
)
_CLOCK = "<clock>"


def published_as_compared(content: Mapping[str, Any]) -> dict[str, Any]:
    """Return published run metadata as the comparison sees it: its duration, a clock reading, as a sentinel."""
    compared = copy.deepcopy(dict(content))
    usage = compared[AI_RUN_METADATA_KEY].get("usage", {})
    if "duration" in usage:
        usage["duration"] = _CLOCK
    return compared


def golden_metadata(run_id: str) -> dict[str, Any]:
    """Return the metadata the captured run ``run_id`` published, as the comparison sees it."""
    expected = published_as_compared(GOLDEN_RUN_METADATA[run_id])
    if run_id == "run-tools":
        # Agno's streamed reply merged the counters its requests reported into the usage, so the zero cache-write
        # count a request reported appeared as 0; the owned usage leaves zero counters out, as every blocking reply's
        # usage did.
        assert expected[AI_RUN_METADATA_KEY]["usage"].pop("cache_write_tokens") == 0
    return expected
