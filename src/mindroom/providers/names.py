"""Provider name spelling shared by model dispatch, pricing, and the wire adapters; imports nothing."""

# ``provider`` values that select the same route as the value they map to.
_PROVIDER_ALIASES = {"gemini": "google", "openai_codex": "codex", "kimi_code": "kimi"}


def canonical_provider(provider: str) -> str:
    """Return the normalized provider key used for model dispatch and identity checks."""
    return provider.strip().lower().replace("-", "_")


def provider_route(provider: str) -> str:
    """Return the route a configured ``provider`` value selects: its normalized key with aliases resolved."""
    canonical = canonical_provider(provider)
    return _PROVIDER_ALIASES.get(canonical, canonical)
