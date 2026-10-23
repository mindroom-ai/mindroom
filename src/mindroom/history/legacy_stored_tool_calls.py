"""Stored tool-call shapes earlier releases wrote, read in the current shape wherever stored runs feed a model.

The repair returns a new list and replaces, never edits, a message it changes, so the rows a request is built from are
never rewritten. Replayed history is kept out of storage by its ``from_history`` mark, but a resumed run stores the
request it sent, so a legacy paused run is stored repaired, in the shape current releases store.

``with_tool_calls`` is the one rule for narrowing an assistant message's calls, which the repair and the tool-call
window of ``request_messages`` share.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["repair_legacy_tool_calls", "with_tool_calls"]


# LEGACY_COMPAT: Persisted Anthropic tool calls without empty arguments.
# Legacy format: Agno writers omitted the arguments field for empty Anthropic tool calls.
# Last legacy release: v2026.9.43; replacement: v2026.9.44 included the upstream Agno 3.0.7 writer fix.
# Handling: Supply `{}` arguments in the request copies of affected calls, for every route, where stored history, a
# compaction's input, and a paused run become messages, until those histories are migrated or dropped. A resumed run
# stores its request, so a legacy paused run is stored with the arguments supplied.
# Coverage: tests/test_history_legacy_tool_calls.py::test_a_stored_call_without_arguments_replays_with_empty_arguments,
# tests/test_history_legacy_tool_calls.py::test_compaction_reads_stored_calls_as_a_request_replays_them,
# tests/test_history_legacy_tool_calls.py::test_a_resumed_legacy_run_is_stored_in_the_current_shape,
# tests/providers/test_provider_families.py::test_every_family_replays_the_tool_calls_any_provider_stored


# LEGACY_COMPAT: Persisted sparse OpenAI tool placeholders and orphan results.
# Legacy format: Streamed OpenAI history retained id-only tool placeholders and their orphan tool results.
# Last legacy release: v2026.7.172; replacement: v2026.7.173 filtered sparse placeholders during writes.
# Handling: Remove each placeholder and its matching result, for every route, where stored history, a compaction's
# input, and a paused run become messages, before the message and tool-call windows count them, preserving unchanged
# message identities. An assistant message left with no call and no content is dropped, and one that keeps its
# content holds no calls. A resumed run stores its request, so a legacy paused run is stored without its
# placeholders and their results, which no request replays.
# Coverage: tests/test_history_legacy_tool_calls.py::test_placeholders_and_their_results_never_replay,
# tests/test_history_legacy_tool_calls.py::test_a_message_left_without_calls_or_content_takes_no_message_slot,
# tests/test_history_legacy_tool_calls.py::test_a_message_left_with_content_but_no_calls_holds_none,
# tests/test_history_legacy_tool_calls.py::test_the_message_window_counts_only_messages_that_replay,
# tests/test_history_legacy_tool_calls.py::test_the_tool_call_window_counts_only_calls_that_replay,
# tests/test_history_legacy_tool_calls.py::test_the_summary_input_leaves_out_placeholders_and_their_results,
# tests/test_history_legacy_tool_calls.py::test_a_paused_run_resumes_without_its_placeholders,
# tests/test_history_legacy_tool_calls.py::test_a_resumed_legacy_run_is_stored_in_the_current_shape,
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


def with_tool_calls(message: Message, calls: list[dict[str, Any]]) -> Message | None:
    """Return a copy of an assistant ``message`` holding only ``calls``, or None when it then has no call and no content.

    A message left with nothing would replay as an empty assistant turn that takes a message slot, so every step that
    narrows a message's calls, the repair here and the tool-call window, drops it instead. A message that keeps its
    content ends without calls as None, the shape of a message that never had any.
    """
    if not calls and not message.content:
        return None
    return replace(message, tool_calls=calls or None)
