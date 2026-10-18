"""Known agents and people get one name in the graph: as speakers, in the extraction instructions, and in lookups."""

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.knowledge_graph.names import entity_names

MIND = AgentConfig(display_name="Mind")


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        pytest.param("Alice Example", "Alice Example", id="plain"),
        pytest.param("  Alice\tExample \U0001f495\ufe0f ", "Alice Example", id="emoji-and-spacing"),
        pytest.param("Dr: Who", "Dr Who", id="colon"),
        pytest.param("Mary-Jane O'Brien Jr.", "Mary-Jane O'Brien Jr.", id="kept-punctuation"),
        pytest.param("Jose\u0301 Ra\u0308hul", "Jose\u0301 Ra\u0308hul", id="combining-marks"),
        pytest.param("\U0001f525 :: \U0001f525", "alice", id="only-symbols"),
        pytest.param(None, "alice", id="none"),
    ],
)
def test_a_person_is_named_by_their_display_name_without_symbols(display_name: str | None, expected: str) -> None:
    """Only letters, digits, spaces, and .'- stay, and a name with no letter or digit left falls back to the username."""
    names = entity_names(Config(agents={"mind": MIND}), {})

    assert names.speaker("@alice:example.test", display_name, "mind").name == expected
