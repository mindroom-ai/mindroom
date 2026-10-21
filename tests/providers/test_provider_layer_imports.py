"""The owned provider layer imports nothing from Agno, directly or through the helpers it reuses."""

from __future__ import annotations

import json
import subprocess
import sys

OWNED_PROVIDER_MODULES = (
    "mindroom.providers.assistant_turn",
    "mindroom.providers.cache_ladder",
    "mindroom.providers.errors",
    "mindroom.providers.extra_kwargs",
    "mindroom.providers.remote_media",
    "mindroom.providers.pipeline.compose",
    "mindroom.providers.pipeline.deferred_tools",
    "mindroom.providers.pipeline.guidance_retry",
    "mindroom.providers.pipeline.media",
    "mindroom.providers.pipeline.refusal",
    "mindroom.providers.pipeline.request_log",
    "mindroom.providers.pipeline.retry",
    "mindroom.providers.pipeline.wrapped",
    "mindroom.providers.openai.build",
    "mindroom.providers.openai.chat",
    "mindroom.providers.openai.responses",
    "mindroom.providers.openai.openrouter_cache",
    "mindroom.providers.openai.tool_search",
    "mindroom.providers.anthropic.messages",
    "mindroom.providers.anthropic.prompt_cache",
    "mindroom.providers.gemini.adapter",
    "mindroom.providers.gemini.wire",
    "mindroom.providers.gemini.files",
    "mindroom.providers.ollama_adapter",
    "mindroom.providers.synthetic_adapter",
    "mindroom.providers.registry",
)
# The provider SDKs, which load only when a model of their family is built.
_PROVIDER_SDKS = ("anthropic", "google.genai", "ollama", "openai")

# Imports each module in turn and reports the first one after which an Agno module is loaded.
_PROBE = """
import importlib, json, sys

culprits = {}
for name in json.loads(sys.argv[1]):
    importlib.import_module(name)
    loaded = sorted(module for module in sys.modules if module == "agno" or module.startswith("agno."))
    if loaded:
        culprits[name] = loaded[:5]
        break
print(json.dumps(culprits))
"""


def test_owned_provider_modules_do_not_import_agno() -> None:
    """Every owned adapter, pipeline stage, and shared helper loads without Agno in the process."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(OWNED_PROVIDER_MODULES)],
        check=True,
        capture_output=True,
        text=True,
    )
    culprits = json.loads(completed.stdout.strip().splitlines()[-1])
    assert culprits == {}, f"importing these owned provider modules loaded Agno: {culprits}"


def test_importing_the_registry_loads_no_provider_sdk() -> None:
    """A process that serves only one provider family never pays for the other families' SDKs."""
    probe = f"import json, sys; import mindroom.providers.registry; print(json.dumps(sorted(set({_PROVIDER_SDKS!r}) & set(sys.modules))))"
    completed = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []
