"""Known agents and people get one name in the graph: as speakers, in the extraction instructions, and in lookups."""

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.knowledge_graph.names import EntityNames, KnownEntity, entity_names

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


AGENT_KEYS = {"@mindroom_mind:example.test": "mind", "@mindroom_scout:example.test": "scout"}


def _names(aliases: dict[str, list[str]]) -> EntityNames:
    config = Config(
        agents={"mind": MIND, "scout": AgentConfig(display_name="Scout \U0001f52d")},
        knowledge_graph={"aliases": aliases},
    )
    return entity_names(config, AGENT_KEYS)


def test_agents_are_named_by_display_name_and_known_by_key_and_matrix_id() -> None:
    """An agent's one name comes from its display name; its key, Matrix ID, and username are its other names."""
    names = _names({})

    scout = KnownEntity("Scout", "agent", ("@mindroom_scout:example.test", "mindroom_scout"))
    assert names.speaker("@mindroom_scout:example.test", "Scout \U0001f52d", "mind") == scout
    assert names.speaker(None, None, "scout") == scout
    assert names.speaker(None, None, "router").name == "RouterAgent"
    assert names.canonical("@MINDROOM_SCOUT:example.test") == "Scout"
    assert names.canonical("scout") == "Scout"


def test_an_entry_linking_a_matrix_id_names_that_person_everywhere() -> None:
    """The person behind a listed Matrix ID speaks, is listed, and is looked up under the entry's name."""
    names = _names({"Robin Vale": ["@robin:example.test", "Robin", "robin@example.test"]})

    robin = names.speaker("@robin:example.test", "Robbie \U0001f3b8", "mind")

    assert robin == KnownEntity("Robin Vale", "person", ("@robin:example.test", "Robin", "robin@example.test"))
    assert names.known_entities("mind", [robin])[1] == robin
    for name in ("ROBIN", "robin@example.test", "@robin:example.test"):
        assert names.canonical(name) == "Robin Vale"


def test_a_display_name_never_links_an_entry() -> None:
    """Only a stable ID links an entry, so someone else who calls themselves by an entry's name or alias keeps it."""
    names = _names({"Robin Vale": ["@robin:example.test", "Robin"]})

    assert names.speaker("@impostor:example.test", "Robin", "mind").name == "Robin"
    assert names.speaker("@impostor:example.test", "Robin Vale", "mind").aliases == (
        "@impostor:example.test",
        "impostor",
    )


def test_an_entry_linking_an_agent_key_names_that_agent_everywhere() -> None:
    """The agent behind a listed key speaks, is listed, and is looked up under the entry's name."""
    names = _names({"Mindy": ["mind", "the assistant"]})

    mindy = KnownEntity(
        "Mindy",
        "agent",
        ("mind", "the assistant", "@mindroom_mind:example.test", "mindroom_mind"),
    )
    assert names.speaker(None, None, "mind") == mindy
    assert names.speaker("@mindroom_mind:example.test", "Mind", "scout") == mindy
    assert names.known_entities("mind", []) == (mindy,)
    assert names.canonical("@mindroom_mind:example.test") == "Mindy"
    assert names.canonical("The Assistant") == "Mindy"


def test_plain_aliases_are_listed_and_looked_up() -> None:
    """An entry that links no agent or person still gives its names to the extraction instructions and to lookups."""
    names = _names({"Project Atlas": ["Atlas", "atlas-v2"]})

    assert names.known_entities("mind", [])[1:] == (KnownEntity("Project Atlas", None, ("Atlas", "atlas-v2")),)
    assert names.canonical("atlas") == "Project Atlas"
    assert names.canonical("Beacon") == "Beacon"


def test_no_name_in_an_episode_means_two_entities() -> None:
    """An alias that another listed entity is named, or that two of them go by, is left out of the episode's list.

    Entities with one name become one, as Graphiti merges them anyway.
    """
    names = _names({"Robin Vale": ["@robin:example.test", "Robin", "rob"], "Atlas": ["Mind"]})
    speakers = [
        names.speaker("@rob:example.test", "Robin", "mind"),
        names.speaker("@sam:a.test", "Sam", "mind"),
        names.speaker("@sam:b.test", "Sam", "mind"),
    ]

    known = names.known_entities("mind", speakers)

    assert known == (
        KnownEntity("Mind", "agent", ("@mindroom_mind:example.test", "mindroom_mind")),
        KnownEntity("Robin", "person", ("@rob:example.test",)),
        KnownEntity("Sam", "person", ("@sam:a.test", "@sam:b.test")),
        KnownEntity("Robin Vale", "person", ("@robin:example.test",)),
        KnownEntity("Atlas", None, ()),
    )
