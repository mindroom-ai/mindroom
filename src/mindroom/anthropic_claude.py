"""MindRoom compatibility adapter for the direct Anthropic API."""

from __future__ import annotations

from dataclasses import dataclass

from agno.models.anthropic import Claude

from mindroom.claude_safeguard import ClaudeSafeguardCompat


@dataclass
class MindRoomAnthropicClaude(ClaudeSafeguardCompat, Claude):
    """Anthropic Claude model that preserves safeguard refusal semantics."""
