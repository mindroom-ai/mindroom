"""The one rule that cuts each file inlined into the system prompt."""

from __future__ import annotations

from mindroom.preload_cap import cap_preload_text


def test_text_within_the_limit_is_kept_whole() -> None:
    """Text that fits is kept as it is, unmarked."""
    assert cap_preload_text("one\ntwo", 7) == ("one\ntwo", False)


def test_text_over_the_limit_keeps_its_first_whole_lines_that_fit() -> None:
    """Each kept line counts with its newline: two 3-character lines fill 6 characters, so the third is left out."""
    assert cap_preload_text("aa\nbb\ncc", 6) == ("aa\nbb", True)
    assert cap_preload_text("aa\nbb\ncc", 5) == ("aa", True)


def test_a_first_line_longer_than_the_limit_keeps_its_first_characters() -> None:
    """Only a first line over the limit is cut inside, so the prompt never shows nothing of the file."""
    assert cap_preload_text("x" * 10 + "\nshort", 4) == ("xxxx", True)


def test_appending_past_the_cut_never_changes_the_kept_text() -> None:
    """A new line, or text added to the last line, after the cut leaves the kept text as it was."""
    text = "\n".join(f"line {index}" for index in range(20))
    kept = cap_preload_text(text, 40)

    assert cap_preload_text(text + "\nappended line", 40) == kept
    assert cap_preload_text(text + " appended to the last line", 40) == kept


def test_whitespace_inside_the_kept_text_is_kept() -> None:
    """The kept text is exactly the file's first characters, spaces at its end included."""
    assert cap_preload_text("abc  def", 5) == ("abc  ", True)
    assert cap_preload_text("a  \nbb\ncc", 7) == ("a  \nbb", True)


def test_only_a_line_feed_ends_a_line() -> None:
    """A carriage return counts as a character of its line, as every loader passes the file's text unchanged."""
    assert cap_preload_text("aa\r\nbb\r\ncc", 6) == ("aa\r", True)
