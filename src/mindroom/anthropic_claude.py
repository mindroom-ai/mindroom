"""MindRoom compatibility adapter for the direct Anthropic API."""

from __future__ import annotations

from dataclasses import dataclass

from agno.models.anthropic import Claude

from mindroom.claude_compat import ClaudeProviderCompat


@dataclass
class MindRoomAnthropicClaude(ClaudeProviderCompat, Claude):
    """Anthropic Claude model that preserves safeguard refusal semantics."""
