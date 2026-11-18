"""Write a model's reasoning level into the ``extra_kwargs`` key its provider route reads for effort."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mindroom.providers.names import provider_route

if TYPE_CHECKING:
    from mindroom.config.models import ModelConfig

__all__ = ["apply_reasoning_level"]

_EFFORT_ROUTES = frozenset({"openai", "codex", "azure", "openrouter", "llama_cpp", "kimi"})
_CLAUDE_ROUTES = frozenset({"anthropic", "vertexai_claude", "bedrock_claude"})


def _without(settings: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in settings.items() if key not in keys}


def _claude_settings(level: str, extra_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return Claude settings at ``level``, without the authored setting it conflicts with.

    Claude rejects an effort beside disabled thinking, so ``off`` drops the effort and a level drops disabled thinking.
    """
    output_config = extra_kwargs.get("output_config") or {}
    if not isinstance(output_config, Mapping):
        msg = f"extra_kwargs.output_config must be a mapping, not {type(output_config).__name__}"
        raise ValueError(msg)  # noqa: TRY004 - config load reports a ValueError as a validation error
    if level == "off":
        settings = _without(extra_kwargs, "output_config")
        if rest := _without(dict(output_config), "effort"):
            settings["output_config"] = rest
        return {**settings, "thinking": {"type": "disabled"}}
    thinking = extra_kwargs.get("thinking")
    disabled = isinstance(thinking, Mapping) and thinking.get("type") == "disabled"
    settings = _without(extra_kwargs, "thinking") if disabled else extra_kwargs
    return {**settings, "output_config": {**output_config, "effort": level}}


def _zai_settings(level: str, extra_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return Z.ai settings at ``level``: Z.ai takes no ``none`` effort, so ``off`` sends its thinking switch.

    An authored ``extra_body`` wins over the switch, as it wins over every typed setting.
    """
    if level != "off":
        return {**extra_kwargs, "reasoning_effort": level}
    extra_body = {"thinking": {"type": "disabled"}, **(extra_kwargs.get("extra_body") or {})}
    return {**_without(extra_kwargs, "reasoning_effort"), "extra_body": extra_body}


def _level_settings(route: str, level: str, extra_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``extra_kwargs`` with ``level`` in the keys ``route`` reads; only ``off`` is translated."""
    off = level == "off"
    if route in _EFFORT_ROUTES:
        return {**extra_kwargs, "reasoning_effort": "none" if off else level}
    if route == "zai":
        return _zai_settings(level, extra_kwargs)
    if route == "deepseek":
        settings = _without(extra_kwargs, "reasoning_effort")
        return (
            {**settings, "use_thinking": False}
            if off
            else {**settings, "reasoning_effort": level, "use_thinking": True}
        )
    if route in _CLAUDE_ROUTES:
        return _claude_settings(level, extra_kwargs)
    if route == "google":
        # Gemini rejects a request that sets both thinking keys.
        settings = _without(extra_kwargs, "thinking_level", "thinking_budget")
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
