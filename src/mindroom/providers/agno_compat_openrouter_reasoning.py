"""Temporary compatibility for Agno's streamed OpenRouter reasoning details."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.models.base import MessageData
    from agno.models.message import Message
    from agno.models.response import ModelResponse

_OPENROUTER_REASONING_DETAILS_BUFFER = "_mindroom_openrouter_reasoning_details_buffer"


def _openrouter_detail_values_match(left: object, right: object) -> bool:
    """Compare JSON-like metadata without equating booleans and numbers."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        left_dict = cast("dict[object, object]", left)
        right_dict = cast("dict[object, object]", right)
        return left_dict.keys() == right_dict.keys() and all(
            _openrouter_detail_values_match(left_dict[key], right_dict[key]) for key in left_dict
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _openrouter_detail_values_match(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _openrouter_reasoning_text_detail_parts(detail: object) -> tuple[dict[str, object], str] | None:
    """Return exact metadata and text for a fragment that can be grouped."""
    if not isinstance(detail, dict) or not all(isinstance(key, str) for key in detail):
        return None
    detail_dict = cast("dict[str, object]", detail)
    if detail_dict.get("type") != "reasoning.text":
        return None
    text = detail_dict.get("text")
    if not isinstance(text, str):
        return None

    index = detail_dict.get("index")
    detail_id = detail_dict.get("id")
    has_stable_discriminator = type(index) in (int, float) or (isinstance(detail_id, str) and bool(detail_id))
    if not has_stable_discriminator:
        return None

    return ({key: value for key, value in detail_dict.items() if key != "text"}, text)


@dataclass
class _OpenRouterReasoningTextBlock:
    """One metadata template plus linearly accumulated adjacent text chunks."""

    metadata: dict[str, object]
    text_chunks: list[str]


def _append_openrouter_reasoning_detail_block(blocks: list[object], detail: object) -> None:
    """Append one detail to compact streaming blocks without concatenating text."""
    detail_parts = _openrouter_reasoning_text_detail_parts(detail)
    if detail_parts is None:
        blocks.append(detail)
        return

    metadata, text = detail_parts
    if blocks and isinstance(blocks[-1], _OpenRouterReasoningTextBlock):
        previous_block = blocks[-1]
        if _openrouter_detail_values_match(previous_block.metadata, metadata):
            previous_block.text_chunks.append(text)
            return
    blocks.append(_OpenRouterReasoningTextBlock(metadata=metadata, text_chunks=[text]))


def _materialized_openrouter_reasoning_detail_blocks(blocks: list[object]) -> list[object]:
    """Convert private streaming blocks into persistable provider dictionaries."""
    return [
        {**block.metadata, "text": "".join(block.text_chunks)}
        if isinstance(block, _OpenRouterReasoningTextBlock)
        else block
        for block in blocks
    ]


def coalesced_openrouter_reasoning_details(details: object) -> object:
    """Join only adjacent, unambiguously matching OpenRouter text fragments."""
    if not isinstance(details, list):
        return details

    blocks: list[object] = []
    for detail in details:
        _append_openrouter_reasoning_detail_block(blocks, detail)
    return _materialized_openrouter_reasoning_detail_blocks(blocks)


# AGNO_COMPAT: Streamed OpenRouter reasoning details persist one dictionary per token fragment.
# Reason: Agno 3.0.9 merges streamed provider data by extending list values, so
# OpenRouter's per-chunk `reasoning_details` fragments are stored and replayed as
# hundreds of thousands of dictionaries on long responses, which amplifies memory
# when histories are rebuilt. Only adjacent `reasoning.text` fragments with
# identical metadata and a stable `index` or `id` are joined.
# Upstream issue: https://github.com/agno-agi/agno/issues/8794, open.
# Upstream PR: https://github.com/agno-agi/agno/pull/8861, open; partial, because it
# merges fragments only when formatting replay and still persists every fragment.
# Remove when: The pinned Agno stores streamed OpenRouter reasoning details as
# complete blocks, while caller-visible stream deltas stay unchanged.
# Coverage: tests/test_openai_models.py::test_openrouter_reasoning_details_stream_coalesces_across_deltas_and_preserves_yields;
# tests/test_openai_models.py::test_openrouter_reasoning_details_stream_joins_fragments_only_when_finalized;
# tests/test_openai_models.py::test_openrouter_reasoning_details_stream_preserves_later_non_list_value;
# tests/test_openai_models.py::test_openrouter_reasoning_details_stream_list_after_non_list_starts_a_new_sequence.
class OpenRouterReasoningDetailsCompat:
    """Buffer streamed OpenRouter reasoning details as compact blocks.

    Keep this as a plain mixin ahead of the concrete Agno model. Making it a
    dataclass or an OpenAIChat subclass would overwrite provider-specific field
    defaults during dataclass collection.
    """

    def _populate_stream_data(
        self,
        stream_data: MessageData,
        model_response_delta: ModelResponse,
    ) -> Iterator[ModelResponse]:
        """Accumulate reasoning details without retaining each text token separately."""
        provider_data = model_response_delta.provider_data
        if (
            provider_data
            and "reasoning_details" in provider_data
            and not isinstance(provider_data["reasoning_details"], list)
            and hasattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
        ):
            delattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
        if provider_data and isinstance(provider_data.get("reasoning_details"), list):
            buffered_details = getattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER, None)
            if not isinstance(buffered_details, list):
                buffered_details = []
                if stream_data.response_provider_data is not None:
                    existing_details = stream_data.response_provider_data.pop("reasoning_details", None)
                    if isinstance(existing_details, list):
                        for detail in existing_details:
                            _append_openrouter_reasoning_detail_block(buffered_details, detail)
                setattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER, buffered_details)
            for detail in provider_data["reasoning_details"]:
                _append_openrouter_reasoning_detail_block(buffered_details, detail)

            remaining_provider_data = {key: value for key, value in provider_data.items() if key != "reasoning_details"}
            sanitized_delta = replace(model_response_delta, provider_data=remaining_provider_data)

            for _ in super()._populate_stream_data(stream_data, sanitized_delta):  # ty: ignore[unresolved-attribute]
                yield model_response_delta
            return

        yield from super()._populate_stream_data(stream_data, model_response_delta)  # ty: ignore[unresolved-attribute]

    def _populate_assistant_message_from_stream_data(
        self,
        assistant_message: Message,
        stream_data: MessageData,
    ) -> None:
        """Materialize buffered reasoning fragments once before persistence."""
        buffered_details = getattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER, None)
        if isinstance(buffered_details, list):
            if stream_data.response_provider_data is None:
                stream_data.response_provider_data = {}
            stream_data.response_provider_data["reasoning_details"] = _materialized_openrouter_reasoning_detail_blocks(
                buffered_details,
            )
            delattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
        super()._populate_assistant_message_from_stream_data(  # ty: ignore[unresolved-attribute]
            assistant_message,
            stream_data,
        )
