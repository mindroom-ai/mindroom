"""Task-local execution policy for provider-managed tools."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_tools_disabled: ContextVar[bool] = ContextVar("provider_tools_disabled", default=False)


def provider_tools_disabled() -> bool:
    """Whether the current provider request must prevent tool execution."""
    return _tools_disabled.get()


@contextmanager
def without_provider_tools() -> Iterator[None]:
    """Disable provider-managed execution within this asynchronous task."""
    token = _tools_disabled.set(True)
    try:
        yield
    finally:
        _tools_disabled.reset(token)
