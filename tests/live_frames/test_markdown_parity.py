"""The server-rendered HTML MindRoom Chat's live Markdown renderer must reproduce.

MindRoom Chat carries the same fixture and renders each case's Markdown with its own renderer; this test keeps the
runtime's side of that contract, so a renderer change here fails until the fixture, and Chat's renderer, follow it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindroom.matrix.message_builder import markdown_to_html

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "live-markdown-parity-v1.json").read_text(encoding="utf-8"),
)


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=[case["name"] for case in _FIXTURE["cases"]])
def test_the_runtime_renders_the_shared_markdown_fixture(case: dict[str, str]) -> None:
    """Every Markdown case renders to the HTML the fixture pins."""
    assert markdown_to_html(case["md"]) == case["html"]
