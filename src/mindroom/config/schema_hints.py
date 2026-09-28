"""JSON-schema annotations that let the dashboard render config fields and let displays mask secrets."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic.json_schema import GenerateJsonSchema, JsonDict
from pydantic_core import core_schema

if TYPE_CHECKING:
    from collections.abc import Callable

# The dashboard's configSchema.ts mirrors these kinds and the hint key.
type _ReferenceKind = Literal["model", "agent", "room", "tool"]
_HINT_KEY = "x-mindroom"


def dashboard_hint(
    *,
    reference: _ReferenceKind | None = None,
    key_reference: _ReferenceKind | None = None,
    secret: bool = False,
    multiline: bool = False,
) -> JsonDict:
    """Return a ``json_schema_extra`` mapping for one config field.

    ``reference`` names the configured entity a string (or each list item or
    mapping value) refers to, and ``key_reference`` does the same for mapping
    keys.
    """
    hint: JsonDict = {}
    if reference is not None:
        hint["reference"] = reference
    if key_reference is not None:
        hint["key_reference"] = key_reference
    if secret:
        hint["secret"] = True
    if multiline:
        hint["multiline"] = True
    return {_HINT_KEY: hint}


def _is_secret_schema(schema: Mapping[str, Any]) -> bool:
    hint = schema.get(_HINT_KEY)
    return isinstance(hint, dict) and hint.get("secret") is True


def _mask_secret_value(value: object, replacement: str) -> object:
    """Replace every value inside one secret subtree, keeping mapping keys and list shape."""
    if isinstance(value, Mapping):
        return {key: _mask_secret_value(item, replacement) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_secret_value(item, replacement) for item in value]
    return None if value is None else replacement


def _redact_secret_hints(
    value: object,
    schema: Mapping[str, Any],
    defs: Mapping[str, Mapping[str, Any]],
    replacement: str,
) -> object:
    if _is_secret_schema(schema):
        return _mask_secret_value(value, replacement)
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _redact_secret_hints(value, defs[ref.removeprefix("#/$defs/")], defs, replacement)
    # A value matches one union member, but checking every member keeps a secret field of any of them masked.
    for keyword in ("anyOf", "oneOf", "allOf"):
        for member in schema.get(keyword, ()):
            value = _redact_secret_hints(value, member, defs, replacement)
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        redacted: dict[object, object] = {}
        for key, item in value.items():
            item_schema = properties.get(key, additional)
            redacted[key] = (
                _redact_secret_hints(item, item_schema, defs, replacement) if isinstance(item_schema, Mapping) else item
            )
        return redacted
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, Mapping):
        return [_redact_secret_hints(item, items, defs, replacement) for item in value]
    return value


def redact_secret_hinted_values(value: object, schema: Mapping[str, Any], *, replacement: str) -> object:
    """Mask every value whose schema field carries ``dashboard_hint(secret=True)``.

    ``schema`` is a root JSON schema with its ``$defs``, such as the dashboard
    config schema, and ``value`` is data it describes. Secret subtrees keep their
    mapping keys and list shape so readers still see which entries exist, the
    same way the dashboard masks them.
    """
    return _redact_secret_hints(value, schema, schema.get("$defs", {}), replacement)


class DashboardJsonSchema(GenerateJsonSchema):
    """JSON schema generator that also reports default-factory values.

    Dashboard forms show effective defaults, and most config collections and
    nested blocks declare theirs through ``default_factory``.
    """

    def get_default_value(self, schema: core_schema.WithDefaultSchema) -> Any:  # noqa: ANN401
        """Call argument-free default factories; data-dependent ones stay unreported."""
        factory = schema.get("default_factory")
        if factory is None or schema.get("default_factory_takes_data"):
            return super().get_default_value(schema)
        return cast("Callable[[], object]", factory)()
