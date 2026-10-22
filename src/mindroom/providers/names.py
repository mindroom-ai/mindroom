"""Provider name spelling shared by model dispatch, pricing, and the wire adapters; imports nothing."""


def canonical_provider(provider: str) -> str:
    """Return the normalized provider key used for model dispatch and identity checks."""
    return provider.strip().lower().replace("-", "_")
