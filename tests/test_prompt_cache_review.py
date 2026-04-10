"""Request-shape tests for the Vertex Claude prompt-cache review harness."""

from __future__ import annotations

from scripts.testing.prompt_cache_review import prompt_cache_harness, summarize_block_prefix


def _memory_markdown() -> str:
    return (
        "# Memory\n\n"
        "Stable workspace context.\n"
        "- [id=python] Python backend uses FastAPI and pydantic.\n"
        "- [id=javascript] JavaScript frontend uses Next.js and React.\n"
    )


def test_current_request_places_entrypoint_memories_and_prompt_in_one_user_block() -> None:
    with prompt_cache_harness() as harness:
        harness.write_memory_markdown(_memory_markdown())

        current = harness.current_request("How does the Python backend work?")

        assert len(current.payload["system"]) == 1
        assert "[File memory entrypoint (agent)]" not in current.payload["system"][0]["text"]
        assert len(current.user_blocks) == 1
        assert "[File memory entrypoint (agent)]" in current.first_user_text
        assert "[Automatically extracted agent file memories" in current.first_user_text
        assert current.prompt in current.first_user_text


def test_identical_prompt_keeps_first_user_block_stable() -> None:
    with prompt_cache_harness() as harness:
        harness.write_memory_markdown(_memory_markdown())

        first = harness.current_request("How does the Python backend work?")
        second = harness.current_request("How does the Python backend work?")
        summary = summarize_block_prefix(first, second)

        assert summary.stable_system_blocks == 1
        assert summary.stable_message_blocks == 1
        assert summary.first_diverging_message_index is None
        assert summary.first_diverging_content_index is None


def test_changed_prompt_invalidates_the_whole_fused_first_user_block() -> None:
    with prompt_cache_harness() as harness:
        harness.write_memory_markdown(_memory_markdown())

        first = harness.current_request("Explain the Python backend.")
        second = harness.current_request("Document the Python backend.")
        summary = summarize_block_prefix(first, second)

        assert "Python backend uses FastAPI and pydantic." in first.first_user_text
        assert "Python backend uses FastAPI and pydantic." in second.first_user_text
        assert summary.stable_system_blocks == 1
        assert summary.stable_message_blocks == 0
        assert summary.first_diverging_message_index == 0
        assert summary.first_diverging_content_index == 0


def test_different_queries_select_different_file_memories() -> None:
    with prompt_cache_harness() as harness:
        harness.write_memory_markdown(_memory_markdown())

        python_request = harness.current_request("Explain the Python backend.")
        javascript_request = harness.current_request("Explain the JavaScript frontend.")

        python_extracted = python_request.first_user_text.split(
            "Previous agent file memories that might be related:\n",
            maxsplit=1,
        )[1]
        javascript_extracted = javascript_request.first_user_text.split(
            "Previous agent file memories that might be related:\n",
            maxsplit=1,
        )[1]

        assert "- Python backend uses FastAPI and pydantic." in python_extracted
        assert "- JavaScript frontend uses Next.js and React." not in python_extracted
        assert "- JavaScript frontend uses Next.js and React." in javascript_extracted
        assert "- Python backend uses FastAPI and pydantic." not in javascript_extracted


def test_split_entrypoint_variant_moves_more_stable_text_into_system_blocks() -> None:
    with prompt_cache_harness() as harness:
        harness.write_memory_markdown(_memory_markdown())

        current_first = harness.current_request("Explain the Python backend.")
        current_second = harness.current_request("Explain the JavaScript frontend.")
        split_first = harness.split_entrypoint_request("Explain the Python backend.")
        split_second = harness.split_entrypoint_request("Explain the JavaScript frontend.")

        current_summary = summarize_block_prefix(current_first, current_second)
        split_summary = summarize_block_prefix(split_first, split_second)

        assert current_summary.stable_system_blocks == 1
        assert current_summary.stable_message_blocks == 0
        assert split_summary.stable_system_blocks == 1
        assert split_summary.stable_message_blocks == 0
        assert split_summary.stable_system_text_chars > current_summary.stable_system_text_chars
