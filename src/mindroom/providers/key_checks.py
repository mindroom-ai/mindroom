"""The request that proves a provider API key valid, which ``mindroom doctor`` and the dashboard's provider setup send.

Each is one cheap, non-billable authenticated read the provider rejects for a key that is not valid.
"""

from __future__ import annotations

from dataclasses import dataclass

from mindroom.model_defaults import (
    ANTHROPIC_BASE_URL_DEFAULT,
    CEREBRAS_HOST_DEFAULT,
    DEEPSEEK_BASE_URL_DEFAULT,
    GROQ_HOST_DEFAULT,
    OPENAI_BASE_URL_DEFAULT,
    OPENROUTER_BASE_URL_DEFAULT,
)

__all__ = ["PROVIDER_KEY_CHECKS", "ProviderKeyCheck"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderKeyCheck:
    """One provider's key check: where it is sent and how the key travels.

    ``header_name`` carries the key after ``header_prefix``; without one, the key travels as the ``key`` query
    parameter, as Google's Gemini API takes it.
    """

    url: str
    header_name: str | None
    header_prefix: str = ""
    extra_headers: tuple[tuple[str, str], ...] = ()

    def request(self, api_key: str, *, url: str | None = None) -> tuple[str, dict[str, str]]:
        """Return the URL and headers that send ``api_key`` to ``url``, the check's own URL unless given."""
        target = url or self.url
        headers = dict(self.extra_headers)
        if self.header_name is None:
            return f"{target}?key={api_key}", headers
        return target, {self.header_name: f"{self.header_prefix}{api_key}", **headers}


def _bearer(url: str) -> ProviderKeyCheck:
    return ProviderKeyCheck(url=url, header_name="Authorization", header_prefix="Bearer ")


# By route, as ``provider_route`` names it. No ``zai`` entry: Z.ai's paas/v4 API exposes no read a key check could
# use, so a probe would misreport valid keys as broken.
PROVIDER_KEY_CHECKS: dict[str, ProviderKeyCheck] = {
    "anthropic": ProviderKeyCheck(
        url=f"{ANTHROPIC_BASE_URL_DEFAULT}/v1/models",
        header_name="x-api-key",
        extra_headers=(("anthropic-version", "2023-06-01"),),
    ),
    "openai": _bearer(f"{OPENAI_BASE_URL_DEFAULT}/models"),
    "google": ProviderKeyCheck(
        url="https://generativelanguage.googleapis.com/v1beta/models",
        header_name=None,
    ),
    # OpenRouter's key endpoint describes the key it is sent, so only a valid key reads it.
    "openrouter": _bearer(f"{OPENROUTER_BASE_URL_DEFAULT}/key"),
    "deepseek": _bearer(f"{DEEPSEEK_BASE_URL_DEFAULT}/v1/models"),
    "cerebras": _bearer(f"{CEREBRAS_HOST_DEFAULT}/v1/models"),
    "groq": _bearer(f"{GROQ_HOST_DEFAULT}/openai/v1/models"),
}
