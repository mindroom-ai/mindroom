"""The text a caught exception reports: its text, or its type name when it has none."""


def failure_text(error: BaseException) -> str:
    """Return what a reader sees of a failure: the exception's text, or its type name when it has none.

    Exceptions such as ``TimeoutError`` and ``httpx.ReadTimeout`` have no text, and a failure without text tells the
    model nothing about what failed.
    """
    return str(error) or type(error).__name__
