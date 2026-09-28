"""JSON-schema annotations that let the dashboard render config fields and let displays mask secrets."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic.json_schema import GenerateJsonSchema, JsonDict
from pydantic_core import core_schema

from mindroom.redaction import REDACTED, redact_sensitive_data, redact_sensitive_text

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


def _mask_secret_value(value: object) -> object:
    """Replace every value inside one secret subtree, keeping mapping keys and list shape."""
    if isinstance(value, Mapping):
        return {key: _mask_secret_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_secret_value(item) for item in value]
    return None if value is None else REDACTED


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (Mapping,),
    "array": (list,),
    "string": (str,),
    "null": (type(None),),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


def _resolve_ref(schema: Mapping[str, Any], defs: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    ref = schema.get("$ref")
    return defs[ref.removeprefix("#/$defs/")] if isinstance(ref, str) else schema


def _accepts_json_type(schema: Mapping[str, Any], value: object, defs: Mapping[str, Mapping[str, Any]]) -> bool:
    json_type = _resolve_ref(schema, defs).get("type")
    return not isinstance(json_type, str) or isinstance(value, _JSON_TYPES.get(json_type, (object,)))


def _union_members_for(
    value: object,
    schema: Mapping[str, Any],
    defs: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]] | None:
    """Return the composed members that describe ``value``, or None when ``schema`` composes none."""
    if isinstance(schema.get("allOf"), list):
        return schema["allOf"]
    members = schema.get("anyOf", schema.get("oneOf"))
    if not isinstance(members, list):
        return None
    discriminator = schema.get("discriminator")
    if isinstance(discriminator, Mapping) and isinstance(value, Mapping):
        mapping = discriminator.get("mapping")
        tag = value.get(discriminator.get("propertyName"))
        if isinstance(mapping, Mapping) and isinstance(tag, str) and isinstance(mapping.get(tag), str):
            return [{"$ref": mapping[tag]}]
    # Without a usable tag every member of the value's JSON type applies, so any member's secret stays masked.
    return [member for member in members if _accepts_json_type(member, value, defs)]


def _describes_model(schema: Mapping[str, Any], defs: Mapping[str, Mapping[str, Any]]) -> bool:
    resolved = _resolve_ref(schema, defs)
    members = resolved.get("anyOf", resolved.get("oneOf", ()))
    return "properties" in resolved or any(_describes_model(member, defs) for member in members)


def _redact_mapping_for_display(
    value: Mapping[Any, object],
    schema: Mapping[str, Any],
    defs: Mapping[str, Mapping[str, Any]],
) -> dict[object, object]:
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties")
    entry_schema = additional if isinstance(additional, Mapping) else {}
    redacted: dict[object, object] = {}
    for key, item in value.items():
        field_schema = properties.get(key)
        if isinstance(field_schema, Mapping):
            # A declared field is secret only when its schema says so.
            redacted[key] = _redact_for_display(item, field_schema, defs)
        elif _describes_model(entry_schema, defs):
            # Keys of entity maps such as agents or MCP servers are identifiers, not credential names.
            redacted[key] = _redact_for_display(item, entry_schema, defs)
        else:
            # In a free-form map, the key name is the only hint that an entry holds a credential.
            entry = redact_sensitive_data({key: _redact_for_display(item, entry_schema, defs)})
            redacted.update(cast("dict[object, object]", entry))
    return redacted


def _redact_for_display(value: object, schema: Mapping[str, Any], defs: Mapping[str, Mapping[str, Any]]) -> object:
    if _is_secret_schema(schema):
        return _mask_secret_value(value)
    if isinstance(schema.get("$ref"), str):
        return _redact_for_display(value, _resolve_ref(schema, defs), defs)
    members = _union_members_for(value, schema, defs)
    if members is None:
        return _redact_value_for_display(value, schema, defs)
    if not members:
        return redact_sensitive_data(value)
    for member in members:
        value = _redact_for_display(value, member, defs)
    return value


def _redact_value_for_display(
    value: object,
    schema: Mapping[str, Any],
    defs: Mapping[str, Mapping[str, Any]],
) -> object:
    if isinstance(value, Mapping):
        return _redact_mapping_for_display(value, schema, defs)
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping) and items:
            return [_redact_for_display(item, items, defs) for item in value]
        return redact_sensitive_data(value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def redact_config_for_display(value: object, schema: Mapping[str, Any]) -> object:
    """Redact config data described by ``schema`` before showing it to a requester or model.

    ``schema`` is a root JSON schema with its ``$defs``, such as the dashboard
    config schema. The schema decides for typed fields: every value inside a
    field carrying ``dashboard_hint(secret=True)`` is masked, keeping mapping
    keys and list shape as the dashboard does, and other typed fields keep
    their values. Entries of free-form maps, such as tool overrides or extra
    OAuth parameters, are also masked by credential-like key names, and every
    remaining string still has credential patterns such as URL passwords and
    bearer tokens masked.
    """
    return _redact_for_display(value, schema, schema.get("$defs", {}))


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
