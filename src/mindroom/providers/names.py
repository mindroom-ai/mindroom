"""Provider name spelling shared by config validation, model dispatch, pricing, and the wire adapters; imports nothing."""

# ``provider`` values that select the same route as the value they map to.
_PROVIDER_ALIASES = {"gemini": "google", "openai_codex": "codex", "kimi_code": "kimi"}

# Every route the provider registry builds a model for, as ``provider_route`` names it.
PROVIDER_ROUTES = frozenset(
    {
        "anthropic",
        "azure",
        "bedrock_claude",
        "cerebras",
        "codex",
        "deepseek",
        "google",
        "groq",
        "kimi",
        "llama_cpp",
        "ollama",
        "openai",
        "openrouter",
        "synthetic",
        "vertexai_claude",
        "zai",
    },
)


def provider_route(provider: str) -> str:
    """Return the route a configured ``provider`` value selects, which model dispatch and pricing compare.

    It is the value's normalized spelling with aliases resolved, so ``gemini`` selects ``google``.
    """
    canonical = provider.strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(canonical, canonical)
