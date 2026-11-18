"""The chart schema, pinned by the fixture MindRoom Chat's validator shares."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindroom.ui_widgets.models import parse_widget

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ui-widgets-v1.json").read_text(encoding="utf-8"))
_STAGE_1 = [case for case in _FIXTURE["widgets"] if case["stage"] == 1]


@pytest.mark.parametrize("case", _STAGE_1, ids=[case["name"] for case in _STAGE_1])
def test_parse_widget_agrees_with_the_shared_fixture(case: dict[str, object]) -> None:
    """Every fixture block is a widget exactly when the fixture says so."""
    widget, errors = parse_widget(case["json"])
    assert (widget is not None) is case["valid"], errors
    assert bool(errors) is not case["valid"]


def test_parse_widget_never_raises_and_never_echoes_input() -> None:
    """Hostile input is "not a widget", and no error names a title, a label or a key."""
    for text in (
        "",
        "null",
        "[" * 100_000,
        "[" * 5_000,
        '{"type":"chart","series":7}',
        "\ud800",
        '{"type":"chart","kind":"bar","title":"\\ud800","labels":["a"],"series":[{"name":"s","data":[1]}]}',
        '{"type":"chart","title":"SECRET-TITLE","kind":"x"}',
        '{"type":"chart","SECRET-TITLE":1}',
        '{"type":"chart","kind":"bar","title":"t","labels":["a"],"series":[{"name":"SECRET-TITLE","data":[1,2]}]}',
    ):
        widget, errors = parse_widget(text)
        assert widget is None
        assert errors
        assert "SECRET-TITLE" not in " ".join(errors)
