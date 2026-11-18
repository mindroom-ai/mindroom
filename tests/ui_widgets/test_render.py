"""The reply text and `io.mindroom.ui` value that `ui` blocks become, pinned by the fixture MindRoom Chat shares."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mindroom.matrix.message_builder import markdown_to_html
from mindroom.ui_widgets import render
from mindroom.ui_widgets.models import parse_widget
from mindroom.ui_widgets.render import (
    _canonical_widget_json,
    _fallback_lines,
    _fenced,
    _widget_id,
    recorded_ui_widgets,
    render_ui_widgets,
    without_open_ui_fence,
)

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ui-widgets-v1.json").read_text(encoding="utf-8"))
_RENDERS = [case for case in _FIXTURE["renders"] if case["stage"] == 1]
_VALID = [case for case in _FIXTURE["widgets"] if case["stage"] == 1 and case["valid"]]

CHART = '{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180.5,150]}],"y":"users"}'
BLOCK = "```mindroom-ui-df4d729a5d\nSignups (line chart, users)\n     Signups\nJan      120\nFeb    180.5\nMar      150\n```"
OPEN = 'Signups peaked.\n\n```ui\n{"type":"chart",'


def _chart(**changes: object) -> str:
    return json.dumps({**json.loads(CHART), **changes}, ensure_ascii=False)


def _ui(json_text: str) -> str:
    return f"```ui\n{json_text}\n```"


def test_canonical_json_and_id_match_the_spec() -> None:
    """The spec's chart has the spec's canonical text and id, and a used id is skipped."""
    widget, _ = parse_widget(CHART)
    canonical = _canonical_widget_json(widget)
    assert (
        canonical
        == '{"kind":"line","labels":["Jan","Feb","Mar"],"series":[{"data":[120,180.5,150],"name":"Signups"}],"title":"Signups","type":"chart","y":"users"}'
    )
    assert _widget_id(canonical) == "df4d729a5d"
    assert _widget_id(canonical, taken={"df4d729a5d"}) == hashlib.sha256((canonical + "#1").encode()).hexdigest()[:10]


@pytest.mark.parametrize("case", _VALID, ids=[case["name"] for case in _VALID])
def test_canonical_json_and_id_agree_with_the_shared_fixture(case: dict[str, object]) -> None:
    """Every valid fixture block has the canonical text and id the client is tested against."""
    widget, _ = parse_widget(case["json"])
    canonical = _canonical_widget_json(widget)
    assert canonical == case["canonical"]
    assert _widget_id(canonical) == case["id"]
    assert parse_widget(canonical)[0] == widget


def test_chart_fallback_is_the_spec_table() -> None:
    """The fallback of the spec's chart is the spec's block."""
    assert "\n".join(_fenced(_fallback_lines(parse_widget(CHART)[0]), "df4d729a5d")) == BLOCK


def test_rendering_is_idempotent_and_keeps_the_widget_only_for_raw_blocks() -> None:
    """A rendered text renders to itself, with no widget for its marker block."""
    raw = f"Before.\n\n{_ui(CHART)}\n\nAfter."
    once = render_ui_widgets(raw, in_progress=False)
    assert once.text == f"Before.\n\n{BLOCK}\n\nAfter."
    assert once.content == {
        "version": 1,
        "widgets": [{"id": "df4d729a5d", "json": _canonical_widget_json(parse_widget(CHART)[0])}],
    }
    twice = render_ui_widgets(once.text, in_progress=False)
    assert twice.text is once.text
    assert twice.content is None


def test_a_copied_marker_block_stays_inert_beside_an_identical_new_widget() -> None:
    """A marker block already in the text keeps its id to itself."""
    result = render_ui_widgets(f"{BLOCK}\n\n{_ui(CHART)}", in_progress=False)
    (entry,) = result.content["widgets"]
    assert entry["id"] != "df4d729a5d"
    assert result.text.startswith(BLOCK)
    assert f"mindroom-ui-{entry['id']}" in result.text


def test_text_without_a_ui_fence_is_returned_as_is() -> None:
    """Text with no column-0 `ui` fence is the same object, line endings and all."""
    text = "Hello\r\n\n```python\nx = '```ui'\n```\n"
    result = render_ui_widgets(text, in_progress=False)
    assert result.text is text
    assert result.content is None


def test_an_invalid_block_is_left_as_written() -> None:
    """A closed block that is not a widget stays the code block the model wrote."""
    text = f"{_ui('{"type":"map"}')}\n\n{_ui(_chart(color='red'))}"
    for in_progress in (True, False):
        result = render_ui_widgets(text, in_progress=in_progress)
        assert result.text is text
        assert result.content is None


def test_a_title_of_backticks_gets_a_longer_fence() -> None:
    """Backticks in a string cannot close the block."""
    result = render_ui_widgets(_ui(_chart(title="``` and ````")), in_progress=False)
    assert result.text.startswith("`````mindroom-ui-")
    assert result.text.endswith("\n`````")
    html = markdown_to_html(result.text)
    assert html.count("<pre>") == 1
    assert "``` and ```` (line chart, users)" in html


def test_strings_are_collapsed_to_one_line() -> None:
    """A newline or a fence inside a label adds no line to the block."""
    result = render_ui_widgets(_ui(_chart(labels=["a\n```\nb", "Feb", "Mar"])), in_progress=False)
    lines = result.text.split("\n")
    assert len(lines) == 7
    assert lines[3] == "a ``` b      120"
    assert markdown_to_html(result.text).count("<pre>") == 1


def test_a_45_point_chart_shows_40_rows() -> None:
    """A long table keeps its first and last 20 rows around a count line."""
    labels = [f"d{i}" for i in range(45)]
    result = render_ui_widgets(
        _ui(_chart(labels=labels, series=[{"name": "n", "data": list(range(45))}])),
        in_progress=False,
    )
    lines = result.text.split("\n")
    assert lines[23] == "... 5 more rows ..."
    assert lines[22].split() == ["d19", "19"]
    assert lines[24].split() == ["d25", "25"]
    assert len(lines) == 2 + 2 + 41


def test_large_whole_numbers_keep_their_exponent() -> None:
    """A whole number past 2^53 is not spelled out digit by digit."""
    result = render_ui_widgets(
        _ui(_chart(labels=["a"], series=[{"name": "n", "data": [1e300]}])),
        in_progress=False,
    )
    assert '"data":[1e+300]' in result.content["widgets"][0]["json"]
    assert result.text.split("\n")[3] == "a  1e+300"


def test_entries_stop_at_8_and_at_16384_bytes() -> None:
    """Past either limit a block still shows its fallback, without a widget."""
    nine = render_ui_widgets("\n\n".join(_ui(_chart(title=f"C{i}")) for i in range(9)), in_progress=False)
    assert nine.text.count("```mindroom-ui-") == 9
    assert len(nine.content["widgets"]) == 8

    def big(title: str) -> str:
        labels = [f"{i:040d}" for i in range(200)]
        return _ui(_chart(title=title, labels=labels, series=[{"name": "n", "data": list(range(200))}]))

    two = render_ui_widgets(f"{big('A')}\n\n{big('B')}", in_progress=False)
    assert two.text.count("```mindroom-ui-") == 2
    (entry,) = two.content["widgets"]
    assert 9_000 < len(entry["json"].encode()) <= 16_384
    assert '"title":"A"' in entry["json"]


def test_identical_blocks_share_one_entry() -> None:
    """The same block written twice draws one widget in both places."""
    result = render_ui_widgets(f"{_ui(CHART)}\n\nAgain:\n\n{_ui(CHART)}", in_progress=False)
    assert result.text == f"{BLOCK}\n\nAgain:\n\n{BLOCK}"
    assert [entry["id"] for entry in result.content["widgets"]] == ["df4d729a5d"]


def test_only_column_0_fences_count() -> None:
    """A fence in a list item, a quote or another code block is not a widget; a tilde fence is."""
    quoted = "\n".join(f"> {line}" for line in _ui(CHART).split("\n"))
    listed = "\n".join(f"  {line}" for line in _ui(CHART).split("\n"))
    untouched = f"- item\n\n{listed}\n\n{quoted}\n\n````\n{_ui(CHART)}\n````\n\n"
    result = render_ui_widgets(f"{untouched}~~~ui\n{CHART}\n~~~", in_progress=False)
    assert result.text == untouched + BLOCK
    assert len(result.content["widgets"]) == 1
    only = render_ui_widgets(untouched, in_progress=False)
    assert only.text is untouched
    assert only.content is None


def test_a_block_glued_to_a_paragraph_gets_blank_lines() -> None:
    """The fallback block is set off by a blank line on each side."""
    result = render_ui_widgets(f"Before.\n{_ui(CHART)}\nAfter.", in_progress=False)
    assert result.text == f"Before.\n\n{BLOCK}\n\nAfter."


def test_an_open_fence_is_cut_only_in_progress() -> None:
    """While the reply streams, an unfinished block is hidden; a finished reply shows what was written."""
    streaming = render_ui_widgets(OPEN, in_progress=True)
    assert streaming.text == "Signups peaked."
    assert streaming.content is None
    final = render_ui_widgets(OPEN, in_progress=False)
    assert final.text is OPEN
    assert final.content is None
    after_a_chart = render_ui_widgets(f"{_ui(CHART)}\n\n{OPEN}", in_progress=True)
    assert after_a_chart.text == f"{BLOCK}\n\nSignups peaked."
    assert len(after_a_chart.content["widgets"]) == 1
    for not_yet_ui in ("Hi\n\n```", "Hi\n\n```u", "Hi\n\n```uix"):
        assert render_ui_widgets(not_yet_ui, in_progress=True).text is not_yet_ui
    # The opening line alone is hidden too: a note written below it would complete the line and land in the block.
    assert render_ui_widgets("Hi\n\n```ui", in_progress=True).text == "Hi"
    assert without_open_ui_fence("Hi\n\n```ui") == "Hi"


def test_without_open_ui_fence() -> None:
    """Only an open column-0 `ui` fence is cut, and closed blocks stay as written."""
    closed = f"Intro\n\n{_ui(CHART)}"
    assert without_open_ui_fence(f"{closed}\n\n{OPEN}") == f"{closed}\n\nSignups peaked."
    assert without_open_ui_fence(closed) is closed
    inside_another = f"````\n{OPEN}"
    assert without_open_ui_fence(inside_another) is inside_another
    prose = "No fence here."
    assert without_open_ui_fence(prose) is prose


def test_text_a_stream_already_rendered_gets_its_widgets_from_the_text_as_written() -> None:
    """A write that shows rendered text again finds its widgets in the reply's record, and nowhere else."""
    raw = f"Before.\n\n{_ui(CHART)}\n\nAfter."
    once = render_ui_widgets(raw, in_progress=False)
    assert recorded_ui_widgets(once.text, raw) == once.content
    # Only the widgets the shown text has a block for.
    other = _ui(_chart(title="Other"))
    assert recorded_ui_widgets(once.text, f"{raw}\n\n{other}") == once.content
    assert recorded_ui_widgets("Before.", raw) is None
    # A marker block the record did not author as a `ui` block stays inert.
    assert recorded_ui_widgets(once.text, "Before.\n\nAfter.") is None
    assert recorded_ui_widgets(BLOCK, BLOCK) is None


@pytest.mark.parametrize(
    "before",
    [
        "Example:\n\n    ```\n\n",  # an indented code sample that only looks like a fence
        "- item\n\n  ```python\n  x = 1\n\nBack out.\n\n",  # a fence its list ends without closing
        "```python\nx = 1\n  ```\n\n",  # a block closed by an indented fence
    ],
)
def test_code_before_a_chart_ends_where_markdown_ends_it(before: str) -> None:
    """A widget is found wherever the HTML shows a top-level `ui` code block, and hidden while it is open."""
    assert markdown_to_html(before + _ui(CHART)).count('<pre><code class="language-ui">') == 1
    result = render_ui_widgets(before + _ui(CHART), in_progress=False)
    assert [entry["id"] for entry in result.content["widgets"]] == ["df4d729a5d"]
    assert result.text == before + BLOCK
    open_block = before + '```ui\n{"type":"chart",'
    assert render_ui_widgets(open_block, in_progress=True).text == before.rstrip()
    assert without_open_ui_fence(open_block) == before.rstrip()


def test_raw_html_before_a_fence_is_read_as_the_html_renderer_reads_it() -> None:
    """The renderer adds a line after `<hr>`, which frees one fence and lets another swallow a chart."""
    freed = render_ui_widgets(f"<hr>\n{_ui(CHART)}", in_progress=False)
    assert freed.text == f"<hr>\n\n{BLOCK}"
    assert 'class="language-mindroom-ui-df4d729a5d"' in markdown_to_html(freed.text)
    swallowed = f"<hr>\n`````\ntext\n\n{_ui(CHART)}"
    assert "language-ui" not in markdown_to_html(swallowed)
    assert render_ui_widgets(swallowed, in_progress=False).text is swallowed


def test_a_chart_closed_by_an_indented_fence_is_a_widget() -> None:
    """Markdown closes a fence indented up to three spaces."""
    result = render_ui_widgets(f"```ui\n{CHART}\n   ```\n\nAfter.", in_progress=True)
    assert result.text == f"{BLOCK}\n\nAfter."
    assert len(result.content["widgets"]) == 1


def test_only_a_marker_block_reserves_or_recovers_an_id() -> None:
    """Prose that names a marker neither changes a widget's id nor stands for its block."""
    prose = "The block mindroom-ui-df4d729a5d is a marker."
    result = render_ui_widgets(f"{prose}\n\n{_ui(CHART)}", in_progress=False)
    assert [entry["id"] for entry in result.content["widgets"]] == ["df4d729a5d"]
    assert recorded_ui_widgets(prose, _ui(CHART)) is None
    nested = "\n".join(f"> {line}" for line in BLOCK.split("\n"))
    (entry,) = render_ui_widgets(f"{nested}\n\n{_ui(CHART)}", in_progress=False).content["widgets"]
    assert entry["id"] != "df4d729a5d"


def test_block_results_are_memoized() -> None:
    """A live tick does not validate a closed block again."""
    block = _ui(_chart(title="Memoized once"))
    with patch.object(render, "parse_widget", wraps=parse_widget) as parse:
        first = render_ui_widgets(f"{block}\n\nMore", in_progress=True)
        second = render_ui_widgets(f"{block}\n\nMore text", in_progress=True)
    assert parse.call_count == 1
    assert first.content == second.content


@pytest.mark.parametrize("case", _RENDERS, ids=[case["name"] for case in _RENDERS])
def test_render_agrees_with_the_shared_fixture(case: dict[str, object]) -> None:
    """The transform and the HTML of its text are what the client is tested against."""
    result = render_ui_widgets(case["markdown"], in_progress=case["in_progress"])
    assert result.text == case["text"]
    assert result.content == case["ui"]
    assert markdown_to_html(result.text) == case["html"]


def test_the_fixture_pins_the_spec_reply() -> None:
    """The hand-pinned case is in the fixture, so an empty fixture cannot pass."""
    assert _RENDERS[0]["ui"]["widgets"][0]["id"] == "df4d729a5d"
    assert BLOCK in _RENDERS[0]["text"]
    assert len(_RENDERS) >= 9
