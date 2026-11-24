"""Parse a model's ``extra_kwargs`` into the settings dataclass of its provider adapter, and report its parameters.

``sdk_request_body`` is the body an OpenAI or Anthropic SDK call sends for the arguments an adapter passes it.

Settings fields keep the Agno model field names that authored configs already use.
Keys an adapter does not accept fail with the accepted list instead of being passed through, at config load and
again when the model is built.
"""

from __future__ import annotations

import json
from dataclasses import fields
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from mindroom.config.legacy_fields import retired_model_setting
from mindroom.redaction import redact_sensitive_data

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

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


def settings_keys(settings_type: type[DataclassInstance], accepted: Iterable[str] | None = None) -> frozenset[str]:
    """Return the keys ``settings_type`` declares, narrowed to ``accepted`` for a type that several routes share."""
    declared = frozenset(field.name for field in fields(settings_type))
    return declared if accepted is None else declared & frozenset(accepted)


def reject_unsupported_extra_kwargs(
    extra_kwargs: Mapping[str, Any] | None,
    allowed: Collection[str],
    *,
    provider: str,
) -> None:
    """Raise ``ValueError`` naming the ``extra_kwargs`` keys outside ``allowed`` and listing the accepted ones.

    A retired setting gets the reason it went instead of the accepted list.
    """
    unknown = sorted((extra_kwargs or {}).keys() - set(allowed))
    if (retired := retired_model_setting(unknown)) is not None:
        key, guidance = retired
        msg = f"Unsupported extra_kwargs for provider {provider!r}: {key} was removed; {guidance}."
        raise ValueError(msg)
    if unknown:
        msg = (
            f"Unsupported extra_kwargs for provider {provider!r}: {', '.join(unknown)}; remove or correct them. "
            f"Accepted keys: {', '.join(sorted(allowed))}"
        )
        raise ValueError(msg)


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
    reject_unsupported_extra_kwargs(values, settings_keys(settings_type, accepted), provider=provider)
    return settings_type(**{key: _copied(value) for key, value in values.items()})


def settings_params(settings: DataclassInstance) -> dict[str, Any]:
    """Return a route's JSON-safe, non-secret settings, as the request log records them.

    Fields marked ``SECRET`` are skipped before their values are read, so credentials and SDK clients are never
    copied. The rest are masked as the request log masks its records, so the log never records a credential nested in
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


def sdk_request_body(kwargs: Mapping[str, Any], option_keys: Collection[str]) -> dict[str, Any]:
    """Return the JSON body an OpenAI or Anthropic SDK call sends for ``kwargs``.

    Both SDKs take request options, named by ``option_keys`` (such as headers, query parameters, and the timeout),
    beside the body fields, and merge ``extra_body`` over those fields.
    """
    body = {key: value for key, value in kwargs.items() if key not in option_keys}
    return {**body, **(kwargs.get("extra_body") or {})}


__all__ = [
    "SECRET",
    "reject_unsupported_extra_kwargs",
    "sdk_request_body",
    "settings_from_extra_kwargs",
    "settings_keys",
    "settings_params",
]
