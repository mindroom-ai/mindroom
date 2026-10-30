"""Provider name spelling shared by config validation, model dispatch, pricing, and the wire adapters; imports nothing."""

# ``provider`` values that select the same route as the value they map to.
_PROVIDER_ALIASES = {"gemini": "google", "openai_codex": "codex", "kimi_code": "kimi"}

# The provider name of each route the provider registry builds a model for, as ``provider_route`` names it: runs
# store it as their ``model_provider``, usage rows and prices key by it with the model ID, and errors name it.
ROUTE_PROVIDERS = {
    "anthropic": "Anthropic",
    "azure": "Azure",
    "bedrock_claude": "AwsBedrock",
    "cerebras": "Cerebras",
    "codex": "OpenAI Codex",
    "deepseek": "DeepSeek",
    "google": "Google",
    "groq": "Groq",
    "kimi": "Kimi",
    "llama_cpp": "LlamaCpp",
    "ollama": "Ollama",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "synthetic": "Synthetic",
    "vertexai_claude": "VertexAI",
    "zai": "ZAI",
}
PROVIDER_ROUTES = frozenset(ROUTE_PROVIDERS)


def normalized_provider_name(provider: str) -> str:
    """Return a configured ``provider`` value stripped, lowercased, and with ``-`` spelled ``_``."""
    return provider.strip().lower().replace("-", "_")


def provider_route(provider: str) -> str:
    """Return the route a configured ``provider`` value selects, which model dispatch and pricing compare.

    It is the value's normalized spelling with aliases resolved, so ``gemini`` selects ``google``.
    """
    canonical = normalized_provider_name(provider)
    return _PROVIDER_ALIASES.get(canonical, canonical)
