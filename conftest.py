"""Root pytest configuration.

This file exists at the repository root so pytest imports it before
``tests/conftest.py`` and before any test module imports the MindRoom CLI.
"""

import os

# Rich resolves color support once, when a `Console` is constructed, and the CLI builds
# its consoles at import time (`mindroom.cli.config`, `.desktop`, `.service`), so no
# fixture can influence them. A shell that exports `FORCE_COLOR` (Claude Code, Codex and
# several terminals do) makes Rich force a color terminal even though
# `typer.testing.CliRunner` captures to a non-tty buffer, baking ANSI escapes into output
# that tests assert on and parse as YAML.
#
# Pinning a dumb terminal is the one switch that settles it, because Rich gates all three
# sources of noise on `Console.is_dumb_terminal`: color (`_detect_color_system` returns
# `None`), control codes (`Console.control` writes nothing) and width (`Console.size`
# returns a fixed 80x25 instead of the invoking terminal's size). It wins over
# `FORCE_COLOR` and over Typer's own `force_terminal` consoles. Setting `NO_COLOR`
# instead would not do: it strips color but leaves bold and other SGR codes.
os.environ["TERM"] = "dumb"
