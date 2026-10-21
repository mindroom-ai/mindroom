"""Parse a model's ``extra_kwargs`` into the settings dataclass of its provider adapter.

Settings fields keep the Agno model field names that authored configs already use.
Keys an adapter does not accept fail with the accepted list instead of being passed through.
"""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from _typeshed import DataclassInstance


def settings_from_extra_kwargs[T: DataclassInstance](
    settings_type: type[T],
    extra_kwargs: Mapping[str, Any] | None,
    *,
    provider: str,
) -> T:
    """Build ``settings_type`` from ``extra_kwargs``, rejecting keys it does not declare."""
    values = dict(extra_kwargs or {})
    accepted = sorted(field.name for field in fields(settings_type))
    unknown = sorted(values.keys() - set(accepted))
    if unknown:
        msg = (
            f"Unsupported extra_kwargs for provider '{provider}': {', '.join(unknown)}. "
            f"Accepted keys: {', '.join(accepted)}"
        )
        raise ValueError(msg)
    return settings_type(**values)


__all__ = ["settings_from_extra_kwargs"]
