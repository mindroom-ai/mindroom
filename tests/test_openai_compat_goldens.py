"""The OpenAI-compatible API sends the bodies of its stored wire golden, from the production path.

``tests.agno_parity.openai_compat_golden`` sends the golden's requests: a blocking agent reply, a streamed agent reply
with a tool call, and a blocking and a streamed coordinate team reply.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.agno_parity.golden_files import render
from tests.agno_parity.openai_compat_golden import GOLDEN, regenerated_golden

if TYPE_CHECKING:
    from pathlib import Path


def test_the_api_sends_the_golden_bodies(tmp_path: Path) -> None:
    """Blocking and streamed agent and team completions match the golden: text, tool blocks, member slots, usage."""
    regenerated = regenerated_golden(tmp_path)

    text = GOLDEN.read_text(encoding="utf-8")
    assert regenerated == json.loads(text)
    assert render(regenerated) == text
