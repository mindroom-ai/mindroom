"""Root pytest configuration.

This file exists at the repository root so pytest imports it before
``tests/conftest.py`` and before any test module imports the MindRoom CLI.
"""

from __future__ import annotations

import os

# Rich resolves colour support once, when a `Console` is constructed, and the CLI builds
# its consoles at import time (`mindroom.cli.config`, `.desktop`, `.service`). Typer does
# the same for its own error consoles. A shell that forces colour (Claude Code, Codex and
# several terminals export `FORCE_COLOR`) therefore bakes ANSI escapes into output that
# `typer.testing.CliRunner` captures from a non-tty buffer, breaking substring assertions
# and YAML parsing of `mindroom config init --print`.
#
# Neutralise the forcing variables and pin a dumb terminal here, before anything imports
# Rich or Typer, so `pytest` behaves identically in every shell. `NO_COLOR` alone is not
# enough: it strips colour but leaves bold and other SGR codes.
for _colour_forcing_var in ("CLICOLOR_FORCE", "COLORTERM", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_colour_forcing_var, None)
os.environ["TERM"] = "dumb"
