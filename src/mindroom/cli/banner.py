"""ASCII art banner with Matrix-themed easter eggs for the MindRoom CLI."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from rich.color import Color
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

_LOGO = """\
┌┬┐┬┌┐┌┌┬┐┬─┐┌─┐┌─┐┌┬┐
│││││││ ││├┬┘│ ││ ││││
┴ ┴┴┘└┘─┴┘┴└─└─┘└─┘┴ ┴"""

_TAGLINES = [
    ("💊 What if I told you... ", "AI agents live in Matrix."),
    ("💊 Free your mind. ", "Your agents live in Matrix."),
    ("💊 There is no spoon. ", "Only agents."),
    ("💊 Follow the white rabbit. ", "Into Matrix."),
    ("💊 The Matrix has you... ", "And your AI agents too."),
    ("💊 Wake up... ", "Your agents are in the Matrix."),
    ("💊 Welcome to the real world. ", "Powered by Matrix."),
    ("💊 Tools. Lots of tools. ", "Over 100 integrations."),
    ("💊 I know kung fu. ", "— Your agents, probably."),
    ("💊 He is the One. ", "Well, one of many agents."),
]


def make_banner(
    tagline: tuple[str, str] | None = None,
) -> Panel:
    """Create the MindRoom banner with a red-pill-to-Matrix-green gradient.

    Args:
        tagline: Optional (green_part, dim_part) override. If None, picks a
                 random Matrix quote (or a special one on March 31).

    """
    lines = _LOGO.splitlines()
    # Build the gradient logo
    logo = Text(justify="center")
    for i, line in enumerate(lines):
        if i > 0:
            logo.append("\n")
        line_text = Text(line)
        width = max(len(line_text) - 1, 1)
        for j in range(len(line_text)):
            t = j / width
            r, g, b = int(255 * (1 - t)), int(255 * t), int(65 * t)
            line_text.stylize(Style(color=Color.from_rgb(r, g, b), bold=True), j, j + 1)
        logo.append(line_text)
    # Build the tagline
    if tagline is not None:
        green_part, dim_part = tagline
    else:
        # Easter egg: special tagline on the Matrix release anniversary
        today = datetime.now(tz=UTC).date()
        if today.month == 3 and today.day == 31:
            green_part = "💊 Happy birthday, Matrix! "
            dim_part = "Released March 31, 1999."
        else:
            green_part, dim_part = random.choice(_TAGLINES)  # noqa: S311
    tag = Text(justify="center")
    tag.append(green_part, style="bold green")
    tag.append(dim_part, style="dim")
    # Combine into panel
    content = Text(justify="center")
    content.append_text(logo)
    content.append("\n")
    content.append_text(tag)
    return Panel(content, border_style="green", expand=False)
