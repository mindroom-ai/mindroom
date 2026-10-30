"""The identity of a configured model, read from its config alone.

Every provider builder takes its ``ModelIdentity`` from ``model_identity``, and pricing and budgets read the same rule
without building a provider, so learning a model's identity creates no SDK client or cloud session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.provider import ModelIdentity
from mindroom.model_defaults import CODEX_GPT_ALIAS, CODEX_GPT_ENDPOINT
from mindroom.providers.names import ROUTE_PROVIDERS, provider_route

if TYPE_CHECKING:
    from mindroom.config.models import ModelConfig

# The prefixes of model ids as the Kimi Code and Codex CLI configs and LLM plugins write them, which the endpoints
# do not take, and the Codex aliases of the slugs the Codex endpoint accepts.
_KIMI_MODEL_PREFIX = "kimi-code/"
_CODEX_MODEL_PREFIX = "openai-codex/"
_CODEX_MODEL_ALIASES = {CODEX_GPT_ALIAS: CODEX_GPT_ENDPOINT}


def model_identity(model_config: ModelConfig, *, endpoint: str | None = None) -> ModelIdentity:
    """Return the identity of the model ``model_config`` configures, whose requests reach ``endpoint``.

    Its provider is the route's name (``ROUTE_PROVIDERS``) and its id the one the route sends: Kimi and Codex drop a
    CLI-config prefix, and Codex resolves its aliases. A builder passes the endpoint it resolves; pricing needs none.
    """
    route = provider_route(model_config.provider)
    model_id = model_config.id
    if route == "kimi":
        model_id = model_id.strip().removeprefix(_KIMI_MODEL_PREFIX)
    elif route == "codex":
        model_id = model_id.strip().removeprefix(_CODEX_MODEL_PREFIX)
        model_id = _CODEX_MODEL_ALIASES.get(model_id, model_id)
    return ModelIdentity(id=model_id, provider=ROUTE_PROVIDERS[route], endpoint=endpoint)


__all__ = ["model_identity"]
