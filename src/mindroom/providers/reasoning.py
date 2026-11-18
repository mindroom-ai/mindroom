"""Write a model's reasoning level into the ``extra_kwargs`` key its provider route reads for effort."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.providers.names import provider_route

if TYPE_CHECKING:
    from mindroom.config.models import ModelConfig

__all__ = ["apply_reasoning_level"]

_EFFORT_ROUTES = frozenset({"openai", "codex", "azure", "openrouter", "zai", "llama_cpp", "kimi"})
_CLAUDE_ROUTES = frozenset({"anthropic", "vertexai_claude", "bedrock_claude"})


def _level_settings(route: str, level: str, extra_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``extra_kwargs`` with ``level`` in the keys ``route`` reads; only ``off`` is translated."""
    off = level == "off"
    if route in _EFFORT_ROUTES:
        return {**extra_kwargs, "reasoning_effort": "none" if off else level}
    if route == "deepseek":
        settings = {key: value for key, value in extra_kwargs.items() if key != "reasoning_effort"}
        return {**settings, "use_thinking": False} if off else {**settings, "reasoning_effort": level, "use_thinking": True}
    if route in _CLAUDE_ROUTES:
        if off:
            return {**extra_kwargs, "thinking": {"type": "disabled"}}
        return {**extra_kwargs, "output_config": {**(extra_kwargs.get("output_config") or {}), "effort": level}}
    if route == "google":
        # Gemini rejects a request that sets both thinking keys.
        settings = {key: value for key, value in extra_kwargs.items() if key not in {"thinking_level", "thinking_budget"}}
        return {**settings, "thinking_budget": 0} if off else {**settings, "thinking_level": level}
    if route == "ollama":
        return {**extra_kwargs, "think": False if off else level}
    msg = f"provider {route} takes no reasoning level; remove its reasoning block"
    raise ValueError(msg)


def apply_reasoning_level(model_config: ModelConfig) -> ModelConfig:
    """Return the model with its level (``reasoning.default``) in its route's key, which beats the same authored key.

    A model without a ``reasoning`` block is returned unchanged.
    Raises ``ValueError`` for a route that takes no level.
    """
    if model_config.reasoning is None:
        return model_config
    route = provider_route(model_config.provider)
    extra_kwargs = _level_settings(route, model_config.reasoning.default, model_config.extra_kwargs or {})
    return model_config.model_copy(update={"extra_kwargs": extra_kwargs})
