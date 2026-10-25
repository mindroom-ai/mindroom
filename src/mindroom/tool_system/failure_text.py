"""The text a caught exception reports, shared by every writer that turns one into text a reader sees.

Its writers are the generic catches of failed tool calls, tool jobs, subagents, workflows, scheduled runs, and
replies, whose text a model or user reads or a stored outcome holds.
"""


def failure_text(error: BaseException) -> str:
    """Return what a reader sees of a failure: the exception's text, or its type name when it has none.

    Exceptions such as ``TimeoutError`` and ``httpx.ReadTimeout`` have no text, and a failure without text tells the
    model nothing about what failed.
    """
    return str(error) or type(error).__name__
