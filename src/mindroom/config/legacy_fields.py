"""Diagnostics for retired configuration fields."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, cast

from mindroom.logging_config import get_logger
from mindroom.providers.names import canonical_provider

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

# LEGACY_COMPAT: Scalar knowledge_base agent configuration.
# Legacy format: Agent config accepted scalar knowledge_base instead of knowledge_bases.
# Last legacy release: v2026.2.32; replacement: v2026.2.33 removed the scalar field.
# Handling: Reject the retired spelling with a directed replacement instead of silently discarding it.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_knowledge_base_field.

# LEGACY_COMPAT: Retired memory_dir agent configuration.
# Legacy format: Agent config accepted memory_dir for file-backed memory.
# Last legacy release: v2026.2.166; replacement: v2026.2.167 removed memory_dir.
# Handling: Reject it with guidance to current context files and file-memory configuration.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_memory_dir_field.

# LEGACY_COMPAT: Retired memory_file_path agent configuration.
# Legacy format: Agent config accepted memory_file_path as a file-memory location.
# Last legacy release: v2026.3.100; replacement: v2026.3.101 moved file memory to canonical workspaces.
# Handling: Reject it for every memory backend and direct authored paths to the workspace model.
# Coverage: tests/test_agents.py::test_config_rejects_memory_file_path_even_with_file_backend.

# LEGACY_COMPAT: Sandbox tools configured under sandbox_tools.
# Legacy format: Agent and defaults config accepted sandbox_tools instead of worker_tools.
# Last legacy release: v2026.3.71; replacement: v2026.3.72 renamed the field to worker_tools.
# Handling: Reject both retired locations with their current field name.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_defaults_sandbox_tools_field.

# LEGACY_COMPAT: Agent tool selection through allowed_toolkits and initial_toolkits.
# Legacy format: Agent config accepted allowed_toolkits and initial_toolkits.
# Last legacy release: v2026.6.11; replacement: v2026.6.12 removed both agent fields.
# Handling: Reject them with guidance to expand bundles into per-tool configuration.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_toolkit_fields_with_bundle_safe_hint.


def reject_legacy_agent_fields(value: object) -> object:
    """Reject retired agent fields before regular model validation."""
    if isinstance(value, dict):
        if "knowledge_base" in value:
            msg = "Agent field 'knowledge_base' was removed. Use 'knowledge_bases' (list) instead."
            raise ValueError(msg)
        if "memory_dir" in value:
            msg = "Agent field 'memory_dir' was removed. Use 'context_files' and memory.backend=file instead."
            raise ValueError(msg)
        if "memory_file_path" in value:
            msg = (
                "Agent field 'memory_file_path' was removed. File-backed agent memory now lives in the "
                "canonical agent workspace root; keep memory_backend=file and configure context_files "
                "relative to that workspace."
            )
            raise ValueError(msg)
        if "sandbox_tools" in value:
            msg = "Agent field 'sandbox_tools' was removed. Use 'worker_tools' instead."
            raise ValueError(msg)
        if "allowed_toolkits" in value:
            msg = (
                "Agent field 'allowed_toolkits' was removed. Expand toolkit/preset/bundle entries into individual "
                "tools before applying per-tool defer flags in tools."
            )
            raise ValueError(msg)
        if "initial_toolkits" in value:
            msg = (
                "Agent field 'initial_toolkits' was removed. Expand toolkit/preset/bundle entries into individual "
                "tools before applying per-tool initial flags in tools."
            )
            raise ValueError(msg)
    return value


# LEGACY_COMPAT: Unversioned toolkit-selection fields in defaults.
# Legacy format: Defaults allowed_toolkits and initial_toolkits are unversioned authored input, not native fields.
# Last legacy release: no tagged native model; v2026.4.19 first rejected these previously ignored defaults keys.
# Handling: Reject both spellings with guidance to defaults.tools; do not infer a native removal release.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_defaults_toolkit_fields.
# LEGACY_COMPAT: Retired defaults.auto_resume_after_restart switch.
# Legacy format: Defaults accepted auto_resume_after_restart to allow router resume messages after restarts.
# Last legacy release: v2026.10.121; replacement: v2026.10.122 continues interrupted replies in place and removes the field.
# Handling: Reject it with a note that interrupted replies now always continue in place.
# Coverage: tests/test_agents.py::test_config_rejects_retired_auto_resume_after_restart.
def reject_legacy_defaults_fields(value: object) -> object:
    """Reject retired default fields before regular model validation."""
    if isinstance(value, dict):
        if "auto_resume_after_restart" in value:
            msg = (
                "defaults.auto_resume_after_restart was removed. A reply interrupted by a restart now always "
                "continues in place in the same message; remove the setting."
            )
            raise ValueError(msg)
        if "sandbox_tools" in value:
            msg = "defaults.sandbox_tools was removed. Use defaults.worker_tools instead."
            raise ValueError(msg)
        if "allowed_toolkits" in value:
            msg = "defaults.allowed_toolkits was removed. Use defaults.tools instead."
            raise ValueError(msg)
        if "initial_toolkits" in value:
            msg = "defaults.initial_toolkits was removed. Use defaults.tools instead."
            raise ValueError(msg)
    return value


# LEGACY_COMPAT: Retired Agno Learning switch.
# Legacy format: Agent and defaults config accepted learning to enable Agno Learning.
# Last legacy release: v2026.10.478; replacement: unreleased.
# Handling: Warn once per process and drop the value; existing learning databases stay on disk unread.
# Coverage: tests/test_agents.py::test_config_ignores_retired_learning_with_one_warning.

# LEGACY_COMPAT: Retired Agno Learning mode.
# Legacy format: Agent and defaults config accepted learning_mode (always or agentic) for Agno Learning.
# Last legacy release: v2026.10.478; replacement: unreleased.
# Handling: Warn once per process and drop the value.
# Coverage: tests/test_agents.py::test_config_ignores_retired_learning_mode_with_one_warning.

# LEGACY_COMPAT: Retired tool-result compression switch.
# Legacy format: Agent and defaults config accepted compress_tool_results to compress replayed tool results.
# Last legacy release: v2026.10.478; replacement: unreleased.
# Handling: Warn once per process and drop the value; tool results replay uncompressed.
# Coverage: tests/test_agents.py::test_config_ignores_retired_compress_tool_results_with_one_warning.
_RETIRED_FIELD_REASONS = {
    "learning": "Agno Learning was retired; existing learning databases stay on disk unused",
    "learning_mode": "Agno Learning was retired",
    "compress_tool_results": "Tool-result compression was retired",
}


@cache
def _warn_retired_field(field: str, reason: str) -> None:
    logger.warning("Ignoring a retired config field; remove it from the config", field=field, reason=reason)


def _drop_retired[T](value: dict[str, T], reasons: Mapping[str, str], prefix: str = "") -> dict[str, T]:
    """Return ``value`` without the keys ``reasons`` retires, warning once per field it names under ``prefix``."""
    retired = reasons.keys() & value.keys()
    if not retired:
        return value
    for field in sorted(retired):
        _warn_retired_field(f"{prefix}{field}", reasons[field])
    return {key: item for key, item in value.items() if key not in retired}


def drop_retired_fields(value: object) -> object:
    """Drop retired agent and default fields that older configs still set, warning once per field."""
    if not isinstance(value, dict):
        return value
    return _drop_retired(cast("dict[str, object]", value), _RETIRED_FIELD_REASONS)


@dataclass(frozen=True)
class _RetiredModelSettings:
    """Agno model fields that ``extra_kwargs`` passed through on ``providers``, or on every provider when unset."""

    fields: frozenset[str]
    providers: frozenset[str] | None
    reason: str


_OPENAI_CHAT_PROVIDERS = frozenset(
    {"openai", "azure", "openrouter", "deepseek", "llama_cpp", "zai", "kimi", "kimi_code"},
)
_CLAUDE_PROVIDERS = frozenset({"anthropic", "vertexai_claude", "bedrock_claude"})
_GEMINI_PROVIDERS = frozenset({"google", "gemini"})

# LEGACY_COMPAT: Agno model fields in models.<name>.extra_kwargs.
# Legacy format: extra_kwargs went to the provider's Agno model class as keyword arguments, so every Agno model field
#   loaded: the base Model fields on any provider, and the feature fields of Agno's OpenAI Chat, OpenAI Responses,
#   Claude, and Gemini classes on the providers built from them.
# Last legacy release: v2026.10.478; replacement: unreleased.
#   These keys are authored input that no MindRoom model wrote, so no release stored them.
# Handling: Warn once per field and drop it on the providers whose Agno class took it; every other key the provider's
#   adapter does not accept still fails when the model is built, with the accepted keys listed.
# Coverage: tests/test_extra_kwargs.py::test_a_retired_agno_field_loads_with_one_warning_and_the_model_answers,
#   tests/test_extra_kwargs.py::test_retired_agno_fields_load_with_one_warning_on_the_providers_that_took_them,
#   tests/test_extra_kwargs.py::test_a_retired_field_that_two_providers_took_warns_once,
#   tests/test_extra_kwargs.py::test_keys_no_agno_class_of_the_provider_took_still_fail_at_build,
#   tests/test_extra_kwargs.py::test_no_route_accepts_a_retired_agno_field_of_its_provider.
_RETIRED_MODEL_SETTINGS = (
    _RetiredModelSettings(
        fields=frozenset(
            {
                "name",
                "provider",
                "model_type",
                "supports_native_structured_outputs",
                "supports_json_schema_outputs",
                "_tool_choice",
                "system_prompt",
                "instructions",
                "tool_message_role",
                "assistant_message_role",
                "cache_response",
                "cache_ttl",
                "cache_dir",
                "retries",
                "delay_between_retries",
                "exponential_backoff",
                "retry_with_guidance",
                "retry_with_guidance_limit",
            },
        ),
        providers=None,
        reason="MindRoom's provider adapters replaced Agno's model classes and do not read Agno's base model settings",
    ),
    _RetiredModelSettings(
        fields=frozenset({"audio", "modalities"}),
        providers=_OPENAI_CHAT_PROVIDERS,
        reason="Audio replies were retired with Agno's OpenAI Chat model",
    ),
    _RetiredModelSettings(
        fields=frozenset({"collect_metrics_on_completion"}),
        providers=_OPENAI_CHAT_PROVIDERS | _GEMINI_PROVIDERS,
        reason="Agno's metrics switch was retired with Agno's OpenAI Chat and Gemini models",
    ),
    _RetiredModelSettings(
        fields=frozenset({"background", "background_poll_interval", "background_max_wait", "vector_store_name"}),
        providers=frozenset({"openai", "codex", "openai_codex"}),
        reason="Background polling and file-search uploads were retired with Agno's OpenAI Responses model",
    ),
    _RetiredModelSettings(
        fields=frozenset(
            {
                "skills",
                "mcp_servers",
                "cache_tools",
                "system_prompt_blocks",
                "citations",
                "append_trailing_user_message",
                "trailing_user_message_content",
            },
        ),
        providers=_CLAUDE_PROVIDERS,
        reason="Agno's Claude skills, MCP connector, and request-shaping switches were retired with Agno's Claude model",
    ),
    _RetiredModelSettings(
        fields=frozenset({"auth_token"}),
        providers=_CLAUDE_PROVIDERS - {"anthropic"},
        reason="Vertex AI and Bedrock Claude authenticate with cloud credentials, never an Anthropic auth token",
    ),
    _RetiredModelSettings(
        fields=frozenset(
            {
                "grounding",
                "grounding_dynamic_threshold",
                "url_context",
                "vertexai_search",
                "vertexai_search_datastore",
                "parallel_search",
                "parallel_api_key",
                "parallel_config",
                "file_search_store_names",
                "file_search_metadata_filter",
                "request_params",
                "function_declarations",
            },
        ),
        providers=_GEMINI_PROVIDERS,
        reason="Gemini built-in tools other than search, request overrides, and function declarations were retired "
        "with Agno's Gemini model",
    ),
    _RetiredModelSettings(
        fields=frozenset({"response_modalities", "speech_config"}),
        providers=_GEMINI_PROVIDERS,
        reason="Image and audio replies were retired with Agno's Gemini model",
    ),
)


def drop_retired_model_settings(provider: str, extra_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop the Agno model fields that a ``provider`` model's ``extra_kwargs`` still sets, warning once per field."""
    canonical = canonical_provider(provider)
    reasons = {
        field: retired.reason
        for retired in _RETIRED_MODEL_SETTINGS
        if retired.providers is None or canonical in retired.providers
        for field in retired.fields
    }
    return _drop_retired(extra_kwargs, reasons, "extra_kwargs.")


# LEGACY_COMPAT: Retired website tool knowledge option.
# Legacy format: A website tool entry accepted a text knowledge override, which swapped read_url for a knowledge-base
#   insert that always failed, because config can supply only a string.
# Last legacy release: v2026.10.478; replacement: unreleased.
# Handling: Warn once and drop the override, so the tool offers read_url; the tool layer itself rejects the field.
# Coverage: tests/test_website_tool.py::test_a_retired_knowledge_option_loads_with_one_warning_and_reads_urls.
_RETIRED_TOOL_OVERRIDE_REASONS = {
    "website": {"knowledge": "The website tool no longer adds pages to a knowledge base; it always offers read_url"},
}


def drop_retired_tool_overrides(tool_name: str, overrides: dict[str, object]) -> dict[str, object]:
    """Drop retired overrides that an older entry for ``tool_name`` still sets, warning once per field."""
    return _drop_retired(overrides, _RETIRED_TOOL_OVERRIDE_REASONS.get(tool_name, {}), f"tools.{tool_name}.")
