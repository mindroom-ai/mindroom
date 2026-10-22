"""Parse a model's ``extra_kwargs`` into the settings dataclass of its provider adapter, and report its parameters.

Settings fields keep the Agno model field names that authored configs already use.
Keys an adapter does not accept fail with the accepted list instead of being passed through.
"""

from __future__ import annotations

import json
from dataclasses import fields
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from mindroom.redaction import redact_sensitive_data

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from _typeshed import DataclassInstance

_METADATA_KEY = "secret"
# Field metadata marking a setting that holds a credential, a client, or headers or a query string that can
# carry one, rather than a request parameter; model params never record it.
SECRET = MappingProxyType({_METADATA_KEY: True})


def _copied(value: Any) -> Any:  # noqa: ANN401 - arbitrary authored value
    """Copy authored dicts and lists so request building never writes into the config; other values pass through.

    Credentials, sessions, and transports authored programmatically are not copyable and are not mutated.
    """
    if isinstance(value, dict):
        return {key: _copied(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copied(item) for item in value]
    return value


def settings_from_extra_kwargs[T: DataclassInstance](
    settings_type: type[T],
    extra_kwargs: Mapping[str, Any] | None,
    *,
    provider: str,
    accepted: Iterable[str] | None = None,
) -> T:
    """Build ``settings_type`` from ``extra_kwargs``, rejecting keys it does not declare.

    ``accepted`` narrows the declared fields to the subset one route takes, for settings types that several
    routes share.
    """
    values = dict(extra_kwargs or {})
    declared = {field.name for field in fields(settings_type)}
    allowed = declared if accepted is None else declared & set(accepted)
    if unknown := sorted(values.keys() - allowed):
        msg = (
            f"Unsupported extra_kwargs for provider {provider!r}: {', '.join(unknown)}. "
            f"Accepted keys: {', '.join(sorted(allowed))}"
        )
        raise ValueError(msg)
    return settings_type(**{key: _copied(value) for key, value in values.items()})


def settings_params(settings: DataclassInstance) -> dict[str, Any]:
    """Return a route's JSON-safe, non-secret settings, as run metadata and the request log record them.

    Fields marked ``SECRET`` are skipped before their values are read, so credentials and SDK clients are never
    copied. The rest are masked as the request log masks its records, so neither records a credential nested in
    a raw request map such as ``request_params`` or ``extra_body``.
    """
    params: dict[str, Any] = {}
    for field in fields(settings):
        if field.metadata.get(_METADATA_KEY):
            continue
        value = getattr(settings, field.name)
        if value is None:
            continue
        try:
            json.dumps(value)
        except TypeError:
            continue
        params[field.name] = value
    return cast("dict[str, Any]", redact_sensitive_data(params))


__all__ = ["SECRET", "settings_from_extra_kwargs", "settings_params"]
