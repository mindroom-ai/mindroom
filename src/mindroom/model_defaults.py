"""Central default model identifiers for generated configs and built-in tools."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = (
    "CODEX_GPT",
    "CONFIG_INIT_MODEL_PRESETS",
    "DEEPSEEK_REASONER",
    "GOOGLE_AVATAR_IMAGE",
    "GOOGLE_AVATAR_PROMPT",
    "GROQ_TRANSCRIPTION",
    "GROQ_TTS",
    "LLAMA_CPP_GEMMA",
    "LLAMA_CPP_QWEN",
    "OLLAMA_GEMMA",
    "OLLAMA_QWEN",
    "OPENAI_EMBEDDING_DIMENSIONS",
    "OPENAI_EMBEDDING_LARGE",
    "OPENAI_EMBEDDING_SMALL",
    "OPENAI_GPT_MINI",
    "OPENAI_GPT_NANO",
    "OPENAI_IMAGE",
    "OPENAI_REALTIME",
    "OPENAI_REALTIME_TRANSCRIPTION",
    "OPENAI_TRANSCRIPTION",
    "OPENAI_TTS",
    "SAAS_MODEL_PRESETS",
    "SENTENCE_TRANSFORMERS_DEFAULT",
    "ModelPreset",
    "llama_cpp_server_command",
)


@dataclass(frozen=True)
class ModelPreset:
    """Provider/model pair used by generated configuration."""

    provider: str
    id: str
    context_window: int | None = None

    def to_config_dict(self) -> dict[str, int | str]:
        """Return the minimal YAML-safe model config mapping."""
        config: dict[str, int | str] = {"provider": self.provider, "id": self.id}
        if self.context_window is not None:
            config["context_window"] = self.context_window
        return config


_ANTHROPIC_OPUS = "claude-opus-4-7"
_ANTHROPIC_SONNET = "claude-sonnet-4-6"
_ANTHROPIC_HAIKU = "claude-haiku-4-5"
CODEX_GPT = "gpt-5.5"
_OPENAI_GPT = "gpt-5.5"
OPENAI_GPT_MINI = "gpt-5.4-mini"
OPENAI_GPT_NANO = "gpt-5.4-nano"

GOOGLE_AVATAR_PROMPT = "gemini-3.1-flash-lite-preview"
GOOGLE_AVATAR_IMAGE = "gemini-3.1-flash-image-preview"

_OPENROUTER_CLAUDE_OPUS = "anthropic/claude-opus-4.7"
_OPENROUTER_CLAUDE_SONNET = "anthropic/claude-sonnet-4.6"
_OPENROUTER_CLAUDE_HAIKU = "anthropic/claude-haiku-4.5"
_OPENROUTER_GEMINI_FLASH = "google/gemini-3-flash-preview"
_OPENROUTER_GEMINI_LITE = "google/gemini-3.1-flash-lite-preview"
_OPENROUTER_OPENAI_MINI = "openai/gpt-5.4-mini"
_OPENROUTER_OPENAI_GPT_OSS_120B = "openai/gpt-oss-120b"
_OPENROUTER_DEEPSEEK_CHAT = "deepseek/deepseek-v4-pro"
_OPENROUTER_GLM = "z-ai/glm-5.1"
_OPENROUTER_KIMI = "moonshotai/kimi-k2.6"

OLLAMA_GEMMA = "gemma4"
OLLAMA_QWEN = "qwen3.6:27b"

LLAMA_CPP_GEMMA = "unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M"
LLAMA_CPP_QWEN = "unsloth/Qwen3.6-27B-GGUF:UD-Q4_K_XL"

OPENAI_EMBEDDING_SMALL = "text-embedding-3-small"
OPENAI_EMBEDDING_LARGE = "text-embedding-3-large"
SENTENCE_TRANSFORMERS_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"

OPENAI_TRANSCRIPTION = "gpt-4o-transcribe"
OPENAI_REALTIME_TRANSCRIPTION = "gpt-realtime-whisper"
OPENAI_TTS = "gpt-4o-mini-tts"
OPENAI_REALTIME = "gpt-realtime-2"
OPENAI_IMAGE = "gpt-image-2"

GROQ_TRANSCRIPTION = "whisper-large-v3"
GROQ_TTS = "playai-tts"

DEEPSEEK_REASONER = "deepseek-reasoner"

CONFIG_INIT_MODEL_PRESETS: Mapping[str, ModelPreset] = MappingProxyType(
    {
        "anthropic": ModelPreset("anthropic", _ANTHROPIC_SONNET, 1_000_000),
        "codex": ModelPreset("codex", CODEX_GPT, 258_000),
        "llama_cpp": ModelPreset("openai", LLAMA_CPP_GEMMA, 128_000),
        "ollama": ModelPreset("ollama", OLLAMA_GEMMA, 128_000),
        "openai": ModelPreset("openai", _OPENAI_GPT, 258_000),
        "openai_mini": ModelPreset("openai", OPENAI_GPT_MINI, 400_000),
        "openai_nano": ModelPreset("openai", OPENAI_GPT_NANO, 400_000),
        "openrouter": ModelPreset("openrouter", _OPENROUTER_CLAUDE_SONNET, 1_000_000),
        "vertexai_claude": ModelPreset("vertexai_claude", _ANTHROPIC_SONNET, 1_000_000),
    },
)

SAAS_MODEL_PRESETS: Mapping[str, ModelPreset] = MappingProxyType(
    {
        "default": ModelPreset("openrouter", _OPENROUTER_GEMINI_FLASH, 1_000_000),
        "gpt5mini": ModelPreset("openrouter", _OPENROUTER_OPENAI_MINI, 400_000),
        "gpt5nano": ModelPreset("openai", OPENAI_GPT_NANO, 400_000),
        "opus": ModelPreset("openrouter", _OPENROUTER_CLAUDE_OPUS, 1_000_000),
        "sonnet": ModelPreset("openrouter", _OPENROUTER_CLAUDE_SONNET, 1_000_000),
        "haiku": ModelPreset("openrouter", _OPENROUTER_CLAUDE_HAIKU, 200_000),
        "gemini_flash": ModelPreset("openrouter", _OPENROUTER_GEMINI_FLASH, 1_000_000),
        "gemini_lite": ModelPreset("openrouter", _OPENROUTER_GEMINI_LITE),
        "deepseek": ModelPreset("openrouter", _OPENROUTER_DEEPSEEK_CHAT, 1_048_576),
        "glm": ModelPreset("openrouter", _OPENROUTER_GLM, 202_752),
        "kimi": ModelPreset("openrouter", _OPENROUTER_KIMI, 262_144),
        "gpt_oss_120b": ModelPreset("openrouter", _OPENROUTER_OPENAI_GPT_OSS_120B, 128_000),
    },
)

OPENAI_EMBEDDING_DIMENSIONS: Mapping[str, int] = MappingProxyType(
    {
        OPENAI_EMBEDDING_LARGE: 3072,
        OPENAI_EMBEDDING_SMALL: 1536,
    },
)


def llama_cpp_server_command(model_id: str) -> str:
    """Return the llama.cpp OpenAI-compatible server command for a Hugging Face GGUF ref."""
    return f"llama-server -hf {model_id} --host 127.0.0.1 --port 8080"
