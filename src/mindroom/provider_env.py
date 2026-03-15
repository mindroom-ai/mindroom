"""Provider credential env-key mappings."""

PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "OLLAMA_HOST",
}
VERTEXAI_CLAUDE_ENV_KEYS: tuple[str, str] = ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")


def env_key_for_provider(provider: str) -> str | None:
    """Get the environment variable name for a provider's API key."""
    if provider == "gemini":
        return PROVIDER_ENV_KEYS.get("google")
    return PROVIDER_ENV_KEYS.get(provider)
