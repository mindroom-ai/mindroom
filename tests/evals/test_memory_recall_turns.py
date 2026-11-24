"""Turn extraction from LLM request logs for the memory recall benchmark."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import tiktoken

from evals.memory_recall.turns import Turn, extract_turns, read_turns, write_turns

FIXTURE = Path(__file__).parent / "fixtures" / "memory_recall" / "requests.jsonl"
HEADER = (
    "[Automatically extracted agent file memories - may not be relevant to current context]\n"
    "Previous agent file memories that might be related:\n"
)
SHED_HITS = (
    "[id=m_20260101000000_aaaa0001] The garden shed is painted green.",
    "[id=m_20260101000000_aaaa0002] The cat is called Pixel.",
    "[id=m_20260101000000_aaaa0003] Tea is brewed at 80 degrees.",
)


def _turns() -> dict[str, Turn]:
    turns = extract_turns([FIXTURE], {"alpha"})
    by_session = {turn.session_id: turn for turn in turns}
    assert len(by_session) == len(turns)
    return by_session


def _o200k(text: str) -> int:
    return len(tiktoken.get_encoding("o200k_base").encode_ordinary(text))


def test_extracts_new_format_turn() -> None:
    """A current-format request yields its turn's time, agent, hits and memory token count."""
    turn = _turns()["sess-new"]
    assert turn.agent == "alpha"
    assert turn.ts == datetime.fromisoformat("2026-01-01T10:00:00-08:00")
    assert turn.injected_hits == SHED_HITS
    memory_part = "<mindroom_transient_context>\n" + HEADER + "".join(f"- {hit}\n" for hit in SHED_HITS) + "\n"
    assert turn.injected_tokens == _o200k(memory_part)


def test_extracts_old_format_turn() -> None:
    """An old-format request with top-level messages yields a turn too."""
    turn = _turns()["sess-old"]
    assert turn.agent == "alpha"
    assert turn.ts == datetime.fromisoformat("2025-12-01T09:00:00-08:00")
    assert turn.injected_hits == (
        "[id=m_20251201000000_dddd0001] Tea is brewed at 80 degrees.",
        "[id=m_20251201000000_dddd0002] Green tea needs 2 minutes.",
    )


def test_dedupes_tool_loop_requests() -> None:
    """The requests of one turn's tool loop share its memory block and give one turn."""
    turns = extract_turns([FIXTURE], {"alpha"})
    assert [turn.session_id for turn in turns] == ["sess-new", "sess-parts", "sess-old"]


def test_query_is_current_message() -> None:
    """The query is the text after `Current message:` when present, else the logged turn prompt."""
    turns = _turns()
    assert turns["sess-new"].query == '<msg from="@user:example.org"><![CDATA[What colour is the shed?]]></msg>'
    # The current message and the memory block can share one user message; the query stops at the block.
    assert turns["sess-parts"].query == '<msg from="@user:example.org"><![CDATA[Where is the key?]]></msg>'
    # Old-format logs carry no `Current message:` line, so the query is the logged turn prompt.
    assert turns["sess-old"].query == "Remind me about tea."


def test_hits_split_on_list_items() -> None:
    """Each `- ` list item after the header is one hit."""
    turns = _turns()
    assert len(turns["sess-new"].injected_hits) == 3
    # A block without a message context ends at the closing tag, which is not part of the last hit.
    assert turns["sess-parts"].injected_hits == ("[id=m_20260101000000_cccc0001] The key is under the mat.",)


def test_other_agents_are_skipped() -> None:
    """Only the requested agents' turns are extracted."""
    assert [turn.agent for turn in extract_turns([FIXTURE], {"beta"})] == ["beta"]
    assert extract_turns([FIXTURE], {"gamma"}) == []


def test_turns_round_trip_through_jsonl(tmp_path: Path) -> None:
    """Written turns read back unchanged."""
    turns = extract_turns([FIXTURE], {"alpha", "beta"})
    path = tmp_path / "turns.jsonl"
    write_turns(turns, path)
    assert read_turns(path) == turns
