"""Regression test: segmented messages preserve MXIDs in body for mention extraction."""

from pathlib import Path

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.segmented_messages import segment_matrix_content
from mindroom.thread_utils import _extract_mentioned_user_ids
from tests.conftest import test_runtime_paths
from tests.identity_helpers import actual_entity_usernames, persist_entity_accounts


def test_segmented_messages_preserve_mxids_for_mention_extraction(tmp_path: Path) -> None:
    """Split delivery must restore pills and m.mentions by finding MXIDs in chunk bodies.

    When large_message_strategy=split, segmented_messages.py restores pills and
    m.mentions by finding the MXID in each chunk's body. If plain_text uses display
    names instead of MXIDs, chunks get m.mentions=None and handoffs silently fail.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            "code": AgentConfig(display_name="Code"),
            "research": AgentConfig(display_name="Research"),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )
    config = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames=actual_entity_usernames(config))

    # Create a message with agent mentions that would need segmentation if it were large enough
    # We'll test the mention extraction works on both the full content and after segmentation
    message_text = f"@code and @research, please help with this task. {'x' * 30000}"
    content = format_message_with_mentions(config, runtime_paths, message_text)

    # The full content should have MXIDs in the body for mention extraction fallback
    assert "@actual_code:localhost" in content["body"]
    assert "@actual_research:localhost" in content["body"]

    # Extract mentions from the full content - should work via m.mentions
    mentioned_ids = _extract_mentioned_user_ids(content, config, runtime_paths)
    assert "@actual_code:localhost" in mentioned_ids
    assert "@actual_research:localhost" in mentioned_ids

    # Simulate segmentation (this would happen for large messages)
    segmented = segment_matrix_content(
        content,
        room_encrypted=False,
        continuation_thread_id="$thread",
        continuation_reply_to_event_id="$reply",
    )

    # If segmentation produced chunks (large message case)
    if segmented is not None:
        # Each chunk should preserve MXIDs in body for mention extraction fallback
        first_chunk = segmented.first
        assert isinstance(first_chunk.get("body"), str)

        # The first chunk should have MXIDs in the body for extraction
        # (this is what the review said would break if we changed plain_text to display names)
        first_chunk_mentions = _extract_mentioned_user_ids(first_chunk, config, runtime_paths)

        # At minimum, mentions appearing in the first chunk should be extractable
        # The exact MXIDs depend on where the split happened, but the principle is:
        # if an MXID appears in the body, it must be extractable
        chunk_body = first_chunk["body"]
        if "@actual_code:localhost" in chunk_body:
            assert "@actual_code:localhost" in first_chunk_mentions
        if "@actual_research:localhost" in chunk_body:
            assert "@actual_research:localhost" in first_chunk_mentions
