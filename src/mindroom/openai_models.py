"""OpenAI and OpenAI-compatible models with cross-provider tool-call replay support."""

from __future__ import annotations

import base64
import math
import os
import struct
from copy import copy, deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, cast

from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.openai import OpenAIChat, OpenAIResponses
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter
from agno.utils.media import get_image_type
from agno.utils.tokens import _parse_image_dimensions_from_bytes

from mindroom.agno_compat_openai_chat import OpenAIChatProviderCompat as AgnoOpenAIChatProviderCompat
from mindroom.agno_compat_openai_responses import OpenAIResponsesProviderCompat
from mindroom.agno_compat_openai_responses_items import record_response_output, record_tool_search_items
from mindroom.history.message_content import image_content_for_token_estimation
from mindroom.legacy_openai_tool_replay import repair_legacy_openai_tool_replay
from mindroom.model_defaults import OPENAI_IMAGE_ORIGINAL_NO_PATCH_BUDGET_PREFIXES, OPENAI_IMAGE_PATCH_MODEL_PREFIXES
from mindroom.native_compaction import (
    NativeCompactionModel,
    common_native_endpoint,
    native_replay_messages,
    record_native_checkpoint,
    recorded_native_settings,
)
from mindroom.openai_prompt_cache import formatted_input_with_shared_system_prefix, supports_openai_cache_breakpoints
from mindroom.openai_response_replay import formatted_input_with_provider_items
from mindroom.openai_tool_search import (
    model_deferred_tool_names,
    request_params_with_deferred_tool_search,
)
from mindroom.provider_tool_policy import disable_tool_selection, provider_tools_disabled
from mindroom.token_budget import approximate_o200k_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.tools.function import Function
    from openai.types.responses import Response
    from pydantic import BaseModel


class OpenAIChatProviderCompat(AgnoOpenAIChatProviderCompat):
    """Repair canonical tool replay after provider parser compatibility.

    Mix in ahead of an ``OpenAIChat`` subclass; ``_format_all_messages`` is the
    single choke point for all four request paths.  Deliberately not a
    dataclass and not an ``OpenAIChat`` subclass: either would re-apply
    ``OpenAIChat`` field defaults over provider-specific ones (base URL, name)
    during dataclass field collection.
    """

    def get_request_params(
        self,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
    ) -> dict[str, Any]:
        """Reject mandatory native search before reaching a Chat Completions client."""
        request_params = super().get_request_params(  # ty: ignore[unresolved-attribute]
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
        )
        if provider_tools_disabled():
            extra_body = request_params.get("extra_body")
            sources = [request_params, extra_body] if isinstance(extra_body, dict) else [request_params]
            model_ids = [cast("OpenAIChat", self).id, *(str(source.get("model", "")) for source in sources)]
            model_ids.extend(str(model_id) for source in sources for model_id in source.get("models") or [])
            if any(source.get("web_search_options") is not None for source in sources) or any(
                marker in model_id.casefold() for model_id in model_ids for marker in ("-search-api", "-search-preview")
            ):
                msg = "Participation decisions cannot disable native Chat Completions search"
                raise ValueError(msg)
            # OpenRouter runs online variants and web plugins once before generation.
            # Respect the SDK's final extra_body override, including enabled=False.
            plugins = (
                extra_body.get("plugins", request_params.get("plugins"))
                if isinstance(extra_body, dict)
                else request_params.get("plugins")
            )
            if any("online" in model_id.casefold().split(":")[1:] for model_id in model_ids) or any(
                isinstance(plugin, dict) and plugin.get("id") == "web" and plugin.get("enabled") is not False
                for plugin in plugins or []
            ):
                msg = "Participation decisions cannot disable native OpenRouter search"
                raise ValueError(msg)
        return disable_tool_selection(request_params)

    def _format_all_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
    ) -> list[dict[str, Any]]:
        """Supply the arguments string required by OpenAI for every tool call."""
        return super()._format_all_messages(  # ty: ignore[unresolved-attribute]  # resolved by the OpenAIChat sibling base
            repair_legacy_openai_tool_replay(messages),
            compress_tool_results,
        )


@dataclass
class MindRoomOpenAIChat(OpenAIChatProviderCompat, OpenAIChat):
    """OpenAI Chat model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAILike(OpenAIChatProviderCompat, OpenAILike):
    """OpenAI-compatible endpoint model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenRouter(OpenAIChatProviderCompat, OpenRouter):
    """OpenRouter model that can replay tool calls from other providers."""


@dataclass
class MindRoomDeepSeek(OpenAIChatProviderCompat, DeepSeek):
    """DeepSeek model that can replay tool calls from other providers."""


@dataclass
class MindRoomLlamaCpp(OpenAIChatProviderCompat, LlamaCpp):
    """llama.cpp server model that can replay tool calls from other providers."""


# AGNO_COMPAT: Byte-only image dimension parsing is exposed only as a private helper.
# Reason: Agno's higher-level image helper can fetch URLs or open files; the
# private header parser supplies dimensions without I/O or pixel decoding.
# Upstream issue: No matching public byte-only dimension API issue identified.
# Upstream PR: None identified; the missing public utility remains untracked.
# Remove when: Agno exposes a public byte-only parser; retain bounded header
# decoding, unknown-format fallback, and MindRoom's visual token budgeting.
# Coverage: tests/test_native_compaction_history.py::test_responses_image_budget_reads_only_bounded_header_bytes;
# tests/test_native_compaction_history.py::test_responses_image_header_dimensions_keep_nonzero_visual_budget.
def _embedded_image_dimensions(source: object) -> tuple[int, int] | None:
    """Read known image headers locally, with no URL fetch or pixel decoding."""
    if not isinstance(source, str) or not source.startswith("data:image/") or ";base64," not in source:
        return None
    try:
        # Bound allocations and JPEG marker scanning; longer metadata uses the fallback.
        offset = source.index(",") + 1
        data = base64.b64decode(source[offset : offset + 64 * 1024], validate=True)
        image_type = get_image_type(data)
        if image_type == "webp" and data[12:16] not in {b"VP8X", b"VP8 ", b"VP8L"}:
            return None
        if image_type in {"png", "gif", "jpeg", "webp"}:
            width, height = _parse_image_dimensions_from_bytes(data, image_type)
            if width > 0 and height > 0:
                return width, height
    except (ValueError, TypeError, struct.error):
        pass
    return None


def _responses_image_tokens(block: dict[str, Any], model_id: str) -> int:
    """Estimate visual patches, never tokenize encoded image transport as text.

    Current patch-model sizing: https://developers.openai.com/api/docs/guides/images-vision
    Unknown dimensions use the model/detail image ceiling.
    """
    detail = block.get("detail", "auto")
    recent = model_id.startswith(OPENAI_IMAGE_ORIGINAL_NO_PATCH_BUDGET_PREFIXES)
    if detail == "auto":
        detail = "high" if model_id.startswith("gpt-5.4") else "original"
    max_dimension, patch_limit = 65_535, 30_000
    if detail == "low":
        max_dimension, patch_limit = (2048, 6144) if model_id.startswith("gpt-5.4") else (512, 256)
    elif detail == "high":
        max_dimension, patch_limit = (65_535 if model_id.startswith("gpt-6") else 2048), 2500
    elif not recent:
        max_dimension, patch_limit = 6000, 10_000

    dimensions = _embedded_image_dimensions(block.get("image_url"))
    if dimensions is None:
        return math.ceil(patch_limit * 1.2)
    width, height = dimensions
    scale = min(1, max_dimension / max(width, height))
    patches = math.ceil(math.ceil(width * scale) / 32) * math.ceil(math.ceil(height * scale) / 32)
    # The resize budget is a conservative upper bound; exact resized coverage
    # can be slightly smaller. Original detail on recent models is not resized
    # to the 30,000-patch rejection limit, so do not hide oversized input.
    if not (recent and detail == "original"):
        patches = min(patches, patch_limit)
    return math.ceil(patches * 1.2)


def _prepare_response_continuation(
    messages: list[Message],
    *,
    explicit_replay: bool = False,
) -> tuple[list[Message], bool]:
    """Avoid chaining to an unstored response or an older incomplete server context."""
    latest = next(
        (
            message.provider_data
            for message in reversed(messages)
            if message.role == "assistant" and message.provider_data and message.provider_data.get("response_id")
        ),
        None,
    )
    if not explicit_replay and (latest is None or latest.get("mindroom_response_stored") is not False):
        return messages, False
    return [
        message.model_copy(
            update={
                "provider_data": {key: value for key, value in message.provider_data.items() if key != "response_id"},
            },
        )
        if message.provider_data and "response_id" in message.provider_data
        else message
        for message in messages
    ], True


@dataclass
class MindRoomOpenAIResponses(NativeCompactionModel, OpenAIResponsesProviderCompat, OpenAIResponses):
    """OpenAI Responses model that preserves completed continuation and ordered output."""

    approval_receipt_after_response_id: ClassVar[bool] = True
    supports_prompt_cache_breakpoints: ClassVar[bool] = True
    cache_system_prompt: bool = True
    _store_before_native_compaction: bool | None = field(default=None, init=False, repr=False)
    _portable_replay: bool = field(default=False, init=False, repr=False)

    def configure_portable_replay(self, *, enabled: bool = True) -> None:
        """Replay the locally budgeted history without hidden server-side context."""
        self._portable_replay = enabled

    def restore_portable_replay(self, message: Message | None) -> None:
        """Restore exact saved policy, using canonical provenance for legacy responses."""
        data = (message.provider_data or {}) if message is not None else {}
        saved = data.get("mindroom_portable_replay")
        if isinstance(saved, bool):
            self.configure_portable_replay(enabled=saved)
            return
        items = data.get("mindroom_response_output")
        has_ordered_reasoning = (
            isinstance(items, list)
            and all(isinstance(item, dict) for item in items)
            and any(
                item.get("type") == "reasoning"
                and isinstance(encrypted := item.get("encrypted_content"), str)
                and bool(encrypted.strip())
                for item in items
            )
        )
        # Legacy records cannot always reveal the original budget. Preserve
        # stored continuation unless canonical replay has its own provenance.
        self.configure_portable_replay(
            enabled=data.get("mindroom_response_stored") is False
            or (message is not None and recorded_native_settings(message) is not None)
            or has_ordered_reasoning,
        )

    def estimate_portable_replay_tokens(self, messages: list[Message]) -> int:
        """Count the explicit Responses payload used by portable history planning."""
        replay_model = copy(self)
        replay_model._portable_replay = True
        replay_model.native_compaction = None
        formatted = replay_model._format_messages(messages)
        if not self.portable_replay_uses_visual_tokens():
            return approximate_o200k_tokens(stable_serialize(formatted))
        estimated_input = []
        image_tokens = 0
        for item in formatted:
            projected = dict(item)
            for field_name in ("content", "output"):
                content = item.get(field_name)
                if isinstance(content, list):
                    projected[field_name] = [image_content_for_token_estimation(block) for block in content]
                    image_tokens += sum(
                        _responses_image_tokens(block, self.id)
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "input_image"
                    )
            estimated_input.append(projected)
        return approximate_o200k_tokens(stable_serialize(estimated_input)) + image_tokens

    def portable_replay_uses_visual_tokens(self) -> bool:
        """Use visual accounting only for models with known image patch budgets."""
        return self.id.startswith(OPENAI_IMAGE_PATCH_MODEL_PREFIXES)

    def native_compaction_supported(self) -> bool:
        """Use explicit replay on public Responses and Codex routes."""
        return (
            self.store is not True
            and not self.background
            and self.id.startswith(("gpt-5.3-codex", "gpt-5.4", "gpt-6"))
            and self.native_compaction_endpoint()
            in {
                "https://api.openai.com/v1",
                "https://chatgpt.com/backend-api/codex",
            }
            and not any(
                any(key in params for key in ("context_management", "previous_response_id", "background", "store"))
                for params in (
                    self.request_params or {},
                    self.extra_body or {},
                    (self.request_params or {}).get("extra_body") or {},
                )
            )
        )

    def native_compaction_endpoint(self) -> str:
        """Bind replay to the effective client endpoint."""
        clients = [client for client in (self.async_client, self.client) if client is not None]
        if clients:
            return common_native_endpoint([str(client.base_url).rstrip("/") for client in clients])
        return str(
            (self.client_params or {}).get("base_url")
            or self.base_url
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1",
        ).rstrip("/")

    def configure_native_compaction(
        self,
        *,
        threshold: int | None,
        history_generation: str = "",
        allow_authored: bool = False,
    ) -> None:
        """Use self-contained replay when native compaction is enabled."""
        if self.native_compaction is not None:
            self.store = self._store_before_native_compaction
        super().configure_native_compaction(
            threshold=threshold,
            history_generation=history_generation,
            allow_authored=allow_authored,
        )
        if self.native_compaction is not None:
            self._store_before_native_compaction = self.store
            self.store = False

    def __post_init__(self) -> None:
        """Use one storage setting for request construction and history replay."""
        super().__post_init__()
        if self.request_params is not None and "store" in self.request_params:
            self.request_params = dict(self.request_params)
            self.store = self.request_params.pop("store")
        if self.background and self.store is False:
            msg = "Background Responses require store=True"
            raise ValueError(msg)

    def get_request_params(
        self,
        messages: list[Message] | None = None,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
    ) -> dict[str, Any]:
        """Tag deferred functions and add hosted tool search."""
        if provider_tools_disabled():
            # Agno mutates nested function schemas and inserts deep-research tools.
            tools = deepcopy(tools)
        if messages is not None:
            messages, _ = _prepare_response_continuation(messages, explicit_replay=self._portable_replay)
        request_params = super().get_request_params(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
        )
        if self._portable_replay:
            request_params.pop("previous_response_id", None)
            if isinstance(extra_body := request_params.get("extra_body"), dict):
                request_params["extra_body"] = {
                    key: value for key, value in extra_body.items() if key != "previous_response_id"
                }
            include = list(request_params.get("include") or [])
            if "reasoning.encrypted_content" not in include:
                include.append("reasoning.encrypted_content")
            request_params["include"] = include
        if self.native_compaction is not None:
            request_params["context_management"] = [
                {"type": "compaction", "compact_threshold": self.native_compaction.threshold},
            ]
        request_params = request_params_with_deferred_tool_search(request_params, model_deferred_tool_names(self))
        return disable_tool_selection(request_params)

    def _format_tool_params(
        self,
        messages: list[Message],
        tools: list[Function | dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Avoid eager file-search uploads during a decision that cannot use them."""
        return super()._format_tool_params([] if provider_tools_disabled() else messages, tools)

    def _format_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
        tools: list[Function | dict[str, Any]] | None = None,
    ) -> list[Any]:
        """Reconstruct ordered provider output after canonical history conversion."""
        messages, explicit_replay = _prepare_response_continuation(messages, explicit_replay=self._portable_replay)
        messages = repair_legacy_openai_tool_replay(messages)
        route = self.native_compaction.route if self.native_compaction is not None else None
        messages = native_replay_messages(messages, route)
        # Agno couples encrypted-reasoning replay to response storage. A local
        # view preserves stateless history even when the next response is stored.
        replay_model = copy(self) if explicit_replay else self
        if explicit_replay:
            replay_model.store = False
        formatted_input = OpenAIResponses._format_messages(replay_model, messages, compress_tool_results, tools=tools)
        if replay_model.store is not False:
            # Match Agno's continuation boundary before locating assistant spans.
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if message.role == "assistant" and message.provider_data and "response_id" in message.provider_data:
                    messages = messages[index + 1 :]
                    break
        formatted_input = formatted_input_with_provider_items(
            messages,
            formatted_input,
            native_route=route,
            replay_reasoning=replay_model.store is False,
        )
        if self.cache_system_prompt:
            formatted_input = formatted_input_with_shared_system_prefix(
                formatted_input,
                explicit_breakpoint=self.supports_prompt_cache_breakpoints and supports_openai_cache_breakpoints(self),
            )
        return formatted_input

    def _parse_provider_response(self, response: Response, **kwargs: object) -> ModelResponse:
        """Capture completed provider output and response storage provenance."""
        model_response = super()._parse_provider_response(response, **kwargs)
        model_response.provider_data = {
            **(model_response.provider_data or {}),
            "mindroom_response_stored": self.store is not False,
            "response_status": response.status,
            "incomplete_reason": response.incomplete_details.reason
            if response.incomplete_details is not None
            else None,
        }
        record_tool_search_items(model_response, response.output)
        if response.status == "completed":
            items = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
            self._record_completed_responses_output(model_response, items)
        return model_response

    def _record_completed_responses_output(
        self,
        model_response: ModelResponse,
        items: list[dict[str, Any]],
    ) -> None:
        """Apply application replay and compaction policy after provider completion."""
        model_response.provider_data = {
            **(model_response.provider_data or {}),
            "mindroom_response_stored": self.store is not False,
            "mindroom_portable_replay": self._portable_replay,
        }
        record_native_checkpoint(model_response, items, self.native_compaction)
        if self.store is False or self._portable_replay:
            record_response_output(model_response, items)

    def _record_provider_only_responses_items(
        self,
        model_response: ModelResponse,
        output_items: list[Any],
    ) -> None:
        """Capture provider-only items selected by canonical replay policy."""
        record_tool_search_items(model_response, output_items)

    def _should_buffer_responses_output(self) -> bool:
        """Buffer ordered streaming output only for explicit replay modes."""
        return self.store is False or self._portable_replay
