"""The text a failure reports, shared by every writer of a failed tool call, tool job, or subagent."""


def failure_text(error: BaseException) -> str:
    """Return what the model reads of a failure: the exception's text, or its type name when it has none.

    Exceptions such as ``TimeoutError`` and ``httpx.ReadTimeout`` have no text, and a failure without text tells the
    model nothing about what failed.
    """
    return str(error) or type(error).__name__
