"""The ``extra_kwargs`` a ``claude_code`` model accepts; reading them loads no SDK."""

from __future__ import annotations

from dataclasses import dataclass, field

from mindroom.providers.extra_kwargs import SECRET

__all__ = ["ClaudeCodeSettings"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeCodeSettings:
    """How one ``claude_code`` model runs Claude Code.

    ``cli_path`` is the ``claude`` executable; unset, the one on ``PATH`` runs, else the copy the Claude Agent SDK
    ships. ``effort`` is Claude Code's effort level, such as ``low`` or ``high``.
    """

    # The shared credential flow can give any model a key; this route signs in through Claude Code and ignores it.
    api_key: str | None = field(default=None, metadata=SECRET)
    cli_path: str | None = None
    effort: str | None = None
