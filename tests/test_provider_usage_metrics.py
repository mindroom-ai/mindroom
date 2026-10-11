"""Provider usage reaches MindRoom with the same counter meanings for every provider."""

from __future__ import annotations

import pytest
from cerebras.cloud.sdk.types.chat.chat_completion import (
    ChatChunkResponseUsage,
    ChatChunkResponseUsagePromptTokensDetails,
    ChatCompletionResponseUsage,
    ChatCompletionResponseUsagePromptTokensDetails,
)
from google.genai.types import GenerateContentResponseUsageMetadata
from groq.types import CompletionUsage
from groq.types.completion_usage import CompletionTokensDetails, PromptTokensDetails

from mindroom.cerebras_model import MindRoomCerebras
from mindroom.google_gemini import MindRoomGoogleGemini
from mindroom.groq_model import MindRoomGroq


def test_gemini_output_includes_thinking() -> None:
    """Gemini bills thinking as output, as every other provider reports it."""
    metrics = MindRoomGoogleGemini(id="gemini-3.8-flash")._get_metrics(
        GenerateContentResponseUsageMetadata(
            prompt_token_count=1000,
            cached_content_token_count=400,
            candidates_token_count=200,
            thoughts_token_count=1500,
            total_token_count=2700,
        ),
    )

    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (1000, 1700, 2700)
    assert (metrics.reasoning_tokens, metrics.cache_read_tokens) == (1500, 400)


def test_gemini_without_thinking_keeps_its_counts() -> None:
    """A Gemini reply without thinking keeps its prompt, answer, and total counts."""
    metrics = MindRoomGoogleGemini(id="gemini-3.8-flash")._get_metrics(
        GenerateContentResponseUsageMetadata(
            prompt_token_count=1000,
            candidates_token_count=200,
            total_token_count=1200,
        ),
    )

    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (1000, 200, 1200)
    assert (metrics.reasoning_tokens, metrics.cache_read_tokens) == (0, 0)


def test_groq_reports_cached_input_and_reasoning() -> None:
    """Groq's cached input and reasoning stay inside its prompt and completion totals."""
    metrics = MindRoomGroq(id="openai/gpt-oss-120b")._get_metrics(
        CompletionUsage(
            prompt_tokens=5000,
            completion_tokens=300,
            total_tokens=5300,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=4608),
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=120),
        ),
    )

    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (5000, 300, 5300)
    assert (metrics.cache_read_tokens, metrics.reasoning_tokens) == (4608, 120)


@pytest.mark.parametrize(
    ("usage_type", "details_type"),
    [
        (ChatCompletionResponseUsage, ChatCompletionResponseUsagePromptTokensDetails),
        (ChatChunkResponseUsage, ChatChunkResponseUsagePromptTokensDetails),
    ],
)
def test_cerebras_reports_cached_input(usage_type: type, details_type: type) -> None:
    """Cerebras's cached input stays inside its prompt total, in full and streamed responses."""
    metrics = MindRoomCerebras(id="gpt-oss-120b")._get_metrics(
        usage_type(
            prompt_tokens=5000,
            completion_tokens=300,
            total_tokens=5300,
            prompt_tokens_details=details_type(cached_tokens=4096),
        ),
    )

    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (5000, 300, 5300)
    assert metrics.cache_read_tokens == 4096
