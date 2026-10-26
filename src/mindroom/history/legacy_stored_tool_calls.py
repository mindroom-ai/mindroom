"""Tool-call shapes earlier releases stored, which the conversion to storage v2 stores in the current shape.

Every request, compaction input, resume, and background reader then reads the calls as current releases store them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.history.message_content import with_tool_calls

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["repair_legacy_tool_calls"]


# LEGACY_COMPAT: Persisted Anthropic tool calls without empty arguments.
# Legacy format: Agno writers omitted the arguments field for empty Anthropic tool calls.
# Last legacy release: v2026.9.43; replacement: v2026.9.44 included the upstream Agno 3.0.7 writer fix.
# Handling: The conversion to storage v2 supplies `{}` arguments to affected calls in every run it writes, so replay
# on every route, a compaction's input, a paused run's resume, and the background readers read them as current
# releases store them. A folded message the run's row holds is stored repaired; one the row does not hold, one the
# repair removed included, is stored as the fold took it. Folded messages never replay, and fold segments, first
# written by v2026.10.341, postdate this shape.
# Coverage: tests/test_history_legacy_tool_calls.py::test_a_stored_call_without_arguments_replays_with_empty_arguments,
# tests/test_history_legacy_tool_calls.py::test_compaction_reads_stored_calls_as_a_request_replays_them,
# tests/test_history_legacy_tool_calls.py::test_a_converted_paused_run_resumes_and_is_stored_in_the_current_shape,
# tests/test_history_legacy_tool_calls.py::test_a_turns_transcript_reads_converted_calls_as_replay_does,
# tests/session_storage/test_legacy_agno3_conversion.py::test_a_folded_message_the_run_row_holds_is_stored_repaired,
# tests/providers/test_provider_families.py::test_every_family_replays_the_tool_calls_any_provider_stored


# LEGACY_COMPAT: Persisted sparse OpenAI tool placeholders and orphan results.
# Legacy format: Streamed OpenAI history retained id-only tool placeholders and their orphan tool results.
# Last legacy release: v2026.7.172; replacement: v2026.7.173 filtered sparse placeholders during writes.
# Handling: The conversion to storage v2 removes each placeholder and its matching result from every run it writes,
# keeping the identities of the messages it leaves unchanged, so replay on every route, a compaction's input, a paused
# run's resume, the message and tool-call windows, and the background readers never see them. An assistant message
# left with no call and no content is dropped, and one that keeps its content holds no calls. A folded message the
# run's row holds is stored repaired; one the row does not hold, one the repair removed included, is stored as the
# fold took it. Folded messages never replay, and fold segments, first written by v2026.10.341, postdate this shape.
# Coverage: tests/test_history_legacy_tool_calls.py::test_placeholders_and_their_results_never_replay,
# tests/test_history_legacy_tool_calls.py::test_a_message_left_without_calls_or_content_takes_no_message_slot,
# tests/test_history_legacy_tool_calls.py::test_a_message_left_with_content_but_no_calls_holds_none,
# tests/test_history_legacy_tool_calls.py::test_the_message_window_counts_only_messages_that_replay,
# tests/test_history_legacy_tool_calls.py::test_the_tool_call_window_counts_only_calls_that_replay,
# tests/test_history_legacy_tool_calls.py::test_the_summary_input_leaves_out_placeholders_and_their_results,
# tests/test_history_legacy_tool_calls.py::test_a_paused_run_resumes_without_its_placeholders,
# tests/test_history_legacy_tool_calls.py::test_a_converted_paused_run_resumes_and_is_stored_in_the_current_shape,
# tests/test_history_legacy_tool_calls.py::test_a_turns_transcript_reads_converted_calls_as_replay_does,
# tests/test_history_legacy_tool_calls.py::test_a_paused_team_runs_embedded_members_are_repaired,
# tests/session_storage/test_legacy_agno3_conversion.py::test_converted_call_ids_are_text_or_none_so_folded_calls_read,
# tests/session_storage/test_legacy_agno3_conversion.py::test_a_folded_message_the_run_row_holds_is_stored_repaired,
# tests/providers/test_provider_families.py::test_every_family_replays_the_tool_calls_any_provider_stored
def repair_legacy_tool_calls(messages: Sequence[Message]) -> list[Message]:
    """Supply missing call arguments, and drop id-only placeholder calls with their results and the messages they empty."""
    repaired: list[Message] = []
    removed_ids: set[str] = set()
    for message in messages:
        if message.role == "tool" and message.tool_call_id in removed_ids:
            continue
        if message.role != "assistant" or not message.tool_calls:
            repaired.append(message)
            continue
        calls: list[dict[str, Any]] = []
        changed = False
        for call in message.tool_calls:
            function = call.get("function")
            if not isinstance(function, dict):
                if isinstance(call_id := call.get("id"), str):
                    removed_ids.add(call_id)
                changed = True
            elif "arguments" not in function:
                calls.append({**call, "function": {**function, "arguments": "{}"}})
                changed = True
            else:
                calls.append(call)
        if not changed:
            repaired.append(message)
        elif (narrowed := with_tool_calls(message, calls)) is not None:
            repaired.append(narrowed)
    return repaired
