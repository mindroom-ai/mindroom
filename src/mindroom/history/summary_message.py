"""The compaction summary as a user message: what it says, the kind of summary it is, and where replay places it."""

from __future__ import annotations

from mindroom.llm.messages import Message

_COMPACTION_SUMMARY_MARKER = "mindroom_compaction_summary"
_SCOPE_SUMMARY = "scope"
_RUN_LOCAL_SUMMARY = "run"
_SUMMARY_FOLLOWS_KEY = "mindroom_compaction_summary_follows"


def compaction_summary_message(summary: str, *, run_local: bool) -> Message:
    """Render one compaction summary as the user message that replaces the history it covers.

    The scope's summary is rebuilt from its latest summary row for every request, so it is history; a run-local summary is
    stored with its run. The marker keeps that kind when a later request replays the run and marks its messages history.
    """
    return Message(
        role="user",
        content=(
            "The earlier part of this conversation was compacted into this summary:\n\n"
            f"<compacted_history>\n{summary}\n</compacted_history>\n\n"
            "The conversation continues after this summary. If it records progress on the current request, "
            "continue that work from its next steps instead of starting over."
        ),
        provider_data={_COMPACTION_SUMMARY_MARKER: _RUN_LOCAL_SUMMARY if run_local else _SCOPE_SUMMARY},
        from_history=not run_local,
    )


def compaction_summary_text(message: Message) -> str:
    """Return the summary a rendered compaction summary message carries."""
    content = str(message.content)
    start = content.index("<compacted_history>\n") + len("<compacted_history>\n")
    return content[start : content.rindex("\n</compacted_history>")]


def is_compaction_summary(message: Message) -> bool:
    """Return whether one message is a rendered compaction summary."""
    return _summary_kind(message) in {_SCOPE_SUMMARY, _RUN_LOCAL_SUMMARY}


def is_scope_summary(message: Message) -> bool:
    """Return whether one message is the scope's summary, as opposed to one run's own."""
    return _summary_kind(message) == _SCOPE_SUMMARY


def mark_summary_follows(prompt: Message) -> None:
    """Record that the scope's summary replays right after this prompt, where a mid-turn compaction placed it."""
    prompt.provider_data = {**(prompt.provider_data or {}), _SUMMARY_FOLLOWS_KEY: True}


def summary_follows(message: Message) -> bool:
    """Return whether the scope's summary replays right after this message."""
    return isinstance(message.provider_data, dict) and message.provider_data.get(_SUMMARY_FOLLOWS_KEY) is True


def _summary_kind(message: Message) -> object:
    return message.provider_data.get(_COMPACTION_SUMMARY_MARKER) if isinstance(message.provider_data, dict) else None
