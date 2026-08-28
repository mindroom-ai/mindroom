"""Authorization configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from mindroom.config.validation import duplicate_items


class AuthorizationConfig(BaseModel):
    """Authorization configuration with fine-grained permissions."""

    config_command_enabled: bool = Field(
        default=False,
        description="Enable the chat !config command for platform administrators.",
    )
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map canonical Matrix user IDs to bridge aliases. "
            "A message from any alias is treated as if sent by the canonical user. "
            "E.g., {'@alice:example.com': ['@telegram_123:example.com']}"
        ),
    )

    @field_validator("aliases")
    @classmethod
    def validate_unique_aliases(cls, aliases: dict[str, list[str]]) -> dict[str, list[str]]:
        """Ensure each alias is assigned to at most one canonical user."""
        duplicates = duplicate_items([alias for alias_list in aliases.values() for alias in alias_list])
        if duplicates:
            msg = f"Duplicate bridge aliases are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return aliases

    def resolve_alias(self, sender_id: str) -> str:
        """Return the canonical user ID for a bridge alias, or the sender_id itself."""
        for canonical, alias_list in self.aliases.items():
            if sender_id in alias_list:
                return canonical
        return sender_id
