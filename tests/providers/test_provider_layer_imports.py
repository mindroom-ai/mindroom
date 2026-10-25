"""The owned provider layer imports nothing from Agno, and loads each provider SDK when it builds a model of that family."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

OWNED_PROVIDER_MODULES = (
    "mindroom.providers.assistant_turn",
    "mindroom.providers.cache_ladder",
    "mindroom.providers.errors",
    "mindroom.providers.extra_kwargs",
    "mindroom.providers.remote_media",
    "mindroom.providers.pipeline.compose",
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
    "mindroom.providers.ollama.adapter",
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


# Reports whether the family's SDK is loaded before and after the registry builds one model of that family.
_BUILD_PROBE = """
import json, sys, tempfile
from pathlib import Path

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.registry import build_model_provider

provider, sdk = sys.argv[1:]
with tempfile.TemporaryDirectory() as root:
    config_path = Path(root) / "config.yaml"
    config_path.write_text("router:\\n  model: default\\n", encoding="utf-8")
    paths = resolve_runtime_paths(config_path=config_path, storage_path=Path(root) / "data", process_env={})
    loaded_before = sdk in sys.modules
    config = Config(models={"default": ModelConfig(provider=provider, id="test-model", api_key="test-key")})
    build_model_provider(config, paths)
    print(json.dumps([loaded_before, sdk in sys.modules]))
"""


@pytest.mark.parametrize(
    ("provider", "sdk"),
    [("anthropic", "anthropic"), ("google", "google.genai"), ("ollama", "ollama"), ("openai", "openai")],
)
def test_building_a_model_loads_its_sdk_before_its_first_request(provider: str, sdk: str) -> None:
    """A model's first request imports none of its SDK: building the model loads it.

    A reply builds its model off the event loop, so a first-time SDK import inside the request would stall every
    reply the loop serves.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _BUILD_PROBE, provider, sdk],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == [False, True]


# Reports which provider SDKs are loaded after config load checks the extra_kwargs of one model of every family.
_CONFIG_LOAD_PROBE = """
import json, sys, tempfile
from pathlib import Path

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths

with tempfile.TemporaryDirectory() as root:
    config_path = Path(root) / "config.yaml"
    paths = resolve_runtime_paths(config_path=config_path, storage_path=Path(root) / "data", process_env={})
    families = ("openai", "codex", "anthropic", "vertexai_claude", "google", "ollama", "synthetic")
    models = {name: {"provider": name, "id": "m", "extra_kwargs": {"timeout": 5}} for name in families[:-1]}
    Config.validate_with_runtime({"models": {**models, "synthetic": {"provider": "synthetic", "id": "m"}}}, paths)
    print(json.dumps(sorted(set(sys.argv[1:]) & set(sys.modules))))
"""


def test_config_load_checks_every_family_s_extra_kwargs_without_loading_an_sdk() -> None:
    """Config validation checks each model's keys against its route without importing any provider SDK."""
    completed = subprocess.run(
        [sys.executable, "-c", _CONFIG_LOAD_PROBE, *_PROVIDER_SDKS],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []
