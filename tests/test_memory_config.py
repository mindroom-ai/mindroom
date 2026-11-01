"""Tests for memory configuration."""

from __future__ import annotations

import asyncio
import re
import tempfile
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic.types import Message as AnthropicMessage
from anthropic.types import TextBlock
from anthropic.types import Usage as AnthropicUsage
from chromadb.api.rust import RustBindingsAPI
from mem0 import AsyncMemory
from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.openai import OpenAIEmbedding
from mem0.llms.openai import OpenAILLM
from mem0.utils.factory import LlmFactory
from openai import APIConnectionError
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config, config_validation_issues
from mindroom.config.memory import MemoryConfig, _MemoryEmbedderConfig, _MemoryLLMConfig
from mindroom.config.models import EmbedderConfig, ModelConfig, RouterConfig
from mindroom.config.resolution import memory_search
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.credentials.sync import _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder
from mindroom.embeddings.errors import EmbedderRequestError, openai_embedder_error
from mindroom.embeddings.factory import create_configured_embedder, resolve_embedder_settings
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.memory.config import (
    _get_memory_config,
    _Mem0Embedder,
    _Mem0StrictOpenAIEmbedder,
    _ollama_embedder_error,
    create_memory_instance,
    mem0_collection_name,
    recorded_embedder_failures,
)
from mindroom.model_defaults import (
    ANTHROPIC_BASE_URL_DEFAULT,
    MEMORY_OLLAMA_LLM,
    OLLAMA_HOST_DEFAULT,
    OPENAI_BASE_URL_DEFAULT,
    OPENAI_GPT_LUNA,
    OPENAI_GPT_SOL,
    OPENROUTER_BASE_URL_DEFAULT,
    OPENROUTER_OPENAI_EMBEDDING_SMALL,
    OPENROUTER_OPENAI_LUNA,
)
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.path_globs import matches_root_glob
from mindroom.providers.ollama.adapter import build_provider as build_ollama_provider
from mindroom.tool_system.dependencies import optional_dependency_environment
from tests.conftest import orchestrator_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable


def _fake_mem0_llm() -> SimpleNamespace:
    """Return a stand-in for Mem0's default Ollama LLM, whose client call MindRoom counts."""
    return SimpleNamespace(config=SimpleNamespace(model="gemma4"), client=SimpleNamespace(chat=MagicMock()))


_SAVED_OLLAMA = "http://saved-ollama:11434"
_DOTENV_URL = "http://dotenv.local/v1"
_DOTENV_BASE_URL = f"OPENAI_BASE_URL={_DOTENV_URL}\n"
_CONFIGURED_OLLAMA = "http://config-ollama:11434"
_GATEWAY_URL = "http://claude-gateway.local"
_DOTENV_ANTHROPIC_BASE_URL = f"ANTHROPIC_BASE_URL={_GATEWAY_URL}\n"


def _runtime_paths(tmp_path: Path, process_env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env,
    )


class TestMemoryConfig:
    """Test memory configuration."""

    def test_get_memory_config_with_ollama(
        self,
        tmp_path: Path,
    ) -> None:
        """Test memory config creation with Ollama embedder."""
        # Create config with Ollama embedder
        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(
                model="nomic-embed-text",
                host="http://localhost:11434",
            ),
        )
        llm_config = _MemoryLLMConfig(
            provider="ollama",
            config={
                "model": "llama3.2",
                "host": "http://localhost:11434",
                "temperature": 0.1,
                "top_p": 1,
            },
        )
        memory = MemoryConfig(embedder=embedder_config, llm=llm_config)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = _get_memory_config(storage_path, config, _runtime_paths(tmp_path))

        # Verify embedder config
        assert result["embedder"]["provider"] == "ollama"
        assert result["embedder"]["config"]["model"] == "nomic-embed-text"
        assert result["embedder"]["config"]["ollama_base_url"] == "http://localhost:11434"

        # Verify LLM config
        assert result["llm"]["provider"] == "ollama"
        assert result["llm"]["config"]["model"] == "llama3.2"
        assert result["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

        # Verify vector store config
        assert result["vector_store"]["provider"] == "chroma"
        assert result["vector_store"]["config"]["collection_name"] == mem0_collection_name(config)
        assert str(storage_path / "chroma") in result["vector_store"]["config"]["path"]

    def test_get_memory_config_with_openai(
        self,
        tmp_path: Path,
    ) -> None:
        """Test memory config creation with OpenAI embedder."""
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).save_credentials("openai", {"api_key": "test-key"})

        # Create config with OpenAI embedder
        embedder_config = _MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(model="text-embedding-3-small"),
        )
        llm_config = _MemoryLLMConfig(
            provider="openai",
            config={"model": OPENAI_GPT_LUNA},
        )
        memory = MemoryConfig(embedder=embedder_config, llm=llm_config)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = _get_memory_config(storage_path, config, runtime_paths)

        # Verify embedder config
        assert result["embedder"]["provider"] == "openai"
        assert result["embedder"]["config"]["model"] == "text-embedding-3-small"
        assert result["embedder"]["config"]["api_key"] == "test-key"

        # Verify LLM config
        assert result["llm"]["provider"] == "openai"
        assert result["llm"]["config"]["model"] == OPENAI_GPT_LUNA
        assert result["llm"]["config"]["api_key"] == "test-key"

    @pytest.mark.parametrize(
        ("configured_api_key", "expected_api_key"),
        [(" sk-configured ", "sk-configured"), ("  ", "sk-shared"), (None, "sk-shared")],
        ids=["explicit-trimmed", "blank", "missing"],
    )
    def test_memory_llm_explicit_api_key_beats_shared_provider_key(
        self,
        tmp_path: Path,
        configured_api_key: str | None,
        expected_api_key: str,
    ) -> None:
        """An explicit memory.llm.config.api_key must not be replaced by the provider's shared key."""
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).save_credentials("openai", {"api_key": "sk-shared"})
        llm_settings: dict[str, object] = {"model": OPENAI_GPT_LUNA}
        if configured_api_key is not None:
            llm_settings["api_key"] = configured_api_key
        memory = MemoryConfig(llm=_MemoryLLMConfig(provider="openai", config=llm_settings))
        config = Config(memory=memory, router=RouterConfig(model="default"))

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["llm"]["config"]["api_key"] == expected_api_key

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model_id", "expected_temperature", "expected_top_p", "sends_max_tokens", "expected_reasoning_effort"),
        [
            ("gpt-6-astra", None, None, False, "low"),
            (OPENAI_GPT_LUNA, None, None, False, "low"),
            (OPENAI_GPT_SOL, None, None, False, "low"),
            ("gpt-5.6-luna", None, None, False, "low"),
            ("gpt-5.6-terra", None, None, False, "low"),
            ("gpt-5.4-mini", None, None, False, "low"),
            ("gpt-5-mini", None, None, False, "low"),
            ("gpt-5-2025-08-07", None, None, False, "low"),
            # Keep a legacy non-reasoning model as the sampling-control positive case.
            ("gpt-4", 0.1, 0.8, True, None),
        ],
        ids=[
            "astra",
            "luna",
            "sol",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.4-mini",
            "gpt-5-mini",
            "gpt-5-snapshot",
            "gpt-4",
        ],
    )
    async def test_mem0_openai_request_parameters_are_model_specific(
        self,
        tmp_path: Path,
        model_id: str,
        expected_temperature: float | None,
        expected_top_p: float | None,
        sends_max_tokens: bool,
        expected_reasoning_effort: str | None,
    ) -> None:
        """Memory extraction sends GPT-5 and later models what Mem0 sends the reasoning models it knows."""
        memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="ollama",
                config=EmbedderConfig(model="nomic-embed-text", host="http://localhost:11434"),
            ),
            llm=_MemoryLLMConfig(
                provider="openai",
                config={
                    "api_key": "test-key",
                    "model": model_id,
                    "temperature": 0.1,
                    "top_p": 0.8,
                    "reasoning_effort": "low",
                },
            ),
        )
        config = Config(memory=memory, router=RouterConfig(model="default"))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        create_completion = MagicMock(return_value=response)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_completion)))

        with patch("mem0.llms.openai.OpenAI", return_value=fake_client):
            mem0_llm = OpenAILLM(memory.llm.config)

        with patch.object(
            AsyncMemory,
            "from_config",
            return_value=SimpleNamespace(llm=mem0_llm, embedding_model=MagicMock(), vector_store=MagicMock()),
        ):
            instance = await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert instance.llm is mem0_llm
        instance.llm.generate_response([{"role": "user", "content": "remember this"}])

        request_params = create_completion.call_args.kwargs
        expected_sampling = {"temperature": expected_temperature, "top_p": expected_top_p}
        assert {name: request_params[name] for name in expected_sampling if name in request_params} == {
            name: value for name, value in expected_sampling.items() if value is not None
        }
        assert ("max_tokens" in request_params) is sends_max_tokens
        # A max_completion_tokens cap would also count reasoning tokens and could leave no answer.
        assert "max_completion_tokens" not in request_params
        assert request_params.get("reasoning_effort") == expected_reasoning_effort

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provider", "authored_config", "expected_reasoning_model"),
        [
            ("openrouter", {"model": "openai/gpt-6-luna"}, True),
            ("openrouter", {"model": "anthropic/claude-sonnet-5.5"}, None),
            ("openai", {"model": OPENAI_GPT_LUNA, "is_reasoning_model": False}, False),
        ],
        ids=["openrouter-gpt-6", "openrouter-claude", "authored-false"],
    )
    async def test_mem0_reasoning_model_request_follows_the_routed_model_and_the_authored_setting(
        self,
        tmp_path: Path,
        provider: str,
        authored_config: dict[str, object],
        expected_reasoning_model: bool | None,
    ) -> None:
        """GPT-5 and GPT-6 models get the reasoning-model request through OpenRouter too, unless config sets it."""
        memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="ollama",
                config=EmbedderConfig(model="nomic-embed-text", host="http://localhost:11434"),
            ),
            llm=_MemoryLLMConfig(provider=provider, config={"api_key": "test-key", **authored_config}),
        )
        config = Config(memory=memory, router=RouterConfig(model="default"))
        runtime_paths = _runtime_paths(tmp_path)
        create_completion = MagicMock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]),
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_completion)))
        with patch("mem0.llms.openai.OpenAI", return_value=fake_client):
            mem0_llm = OpenAILLM(_get_memory_config(tmp_path / "memory", config, runtime_paths)["llm"]["config"])

        mem0_memory = SimpleNamespace(llm=mem0_llm, embedding_model=MagicMock(), vector_store=MagicMock())
        with patch.object(AsyncMemory, "from_config", return_value=mem0_memory):
            instance = await create_memory_instance(tmp_path / "memory", config, runtime_paths)
        instance.llm.generate_response([{"role": "user", "content": "remember this"}])

        assert instance.llm.config.is_reasoning_model is expected_reasoning_model
        # Mem0 sends its default temperature to every model that does not get the reasoning-model request.
        assert ("temperature" in create_completion.call_args.kwargs) is (expected_reasoning_model is not True)

    @pytest.mark.parametrize(
        ("authored_config", "expected_sampling"),
        [
            ({"model": "claude-sonnet-5-5"}, {}),
            ({"model": "claude-opus-5-5", "temperature": 0.3}, {}),
            ({"model": "claude-fable-5-1", "top_p": 0.8}, {}),
            ({"model": "claude-sonnet-5-5", "temperature": None}, {}),
            ({"model": "claude-sonnet-5-5", "temperature": 0.3, "top_p": 0.8, "top_k": 5}, {}),
            # Models that accept sampling values keep Mem0's default temperature and the authored values.
            ({"model": "claude-sonnet-4-5"}, {"temperature": 0.1}),
            ({"model": "claude-sonnet-4-5", "temperature": None, "top_p": 0.8}, {"top_p": 0.8}),
            ({}, {"temperature": 0.1}),
        ],
        ids=[
            "sonnet-5.5-default",
            "opus-5.5-temperature",
            "fable-5.1-top-p",
            "sonnet-5.5-null-temperature",
            "sonnet-5.5-all-three",
            "sonnet-4.5-default",
            "sonnet-4.5-top-p",
            "mem0-default-model",
        ],
    )
    def test_mem0_anthropic_sends_sampling_only_to_models_that_accept_it(
        self,
        tmp_path: Path,
        authored_config: dict[str, object],
        expected_sampling: dict[str, float],
    ) -> None:
        """A Claude memory LLM sends no ``temperature``, ``top_p``, or ``top_k`` to the models that reject them."""
        config = Config(
            memory={"llm": {"provider": "anthropic", "config": {"api_key": "sk-ant-test", **authored_config}}},
            router=RouterConfig(model="default"),
        )
        llm_config = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))["llm"]
        mem0_llm = LlmFactory.create(llm_config["provider"], llm_config["config"])
        reply = AnthropicMessage(
            id="msg_test",
            type="message",
            role="assistant",
            model=mem0_llm.config.model,
            content=[TextBlock(type="text", text='{"memory": []}')],
            stop_reason="end_turn",
            usage=AnthropicUsage(input_tokens=1, output_tokens=1),
        )
        create_message = MagicMock(return_value=reply)
        mem0_llm.client.messages.create = create_message

        assert mem0_llm.generate_response([{"role": "user", "content": "remember this"}]) == '{"memory": []}'

        request_params = create_message.call_args.kwargs
        sampling_names = ("temperature", "top_p", "top_k")
        assert {name: request_params[name] for name in sampling_names if name in request_params} == expected_sampling

    def test_mem0_openai_memory_llm_keeps_its_authored_sampling_for_claude_models(self, tmp_path: Path) -> None:
        """Only Mem0's Anthropic LLM omits a sampling value set to None; its OpenAI LLM would send it as null."""
        authored_config = {"model": "anthropic/claude-sonnet-5.5", "temperature": 0.3}
        config = Config(
            memory={"llm": {"provider": "openrouter", "config": authored_config}},
            router=RouterConfig(model="default"),
        )

        llm_config = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))["llm"]["config"]

        assert llm_config["temperature"] == 0.3
        assert "top_p" not in llm_config

    def test_get_memory_config_passes_configured_embedding_dimensions(
        self,
        tmp_path: Path,
    ) -> None:
        """Configured embedding dimensions should be forwarded to Mem0."""
        embedder_config = _MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(
                model="gemini-embedding-2",
                host="http://example.com/v1",
                dimensions=3072,
            ),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        result = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result["embedder"]["config"]["embedding_dims"] == 3072

    def test_get_memory_config_with_sentence_transformers(
        self,
        tmp_path: Path,
    ) -> None:
        """Sentence-transformers should map to Mem0's local huggingface embedder."""
        embedder_config = _MemoryEmbedderConfig(
            provider="sentence_transformers",
            config=EmbedderConfig(
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        result = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result["embedder"]["provider"] == "huggingface"
        assert result["embedder"]["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"

    def test_get_memory_config_keeps_existing_huggingface_provider_support(
        self,
        tmp_path: Path,
    ) -> None:
        """Existing Mem0 providers should remain valid after adding sentence-transformers."""
        config = Config(
            memory={
                "embedder": {
                    "provider": "huggingface",
                    "config": {
                        "model": "sentence-transformers/all-MiniLM-L6-v2",
                    },
                },
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result["embedder"]["provider"] == "huggingface"
        assert result["embedder"]["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"

    def test_memory_collection_name_changes_when_embedder_changes(self) -> None:
        """Different embedder settings should isolate memories into different collections."""
        openai_memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model="text-embedding-3-small"),
            ),
            llm=None,
        )
        local_memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="sentence_transformers",
                config=EmbedderConfig(model="sentence-transformers/all-MiniLM-L6-v2"),
            ),
            llm=None,
        )
        openai_config = Config(memory=openai_memory, router=RouterConfig(model="default"))
        local_config = Config(memory=local_memory, router=RouterConfig(model="default"))

        assert mem0_collection_name(openai_config) != mem0_collection_name(local_config)

    def test_memory_collection_name_changes_when_embedder_prefixes_change(self) -> None:
        """Memories embedded with prefixes are isolated from unprefixed ones."""

        def collection(**prefixes: str) -> str:
            embedder = _MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model="embeddinggemma-2", **prefixes),
            )
            return mem0_collection_name(
                Config(memory=MemoryConfig(embedder=embedder, llm=None), router=RouterConfig(model="default")),
            )

        assert len({collection(), collection(query_prefix="q: "), collection(document_prefix="d: ")}) == 3

    def test_get_memory_config_uses_runtime_shared_credentials_path(self, tmp_path: Path) -> None:
        """Runtime-shared credential overrides should be visible to Mem0 provider config."""
        runtime_paths = resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env={"MINDROOM_SHARED_CREDENTIALS_PATH": str(tmp_path / ".shared_credentials")},
        )
        get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
            "openai",
            {"api_key": "shared-openai-key"},
        )

        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small"},
                },
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"]["api_key"] == "shared-openai-key"

    def test_get_memory_config_prefers_dedicated_embedder_credential(self, tmp_path: Path) -> None:
        """The dedicated embedder credential should beat the shared openai key for Mem0."""
        runtime_paths = _runtime_paths(tmp_path)
        creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
        creds_manager.save_credentials("openai", {"api_key": "shared-openai-key"})
        creds_manager.save_credentials("embedder", {"api_key": "dedicated-embedder-key"})
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small"},
                },
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"]["api_key"] == "dedicated-embedder-key"
        assert "dedicated-embedder-key" not in result["vector_store"]["config"]["collection_name"]

    def test_get_memory_config_explicit_embedder_api_key_wins(self, tmp_path: Path) -> None:
        """An explicit memory.embedder.config.api_key should beat every credential service."""
        runtime_paths = _runtime_paths(tmp_path)
        creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
        creds_manager.save_credentials("openai", {"api_key": "shared-openai-key"})
        creds_manager.save_credentials("embedder", {"api_key": "dedicated-embedder-key"})
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small", "api_key": "explicit-config-key"},
                },
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"]["api_key"] == "explicit-config-key"

    def test_mem0_and_knowledge_embedders_resolve_the_same_key(self, tmp_path: Path) -> None:
        """Both embedder construction paths must authenticate with the same resolved key."""
        runtime_paths = _runtime_paths(tmp_path)
        creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
        creds_manager.save_credentials("openai", {"api_key": "shared-openai-key"})
        creds_manager.save_credentials("embedder", {"api_key": "dedicated-embedder-key"})
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small"},
                },
            },
            router=RouterConfig(model="default"),
        )

        mem0_key = _get_memory_config(tmp_path / "memory", config, runtime_paths)["embedder"]["config"]["api_key"]
        knowledge_embedder = create_configured_embedder(config, runtime_paths)

        assert mem0_key == "dedicated-embedder-key"
        assert knowledge_embedder.api_key == mem0_key

    def test_mem0_and_knowledge_embedders_share_named_credential_binding(self, tmp_path: Path) -> None:
        """Every semantic consumer should use the same explicitly bound credential service."""
        runtime_paths = _runtime_paths(tmp_path)
        creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
        creds_manager.save_credentials("openai", {"api_key": "shared-openai-key"})
        creds_manager.save_credentials("embedder", {"api_key": "legacy-embedder-key"})
        creds_manager.save_credentials("embedding-production", {"api_key": "named-key"})
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": "text-embedding-3-small",
                        "credentials_service": "embedding-production",
                    },
                },
            },
            router=RouterConfig(model="default"),
        )

        mem0_key = _get_memory_config(tmp_path / "memory", config, runtime_paths)["embedder"]["config"]["api_key"]
        knowledge_embedder = create_configured_embedder(config, runtime_paths)

        assert mem0_key == "named-key"
        assert knowledge_embedder.api_key == mem0_key

        resolved_settings = resolve_embedder_settings(config, runtime_paths)
        assert "named-key" not in repr(resolved_settings)

    def test_embedder_prefixes_reach_the_client_and_its_identity(self, tmp_path: Path) -> None:
        """Changing a prefix builds a new client, since it changes every vector the client returns."""
        runtime_paths = _runtime_paths(tmp_path)

        def config_with(prefix: str | None) -> Config:
            return Config(
                memory={
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": "embeddinggemma-2",
                            "host": "http://local/v1",
                            "api_key": "sk-test",
                            "query_prefix": prefix,
                            "document_prefix": "d: ",
                        },
                    },
                },
                router=RouterConfig(model="default"),
            )

        embedder = cast("OpenAIEmbedder", create_configured_embedder(config_with("q: "), runtime_paths))

        assert (embedder.query_prefix, embedder.document_prefix) == ("q: ", "d: ")
        assert (
            resolve_embedder_settings(config_with("q: "), runtime_paths).client_signature
            != resolve_embedder_settings(config_with("other: "), runtime_paths).client_signature
        )

    @pytest.mark.parametrize(
        ("provider", "fields", "message"),
        [
            (
                "ollama",
                {"api_key": "sk-test"},
                "memory.embedder.config.api_key does not apply to the ollama provider; remove it",
            ),
            (
                "sentence_transformers",
                {"credentials_service": "embedder"},
                "memory.embedder.config.credentials_service does not apply to the sentence_transformers provider;"
                " remove it",
            ),
            (
                "sentence_transformers",
                {"query_prefix": "q: "},
                "memory.embedder.config.query_prefix does not apply to the sentence_transformers provider; remove it",
            ),
            (
                "huggingface",
                {"api_key": "sk-test", "credentials_service": "embedder", "document_prefix": "d: "},
                "memory.embedder.config.credentials_service, memory.embedder.config.api_key and"
                " memory.embedder.config.document_prefix do not apply to the huggingface provider; remove them",
            ),
            (
                "sentence_transformers",
                {"host": "http://local:8080"},
                "memory.embedder.config.host does not apply to the sentence_transformers provider; remove it",
            ),
            (
                "huggingface",
                {"host": "http://tei.local"},
                "memory.embedder.config.host does not apply to the huggingface provider; remove it",
            ),
            (
                "Ollama",
                {"api_key": "sk-test"},
                "memory.embedder.config.api_key does not apply to the ollama provider; remove it",
            ),
            (
                "ollama",
                {"dimensions": 768},
                "memory.embedder.config.dimensions does not apply to the ollama provider; remove it",
            ),
            (
                "sentence-transformers",
                {"dimensions": 256},
                "memory.embedder.config.dimensions does not apply to the sentence_transformers provider; remove it",
            ),
        ],
    )
    def test_embedder_rejects_the_fields_its_provider_ignores(
        self,
        provider: str,
        fields: dict[str, object],
        message: str,
    ) -> None:
        """A field the provider would silently ignore is a config error that names the field."""
        with pytest.raises(ValidationError, match=re.escape(message)):
            _MemoryEmbedderConfig.model_validate({"provider": provider, "config": {"model": "embed", **fields}})

    @pytest.mark.parametrize("blank", [None, "", "   "])
    @pytest.mark.parametrize("provider", ["ollama", "sentence_transformers", "huggingface"])
    def test_embedder_fields_left_unset_load_with_any_provider(self, provider: str, blank: str | None) -> None:
        """A null, empty, or whitespace-only field sets nothing, so a provider that ignores it still loads."""
        unset = {"api_key": blank, "credentials_service": None, "query_prefix": blank, "document_prefix": blank}

        embedder = _MemoryEmbedderConfig.model_validate({"provider": provider, "config": {"model": "embed", **unset}})

        assert embedder.provider == provider

    @pytest.mark.parametrize(
        ("provider", "fields"),
        [
            (
                "openai",
                {
                    "credentials_service": "embedder",
                    "api_key": "sk-test",
                    "host": "http://local/v1",
                    "dimensions": 768,
                    "query_prefix": "q: ",
                    "document_prefix": "d: ",
                },
            ),
            (" OpenAI ", {"api_key": "sk-test", "query_prefix": "q: "}),
            ("ollama", {"host": "http://ollama.local:11434"}),
            # A Mem0-only provider's collection name includes dimensions, so it stays accepted though unread.
            ("huggingface", {"dimensions": 384}),
            ("Sentence-Transformers", {}),
        ],
    )
    def test_embedder_accepts_the_fields_its_provider_may_set(self, provider: str, fields: dict[str, object]) -> None:
        """The provider name is spelled as model providers are, and each accepted field loads as configured."""
        embedder = _MemoryEmbedderConfig.model_validate({"provider": provider, "config": {"model": "embed", **fields}})

        assert embedder.provider == provider.strip().lower().replace("-", "_")
        assert embedder.config.model_dump(exclude_none=True) == {"model": "embed", **fields}

    def test_keyless_local_endpoint_constructs_both_real_clients(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no key anywhere, both real client paths construct via the placeholder (keyless local mode)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        runtime_paths = _runtime_paths(tmp_path)
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "embeddinggemma:300m", "host": "http://localhost:9292/v1"},
                },
            },
            router=RouterConfig(model="default"),
        )

        knowledge_embedder = create_configured_embedder(config, runtime_paths)
        assert knowledge_embedder.api_key == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY
        assert knowledge_embedder._sdk_client().api_key == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY

        mem0_embedder_config = _get_memory_config(tmp_path / "memory", config, runtime_paths)["embedder"]["config"]
        assert mem0_embedder_config["api_key"] == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY
        mem0_embedding = OpenAIEmbedding(BaseEmbedderConfig(**mem0_embedder_config))
        assert mem0_embedding.client.api_key == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY

    def test_get_memory_config_openai_embedder_maps_provider_settings(self, tmp_path: Path) -> None:
        """OpenAI Mem0 embedder config should keep the provider-specific field names."""
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
            "openai",
            {"api_key": "shared-openai-key"},
        )
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": "custom-embedding-model",
                        "host": "http://embeddings.local/v1",
                        "dimensions": 1024,
                    },
                },
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["provider"] == "openai"
        assert result["embedder"]["config"] == {
            "model": "custom-embedding-model",
            "api_key": "shared-openai-key",
            "openai_base_url": "http://embeddings.local/v1",
            "embedding_dims": 1024,
        }

    @pytest.mark.parametrize("blank_host", ["", "   "])
    def test_blank_openai_embedder_host_uses_client_default_url(self, tmp_path: Path, blank_host: str) -> None:
        """A cleared dashboard host must not become an empty base URL."""
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small", "host": blank_host, "api_key": "key"},
                },
            },
            router=RouterConfig(model="default"),
        )
        runtime_paths = _runtime_paths(tmp_path)

        assert config.memory.embedder.config.host is None
        embedder = create_configured_embedder(config, runtime_paths)
        assert str(embedder._sdk_client().base_url) == "https://api.openai.com/v1/"
        mem0_embedder_config = _get_memory_config(tmp_path / "memory", config, runtime_paths)["embedder"]["config"]
        assert mem0_embedder_config["openai_base_url"] == OPENAI_BASE_URL_DEFAULT

    @pytest.mark.parametrize(
        ("exported", "env_file", "embedder_host", "embedder_url", "llm_url"),
        [
            pytest.param({}, _DOTENV_BASE_URL, None, _DOTENV_URL, _DOTENV_URL, id="env-file"),
            pytest.param(
                {"OPENAI_BASE_URL": ""},
                _DOTENV_BASE_URL,
                None,
                _DOTENV_URL,
                _DOTENV_URL,
                id="env-file-under-blank-export",
            ),
            pytest.param(
                {"OPENAI_BASE_URL_FILE": "endpoint"},
                "",
                None,
                "http://file.local/v1",
                "http://file.local/v1",
                id="file-variable",
            ),
            pytest.param(
                {"OPENAI_BASE_URL": ""},
                "",
                None,
                OPENAI_BASE_URL_DEFAULT,
                OPENAI_BASE_URL_DEFAULT,
                id="blank-export",
            ),
            pytest.param(
                {},
                _DOTENV_BASE_URL,
                "http://embedder.local/v1",
                "http://embedder.local/v1",
                _DOTENV_URL,
                id="embedder-host-wins",
            ),
        ],
    )
    def test_openai_memory_clients_without_an_endpoint_reach_openai_base_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exported: dict[str, str],
        env_file: str,
        embedder_host: str | None,
        embedder_url: str,
        llm_url: str,
    ) -> None:
        """The embedder and the memory LLM read `OPENAI_BASE_URL` as chat models do, unless the embedder sets `host`."""
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        for name, value in exported.items():
            monkeypatch.setenv(name, value)
        (tmp_path / ".env").write_text(env_file, encoding="utf-8")
        (tmp_path / "endpoint").write_text("http://file.local/v1\n", encoding="utf-8")
        runtime_paths = _runtime_paths(tmp_path, exported)
        embedder = {"model": "text-embedding-3-small", "api_key": "key", "host": embedder_host}
        config = Config(
            memory={
                "embedder": {"provider": "openai", "config": embedder},
                "llm": {"provider": "openai", "config": {"model": OPENAI_GPT_LUNA, "api_key": "key"}},
            },
            router=RouterConfig(model="default"),
        )

        client = create_configured_embedder(config, runtime_paths)
        assert str(client._sdk_client().base_url) == f"{embedder_url}/"
        mem0_config = _get_memory_config(tmp_path / "memory", config, runtime_paths)
        assert mem0_config["embedder"]["config"]["openai_base_url"] == embedder_url
        assert mem0_config["llm"]["config"]["openai_base_url"] == llm_url
        assert str(OpenAILLM(mem0_config["llm"]["config"]).client.base_url) == f"{llm_url}/"

    @pytest.mark.parametrize(
        ("configured_url", "expected_url"),
        [
            pytest.param("http://memory-llm.local/v1", "http://memory-llm.local/v1", id="configured"),
            pytest.param("", "http://runtime.local/v1", id="blank"),
            pytest.param(None, "http://runtime.local/v1", id="null"),
        ],
    )
    def test_an_openai_memory_llm_endpoint_of_its_own_wins_over_openai_base_url(
        self,
        tmp_path: Path,
        configured_url: str | None,
        expected_url: str,
    ) -> None:
        """A memory LLM's own `openai_base_url` wins, and a blank or null one falls back to `OPENAI_BASE_URL`."""
        (tmp_path / ".env").write_text("OPENAI_BASE_URL=http://runtime.local/v1\n", encoding="utf-8")
        runtime_paths = _runtime_paths(tmp_path, {})
        llm = {"model": OPENAI_GPT_LUNA, "api_key": "key", "openai_base_url": configured_url}
        config = Config(memory={"llm": {"provider": "openai", "config": llm}}, router=RouterConfig(model="default"))

        llm_config = _get_memory_config(tmp_path / "memory", config, runtime_paths)["llm"]["config"]

        assert llm_config["openai_base_url"] == expected_url

    @pytest.mark.parametrize(
        ("exported", "env_file", "configured_url", "expected_url"),
        [
            pytest.param({}, _DOTENV_ANTHROPIC_BASE_URL, None, _GATEWAY_URL, id="env-file"),
            pytest.param(
                {"ANTHROPIC_BASE_URL": ""},
                _DOTENV_ANTHROPIC_BASE_URL,
                None,
                _GATEWAY_URL,
                id="env-file-under-blank-export",
            ),
            pytest.param({"ANTHROPIC_BASE_URL_FILE": "endpoint"}, "", None, _GATEWAY_URL, id="file-variable"),
            pytest.param({"ANTHROPIC_BASE_URL": ""}, "", None, ANTHROPIC_BASE_URL_DEFAULT, id="blank-export"),
            pytest.param({}, _DOTENV_ANTHROPIC_BASE_URL, "", _GATEWAY_URL, id="blank-configured"),
            pytest.param(
                {},
                _DOTENV_ANTHROPIC_BASE_URL,
                "http://memory-llm.local",
                "http://memory-llm.local",
                id="configured-wins",
            ),
        ],
    )
    def test_an_anthropic_memory_llm_reaches_anthropic_base_url_unless_it_sets_its_own(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exported: dict[str, str],
        env_file: str,
        configured_url: str | None,
        expected_url: str,
    ) -> None:
        """The Anthropic memory LLM reads `ANTHROPIC_BASE_URL` as Claude chat models do, unless it sets its own."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        for name, value in exported.items():
            monkeypatch.setenv(name, value)
        (tmp_path / ".env").write_text(env_file, encoding="utf-8")
        (tmp_path / "endpoint").write_text(f"{_GATEWAY_URL}\n", encoding="utf-8")
        runtime_paths = _runtime_paths(tmp_path, exported)
        llm = {"model": "claude-haiku-5-5", "api_key": "key", "anthropic_base_url": configured_url}
        config = Config(memory={"llm": {"provider": "anthropic", "config": llm}}, router=RouterConfig(model="default"))

        llm_config = _get_memory_config(tmp_path / "memory", config, runtime_paths)["llm"]

        assert llm_config["config"]["anthropic_base_url"] == expected_url
        mem0_llm = LlmFactory.create(llm_config["provider"], llm_config["config"])
        assert str(mem0_llm.client.base_url) == expected_url

    def test_openrouter_only_memory_uses_openrouter_for_llm_and_embedder(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Memory must work with only an OpenRouter key, even one saved under its env var name."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
            "OPENROUTER_API_KEY",
            {"api_key": "openrouter-key"},
        )
        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": OPENROUTER_OPENAI_EMBEDDING_SMALL,
                        "host": OPENROUTER_BASE_URL_DEFAULT,
                        "credentials_service": "openrouter",
                        "dimensions": 1536,
                    },
                },
                "llm": {"provider": "openrouter", "config": {"model": OPENROUTER_OPENAI_LUNA, "temperature": 0.1}},
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["llm"] == {
            "provider": "openai",
            "config": {
                "model": OPENROUTER_OPENAI_LUNA,
                "temperature": 0.1,
                "api_key": "openrouter-key",
                "openai_base_url": OPENROUTER_BASE_URL_DEFAULT,
                "openrouter_base_url": OPENROUTER_BASE_URL_DEFAULT,
            },
        }
        mem0_llm = LlmFactory.create(result["llm"]["provider"], result["llm"]["config"])
        assert str(mem0_llm.client.base_url) == f"{OPENROUTER_BASE_URL_DEFAULT}/"
        assert mem0_llm.client.api_key == "openrouter-key"

        assert result["embedder"]["config"] == {
            "model": OPENROUTER_OPENAI_EMBEDDING_SMALL,
            "api_key": "openrouter-key",
            "openai_base_url": OPENROUTER_BASE_URL_DEFAULT,
            "embedding_dims": 1536,
        }
        embedder = create_configured_embedder(config, runtime_paths)
        assert embedder.api_key == "openrouter-key"
        assert str(embedder._sdk_client().base_url) == f"{OPENROUTER_BASE_URL_DEFAULT}/"
        assert embedder._request_parameters(["hi"], purpose="query")["dimensions"] == 1536

    @pytest.mark.parametrize(
        ("provider", "canonical", "mem0_provider"),
        [
            (" OpenRouter ", "openrouter", "openai"),
            ("Ollama", "ollama", "ollama"),
            ("ANTHROPIC", "anthropic", "anthropic"),
        ],
    )
    def test_memory_llm_provider_loads_in_any_spelling_as_the_name_mem0_accepts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
        canonical: str,
        mem0_provider: str,
    ) -> None:
        """A memory LLM provider is spelled as model providers are, so Mem0 builds it instead of rejecting the name."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        config = Config(
            memory={"llm": {"provider": provider, "config": {"model": "m", "api_key": "sk-test"}}},
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert config.memory.llm is not None
        assert config.memory.llm.provider == canonical
        assert result["llm"]["provider"] == mem0_provider
        LlmFactory.create(result["llm"]["provider"], result["llm"]["config"])

    @pytest.mark.parametrize(
        ("saved_host", "configured_host", "memory_host", "model_host"),
        [
            (_SAVED_OLLAMA, _CONFIGURED_OLLAMA, _SAVED_OLLAMA, _CONFIGURED_OLLAMA),
            (None, _CONFIGURED_OLLAMA, _CONFIGURED_OLLAMA, _CONFIGURED_OLLAMA),
            (_SAVED_OLLAMA, None, _SAVED_OLLAMA, _SAVED_OLLAMA),
            (None, None, OLLAMA_HOST_DEFAULT, OLLAMA_HOST_DEFAULT),
        ],
    )
    def test_ollama_memory_clients_put_the_saved_host_first_and_models_their_own(
        self,
        tmp_path: Path,
        saved_host: str | None,
        configured_host: str | None,
        memory_host: str,
        model_host: str,
    ) -> None:
        """The memory LLM and the Ollama embedder reach the saved Ollama host before their ``host``; a model its own."""
        runtime_paths = _runtime_paths(tmp_path)
        if saved_host is not None:
            get_runtime_shared_credentials_manager(runtime_paths).save_credentials("ollama", {"host": saved_host})
        config = Config(
            memory={
                "embedder": {"provider": "ollama", "config": {"model": "nomic-embed-text", "host": configured_host}},
                "llm": {"provider": "ollama", "config": {"model": "gemma4", "host": configured_host}},
            },
            router=RouterConfig(model="default"),
        )
        model = ModelConfig(provider="ollama", id="gemma4", host=configured_host)

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"] == {"model": "nomic-embed-text", "ollama_base_url": memory_host}
        assert result["llm"]["config"] == {"model": "gemma4", "ollama_base_url": memory_host}
        assert build_ollama_provider(model, runtime_paths).identity.endpoint == model_host

    def test_an_ollama_memory_llm_without_a_host_reaches_the_saved_one(self, tmp_path: Path) -> None:
        """Without ``host``, an Ollama memory LLM reaches the saved Ollama host; Mem0's own ``ollama_base_url`` goes as written."""
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).save_credentials("ollama", {"host": _SAVED_OLLAMA})

        def mem0_llm_config(llm_config: dict[str, str]) -> dict[str, object]:
            config = Config(
                memory={"llm": {"provider": "ollama", "config": {"model": "gemma4", **llm_config}}},
                router=RouterConfig(model="default"),
            )
            return _get_memory_config(tmp_path / "memory", config, runtime_paths)["llm"]["config"]

        assert mem0_llm_config({}) == {"model": "gemma4", "ollama_base_url": _SAVED_OLLAMA}
        mem0_host = {"ollama_base_url": "http://mem0-ollama:11434"}
        assert mem0_llm_config(mem0_host) == {"model": "gemma4", "ollama_base_url": "http://mem0-ollama:11434"}
        both = {"host": _CONFIGURED_OLLAMA, **mem0_host}
        assert mem0_llm_config(both) == {"model": "gemma4", "ollama_base_url": "http://mem0-ollama:11434"}

    @pytest.mark.parametrize(
        ("model", "effective_dimensions"),
        [
            ("text-embedding-3-small", 1536),
            ("text-embedding-3-large", 3072),
        ],
    )
    def test_memory_collection_name_ignores_equivalent_mem0_openai_default_dimensions(
        self,
        model: str,
        effective_dimensions: int,
    ) -> None:
        """Equivalent Mem0 OpenAI defaults should reuse the same memory collection."""
        implicit_default = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model=model),
            ),
            llm=None,
        )
        explicit_default = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model=model, dimensions=effective_dimensions),
            ),
            llm=None,
        )
        implicit_config = Config(memory=implicit_default, router=RouterConfig(model="default"))
        explicit_config = Config(memory=explicit_default, router=RouterConfig(model="default"))

        assert mem0_collection_name(implicit_config) == mem0_collection_name(explicit_config)

    def test_custom_openai_compatible_memory_collection_name_tracks_explicit_dimensions(self) -> None:
        """Custom OpenAI-compatible embedders must not treat omitted dimensions as explicit 1536."""
        implicit_dimensions = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(
                    model="gemini-embedding-2",
                    host="http://example.com/v1",
                ),
            ),
            llm=None,
        )
        explicit_dimensions = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(
                    model="gemini-embedding-2",
                    host="http://example.com/v1",
                    dimensions=1536,
                ),
            ),
            llm=None,
        )
        implicit_config = Config(memory=implicit_dimensions, router=RouterConfig(model="default"))
        explicit_config = Config(memory=explicit_dimensions, router=RouterConfig(model="default"))

        assert mem0_collection_name(implicit_config) != mem0_collection_name(explicit_config)

    def test_get_memory_config_no_model_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """Test memory config falls back to Ollama when no model configured."""
        # Create config with no models
        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="nomic-embed-text", host=None),
        )
        # No memory.llm configured - should trigger fallback
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = _get_memory_config(storage_path, config, _runtime_paths(tmp_path))

        # Verify LLM fallback config
        assert result["llm"]["provider"] == "ollama"
        assert result["llm"]["config"]["model"] == MEMORY_OLLAMA_LLM
        assert result["llm"]["config"]["ollama_base_url"] == OLLAMA_HOST_DEFAULT

    def test_chroma_directory_creation(
        self,
        tmp_path: Path,
    ) -> None:
        """Test that ChromaDB directory is created."""
        # Create minimal config
        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="test", host=None),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Get config
        result = _get_memory_config(tmp_path, config, _runtime_paths(tmp_path))

        # Verify chroma path in config
        chroma_path = tmp_path / "chroma"
        assert str(chroma_path) == result["vector_store"]["config"]["path"]

        # Verify directory was created
        assert chroma_path.exists()
        assert chroma_path.is_dir()

    def test_relative_storage_path_remains_stable_after_cwd_change(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relative storage paths should be anchored once and survive later cwd changes."""
        project_root = tmp_path / "project"
        project_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(project_root)

        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="test", host=None),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(Path("mindroom_data")),
            config=config,
        )

        other_cwd = tmp_path / "other"
        other_cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(other_cwd)

        result = _get_memory_config(orchestrator.runtime_paths.storage_root, config, orchestrator.runtime_paths)

        expected_storage = (project_root / "mindroom_data").resolve()
        expected_chroma = (expected_storage / "chroma").resolve()
        assert orchestrator.runtime_paths.storage_root == expected_storage
        assert Path(result["vector_store"]["config"]["path"]) == expected_chroma

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["sentence_transformers", "huggingface"])
    @patch("mindroom.memory.config.ensure_sentence_transformers_dependencies")
    @patch.object(AsyncMemory, "from_config")
    async def test_create_memory_instance_auto_installs_sentence_transformers(
        self,
        mock_from_config: MagicMock,
        mock_ensure_sentence_transformers_dependencies: MagicMock,
        provider: str,
        tmp_path: Path,
    ) -> None:
        """Both local embedders load Mem0's huggingface embedder, which imports sentence-transformers, so both install it."""
        memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider=provider,
                config=EmbedderConfig(model="sentence-transformers/all-MiniLM-L6-v2"),
            ),
            llm=None,
        )
        config = Config(memory=memory, router=RouterConfig(model="default"))
        expected_memory = SimpleNamespace(llm=_fake_mem0_llm(), embedding_model=MagicMock(), vector_store=MagicMock())
        mock_from_config.return_value = expected_memory

        result = await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result is expected_memory
        mock_ensure_sentence_transformers_dependencies.assert_called_once_with(_runtime_paths(tmp_path))
        mock_from_config.assert_called_once()

    @pytest.mark.asyncio
    @patch("mindroom.tool_system.dependencies.check_deps_installed", return_value=True)
    @patch.object(AsyncMemory, "from_config")
    async def test_create_memory_instance_waits_for_installs_off_event_loop(
        self,
        mock_from_config: MagicMock,
        mock_check_deps_installed: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mem0 checks dependencies and waits for a running install without stalling the event loop."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="sentence_transformers",
                config=EmbedderConfig(model="sentence-transformers/all-MiniLM-L6-v2"),
            ),
            llm=None,
        )
        config = Config(memory=memory, router=RouterConfig(model="default"))
        mock_from_config.return_value = SimpleNamespace(
            llm=_fake_mem0_llm(),
            embedding_model=MagicMock(),
            vector_store=MagicMock(),
        )
        install_running = threading.Event()
        finish_install = threading.Event()

        def run_install() -> None:
            with optional_dependency_environment(exclusive=True):
                install_running.set()
                finish_install.wait(5)

        installer = threading.Thread(target=run_install)
        installer.start()
        try:
            assert install_running.wait(5)
            creation = asyncio.create_task(
                create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path)),
            )
            started = time.monotonic()
            await asyncio.sleep(0.2)
            # A blocked event loop would only finish this sleep after the install's 5 s timeout.
            assert time.monotonic() - started < 2
            assert not creation.done()
        finally:
            finish_install.set()
            installer.join(5)
        assert await creation is mock_from_config.return_value
        assert [call.args[0] for call in mock_check_deps_installed.call_args_list] == [
            ["mem0ai"],
            ["chromadb"],
            ["sentence-transformers", "torchvision"],
        ]

    @pytest.mark.asyncio
    async def test_create_memory_instance_replaces_mem0_openai_embedder_with_strict_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        """Malformed OpenAI successes cross Mem0 as classified strict failures."""
        config = Config(
            memory=MemoryConfig(
                embedder=_MemoryEmbedderConfig(
                    provider="openai",
                    config=EmbedderConfig(model="text-embedding-3-small", api_key="sk-test"),
                ),
                llm=None,
            ),
            router=RouterConfig(model="default"),
        )
        client = MagicMock()
        client.embeddings.create.return_value = SimpleNamespace(data=[], usage=None)
        strict_embedder = OpenAIEmbedder(
            model="text-embedding-3-small",
            api_key="sk-test",
            client=client,
            health_recorder=capture_embedder_health_recorder(),
        )
        memory = SimpleNamespace(llm=_fake_mem0_llm(), embedding_model=object(), vector_store=MagicMock())

        with (
            patch.object(AsyncMemory, "from_config", return_value=memory),
            patch("mindroom.embeddings.factory.create_configured_embedder", return_value=strict_embedder),
        ):
            result = await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result.embedding_model.embedder == _Mem0StrictOpenAIEmbedder(strict_embedder)
        with pytest.raises(EmbedderRequestError, match="embedder returned 0 embeddings for 1 inputs"):
            result.embedding_model.embed("query", "search")

    @pytest.mark.asyncio
    async def test_an_ollama_outage_reaches_mem0_callers_as_an_unreachable_embedder(self, tmp_path: Path) -> None:
        """Mem0's own Ollama embedder fails as the typed embedder error, so a memory search degrades instead of failing.

        The embedder asks Ollama for its model while the memory is built, so an outage fails the build as well.
        """
        config = Config(
            memory=MemoryConfig(
                embedder=_MemoryEmbedderConfig(provider="ollama", config=EmbedderConfig(model="nomic-embed-text")),
                llm=None,
            ),
            router=RouterConfig(model="default"),
        )
        refused = ConnectionError("Failed to connect to Ollama. Please check that Ollama is downloaded and running.")
        mem0_embedder = MagicMock()
        mem0_embedder.embed.side_effect = refused
        mem0_embedder.embed_batch.return_value = [[0.5]]
        memory = SimpleNamespace(llm=_fake_mem0_llm(), embedding_model=mem0_embedder, vector_store=MagicMock())

        with (
            patch.object(AsyncMemory, "from_config", side_effect=refused),
            pytest.raises(EmbedderRequestError) as build,
        ):
            await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))
        with patch.object(AsyncMemory, "from_config", return_value=memory):
            result = await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))
        with pytest.raises(EmbedderRequestError) as search:
            result.embedding_model.embed("query", "search")

        for raised in (build.value, search.value):
            assert (str(raised), raised.retryable) == ("embedder endpoint unreachable", True)
        mem0_embedder.embed.assert_called_once_with("query", "search")
        assert result.embedding_model.embed_batch(["memory"], "update") == [[0.5]]
        mem0_embedder.embed_batch.assert_called_once_with(["memory"], "update")
        mem0_embedder.embed_batch.side_effect = refused
        with pytest.raises(EmbedderRequestError, match="embedder endpoint unreachable"):
            result.embedding_model.embed_batch(["memory"])

    @pytest.mark.asyncio
    async def test_an_outage_of_another_mem0_embedder_reaches_mem0_callers_as_an_unreachable_embedder(
        self,
        tmp_path: Path,
    ) -> None:
        """An embedder only Mem0 provides, such as Azure OpenAI over the OpenAI SDK, fails as the typed embedder error."""
        config = Config(
            memory=MemoryConfig(
                embedder=_MemoryEmbedderConfig(
                    provider="azure_openai",
                    config=EmbedderConfig(model="text-embedding-3-small"),
                ),
                llm=None,
            ),
            router=RouterConfig(model="default"),
        )
        request = httpx.Request("POST", "https://r.openai.azure.com/openai/deployments/e/embeddings")
        refused = APIConnectionError(request=request)
        refused.__cause__ = httpx.ConnectError("connection refused", request=request)
        mem0_embedder = MagicMock()
        mem0_embedder.embed.side_effect = refused
        memory = SimpleNamespace(llm=_fake_mem0_llm(), embedding_model=mem0_embedder, vector_store=MagicMock())

        with patch.object(AsyncMemory, "from_config", return_value=memory):
            result = await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))
        with pytest.raises(EmbedderRequestError) as search:
            result.embedding_model.embed("query", "search")

        assert (str(search.value), search.value.retryable) == ("embedder endpoint unreachable", True)

    @pytest.mark.asyncio
    async def test_a_failure_unrelated_to_ollama_while_building_mem0_keeps_its_type(self, tmp_path: Path) -> None:
        """Only an Ollama failure reads as the embedder's; any other build failure propagates as raised."""
        config = Config(
            memory=MemoryConfig(
                embedder=_MemoryEmbedderConfig(provider="ollama", config=EmbedderConfig(model="nomic-embed-text")),
                llm=None,
            ),
            router=RouterConfig(model="default"),
        )

        with (
            patch.object(AsyncMemory, "from_config", side_effect=ValueError("bad vector store")),
            pytest.raises(ValueError, match="bad vector store"),
        ):
            await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))

    @pytest.mark.asyncio
    async def test_a_memory_reads_and_writes_a_store_only_between_another_memorys_writes(self, tmp_path: Path) -> None:
        """Chroma can fail a query that overlaps a new collection's first write, so one store's memories take turns.

        Chroma's error reads ``Error creating hnsw segment reader: Nothing found on disk``; it hit the first memory
        search of a conversation whose previous turn was still saving into a fresh store.
        """
        config = Config(
            memory=MemoryConfig(
                embedder=_MemoryEmbedderConfig(
                    provider="openai",
                    config=EmbedderConfig(model="text-embedding-3-small", api_key="sk-test"),
                ),
                llm=_MemoryLLMConfig(provider="openai", config={"model": OPENAI_GPT_LUNA, "api_key": "sk-test"}),
            ),
            router=RouterConfig(model="default"),
        )
        runtime_paths = _runtime_paths(tmp_path)
        saver = await create_memory_instance(tmp_path / "memory", config, runtime_paths)
        other = await create_memory_instance(tmp_path / "memory", config, runtime_paths)
        vector = [1.0, 0.0, 0.0, 0.0]
        scope = {"user_id": "agent_general"}
        payload = {**scope, "data": "PR review"}
        other_calls = {
            "search": partial(other.vector_store.search, query="PR review", vectors=vector, top_k=5, filters=scope),
            "get": partial(other.vector_store.get, vector_id="memory-1"),
            "list": partial(other.vector_store.list, filters=scope),
            "update": partial(other.vector_store.update, vector_id="memory-1", payload=payload),
            "delete": partial(other.vector_store.delete, vector_id="memory-2"),
        }
        adding = threading.Event()
        all_called = threading.Event()
        called_while_adding: list[bool] = []
        add_records = RustBindingsAPI._add

        def slow_add(api: RustBindingsAPI, *args: object, **kwargs: object) -> object:
            adding.set()
            # The other memory's calls reach Chroma now unless they wait for the write.
            all_called.wait(0.5)
            try:
                return add_records(api, *args, **kwargs)
            finally:
                adding.clear()

        def watched(call: Callable[..., object]) -> Callable[..., object]:
            def watched_call(api: RustBindingsAPI, *args: object, **kwargs: object) -> object:
                called_while_adding.append(adding.is_set())
                if len(called_while_adding) == len(other_calls):
                    all_called.set()
                return call(api, *args, **kwargs)

            return watched_call

        with (
            patch.object(RustBindingsAPI, "_add", slow_add),
            patch.multiple(
                RustBindingsAPI,
                **{name: watched(getattr(RustBindingsAPI, name)) for name in ("_query", "_get", "_update", "_delete")},
            ),
        ):
            saving = asyncio.create_task(
                asyncio.to_thread(saver.vector_store.insert, vectors=[vector], ids=["memory-1"], payloads=[payload]),
            )
            assert await asyncio.to_thread(adding.wait, 5)
            results = dict(
                zip(
                    other_calls,
                    await asyncio.gather(*(asyncio.to_thread(call) for call in other_calls.values())),
                    strict=True,
                ),
            )
            await saving

        assert called_while_adding == [False] * len(other_calls)
        assert [hit.payload["data"] for hit in results["search"]] == ["PR review"]

    def test_mem0_embedder_records_every_failure_of_the_operation(self) -> None:
        """A later successful retry cannot hide a batch failure from the caller, whichever embedder Mem0 calls."""
        failure_detail = "embedder authentication failed (HTTP 401)"

        class BatchFailingEmbedder:
            sends_batches = True

            def embed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
                del purpose
                if len(texts) > 1:
                    raise EmbedderRequestError(failure_detail)
                return [[0.1, 0.2]]

            async def aembed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
                return self.embed(texts, purpose=purpose)

        strict = _Mem0Embedder(_Mem0StrictOpenAIEmbedder(BatchFailingEmbedder()), openai_embedder_error)
        mem0_own = MagicMock()
        mem0_own.embed_batch.side_effect = ConnectionError("Failed to connect to Ollama.")
        mem0_own.embed.return_value = [0.3]
        ollama = _Mem0Embedder(mem0_own, _ollama_embedder_error)

        with recorded_embedder_failures() as failures:
            for embedder in (strict, ollama):
                with pytest.raises(EmbedderRequestError) as raised:
                    embedder.embed_batch(["first", "second"])
                embedder.embed("first")
                # The raised error is the one recorded, so its retry judgment reaches the caller.
                assert failures[-1] is raised.value

        assert [(str(failure), failure.retryable) for failure in failures] == [
            (failure_detail, False),
            ("embedder endpoint unreachable", True),
        ]
        # Outside an operation nothing is recorded.
        with pytest.raises(EmbedderRequestError):
            ollama.embed_batch(["first", "second"])
        assert len(failures) == 2

    def test_mem0_strict_adapter_embeds_searches_as_queries_and_memories_as_documents(self) -> None:
        """Mem0 names the purpose of each embedding, so stored memories get the document prefix."""
        client = MagicMock()
        client.embeddings.create.side_effect = lambda **kwargs: SimpleNamespace(
            data=[SimpleNamespace(embedding=[1.0])]
            * (len(kwargs["input"]) if isinstance(kwargs["input"], list) else 1),
            usage=None,
        )
        adapter = _Mem0StrictOpenAIEmbedder(
            OpenAIEmbedder(
                model="embeddinggemma-2",
                api_key="sk-test",
                base_url="http://local/v1",
                client=client,
                query_prefix="q: ",
                document_prefix="d: ",
                health_recorder=capture_embedder_health_recorder(),
            ),
        )

        adapter.embed("find\nme", "search")
        adapter.embed("remember", "add")
        adapter.embed("changed", "update")
        adapter.embed_batch(["one", "two"])
        # Mem0 embeds the entities it finds in a search query in one batch.
        adapter.embed_batch(["Alice", "Bob"], "search")

        inputs = [call.kwargs["input"] for call in client.embeddings.create.call_args_list]
        assert inputs == ["q: find me", "d: remember", "d: changed", ["d: one", "d: two"], ["q: Alice", "q: Bob"]]

    def test_memory_auto_flush_batch_config_is_parameterized(self) -> None:
        """Auto-flush batch/extractor limits should be configurable."""
        memory = MemoryConfig.model_validate(
            {
                "backend": "file",
                "auto_flush": {
                    "enabled": True,
                    "batch": {
                        "max_sessions_per_cycle": 7,
                        "max_sessions_per_agent_per_cycle": 2,
                    },
                    "extractor": {
                        "max_messages_per_flush": 12,
                        "max_chars_per_flush": 9000,
                    },
                },
            },
        )
        assert memory.backend == "file"
        assert memory.auto_flush.enabled is True
        assert memory.auto_flush.batch.max_sessions_per_cycle == 7
        assert memory.auto_flush.batch.max_sessions_per_agent_per_cycle == 2
        assert memory.auto_flush.extractor.max_messages_per_flush == 12
        assert memory.auto_flush.extractor.max_chars_per_flush == 9000

    def test_memory_auto_flush_extractor_model_must_be_configured(self) -> None:
        """A misspelled extractor model fails when the config loads, not on every flush."""
        with pytest.raises(ValidationError, match=r"Unknown memory\.auto_flush\.extractor\.model: 'fast'"):
            Config(memory={"auto_flush": {"extractor": {"model": "fast"}}})

    def test_memory_auto_flush_default_interval_is_5_minutes(self) -> None:
        """Without the reply path waking it, auto-flush passes every five minutes by default."""
        memory = MemoryConfig()
        assert memory.auto_flush.flush_interval_seconds == 300

    def test_retired_auto_flush_fields_still_load(self) -> None:
        """A config the dashboard saved with the retired dirty-queue fields loads, and a save drops them."""
        memory = MemoryConfig.model_validate(
            {
                "backend": "file",
                "auto_flush": {"enabled": True, "max_dirty_age_seconds": 600, "max_cross_session_reprioritize": 5},
            },
        )
        assert memory.auto_flush.enabled is True
        assert memory.model_dump(exclude_unset=True) == {"backend": "file", "auto_flush": {"enabled": True}}

        with pytest.raises(ValidationError) as exc_info:
            MemoryConfig.model_validate({"auto_flush": {"max_dirty_age": 600}})
        assert config_validation_issues(exc_info.value) == [
            (("auto_flush", "max_dirty_age"), "Unknown key 'max_dirty_age': remove it or correct its spelling"),
        ]

    def test_retired_team_memory_fields_still_load(self) -> None:
        """A config the dashboard saved with the retired team-read switch and file fallback root loads, and a save drops them."""
        memory = MemoryConfig.model_validate(
            {
                "backend": "file",
                "team_reads_member_memory": True,
                "file": {"path": "team_memory", "max_entrypoint_lines": 9},
            },
        )
        assert memory.model_dump(exclude_unset=True) == {"backend": "file", "file": {"max_entrypoint_lines": 9}}

        with pytest.raises(ValidationError) as exc_info:
            MemoryConfig.model_validate({"file": {"paths": "team_memory"}})
        assert config_validation_issues(exc_info.value) == [
            (("file", "paths"), "Unknown key 'paths': remove it or correct its spelling"),
        ]

    def test_misspelled_embedder_field_fails_config_load(self) -> None:
        """An embedder field MindRoom does not define fails config load, naming its location, instead of doing nothing."""
        with pytest.raises(ValidationError) as exc_info:
            Config(memory={"embedder": {"provider": "openai", "config": {"base_url": "http://localhost:8080/v1"}}})

        assert config_validation_issues(exc_info.value) == [
            (("memory", "embedder", "config", "base_url"), "Unknown key 'base_url': remove it or correct its spelling"),
        ]

    def test_memory_config_accepts_disabled_backend(self) -> None:
        """The memory config should accept disabled memory in object and shorthand form."""
        explicit = MemoryConfig.model_validate({"backend": "none"})
        shorthand = MemoryConfig.model_validate("none")

        assert explicit.backend == "none"
        assert shorthand.backend == "none"

    def test_config_accepts_global_disabled_memory_shorthand(self) -> None:
        """The root config should normalize memory: none to a disabled memory config."""
        config = Config(
            agents={"scratch": {"display_name": "Scratch"}},
            memory="none",
            router=RouterConfig(model="default"),
        )

        assert config.memory.backend == "none"
        assert config.resolve_scope("scratch").memory_backend == "none"
        assert config.uses_file_memory() is False

    def test_config_accepts_per_agent_disabled_memory_backend(self) -> None:
        """Per-agent memory_backend should support disabling memory for one agent."""
        config = Config(
            agents={
                "general": {"display_name": "General"},
                "scratch": {"display_name": "Scratch", "memory_backend": "none"},
            },
            memory={"backend": "mem0"},
            router=RouterConfig(model="default"),
        )

        assert config.resolve_scope("general").memory_backend == "mem0"
        assert config.resolve_scope("scratch").memory_backend == "none"
        assert config.uses_file_memory() is False


def test_memory_search_defaults_to_keyword_daily_files() -> None:
    """File-memory search should default to keyword mode over daily memory files."""
    config = Config(router=RouterConfig(model="default"))

    search = memory_search(config, None)

    assert search.mode == "keyword"
    assert search.include == ["memory/**/*.md"]
    assert search.include_entrypoint is False


def test_agent_memory_search_override_merges_per_field() -> None:
    """Per-agent memory search overrides should inherit omitted global fields."""
    config = Config(
        memory={
            "search": {
                "mode": "semantic",
                "include": ["memory/**/*.md"],
                "include_entrypoint": False,
            },
        },
        agents={
            "openclaw": AgentConfig(
                display_name="OpenClaw",
                memory_backend="file",
                memory_search={"include_entrypoint": True},
            ),
        },
        router=RouterConfig(model="default"),
    )

    search = memory_search(config, "openclaw")

    assert search.mode == "semantic"
    assert search.include == ["memory/**/*.md"]
    assert search.include_entrypoint is True


def test_agent_memory_search_can_override_include_patterns() -> None:
    """Per-agent memory search should support custom include patterns."""
    config = Config(
        memory={
            "search": {
                "mode": "semantic",
                "include": ["memory/**/*.md"],
                "include_entrypoint": False,
            },
        },
        agents={
            "openclaw": AgentConfig(
                display_name="OpenClaw",
                memory_backend="file",
                memory_search={
                    "include": ["memory/**/*.md", "decisions/**/*.md"],
                    "include_entrypoint": True,
                },
            ),
        },
        router=RouterConfig(model="default"),
    )

    search = memory_search(config, "openclaw")

    assert search.include == ["memory/**/*.md", "decisions/**/*.md"]
    assert search.include_entrypoint is True


def test_memory_search_include_pattern_matches_direct_and_nested_daily_files() -> None:
    """The root glob matcher should treat memory/**/*.md as daily-memory files."""
    assert matches_root_glob("memory/2026-06-02.md", "memory/**/*.md")
    assert matches_root_glob("memory/2026/06/02.md", "memory/**/*.md")
    assert not matches_root_glob("MEMORY.md", "memory/**/*.md")
    assert not matches_root_glob("docs/runbook.md", "memory/**/*.md")


def test_memory_search_rejects_unsafe_include_pattern() -> None:
    """Memory search include patterns must stay inside the memory root."""
    with pytest.raises(ValueError, match=r"memory\.search\.include"):
        Config(
            memory={"search": {"include": ["../secret.md"]}},
            router=RouterConfig(model="default"),
        )
    with pytest.raises(ValueError, match=r"memory\.search\.include"):
        Config.model_validate(
            {"agents": {"mind": {"display_name": "Mind", "memory_search": {"include": ["../secret.md"]}}}},
        )
