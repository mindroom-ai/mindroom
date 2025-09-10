"""Common error handling utilities."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from backend.config import logger
from fastapi import HTTPException


def handle_errors(operation: str | None = None) -> Callable:
    """Decorator to handle common error patterns."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # noqa: ANN002, ANN003, ANN401
            op_name = operation or func.__name__.replace("_", " ")
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                # Re-raise HTTP exceptions as-is
                raise
            except Exception as e:
                logger.exception("Error in %s", op_name)
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to {op_name}: {e!s}",
                ) from e

        return wrapper

    return decorator


def require_authorization(api_key: str) -> Callable:
    """Decorator to require API key authorization."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # noqa: ANN002, ANN003, ANN401
            # Find authorization parameter in kwargs or args
            auth = kwargs.get("authorization")
            if not auth:
                # Try to find it in args (positional)
                for arg in args:
                    if isinstance(arg, str) and arg.startswith("Bearer "):
                        auth = arg
                        break

            if auth != f"Bearer {api_key}":
                raise HTTPException(status_code=401, detail="Unauthorized")

            return await func(*args, **kwargs)

        return wrapper

    return decorator
