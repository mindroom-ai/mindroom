"""JSON-schema annotations that let the dashboard render config fields."""

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
    clears_inherited: bool = False,
) -> JsonDict:
    """Return a ``json_schema_extra`` mapping for one config field.

    ``reference`` names the configured entity a string (or each list item or
    mapping value) refers to, and ``key_reference`` does the same for mapping
    keys. ``clears_inherited`` marks nullable fields where an authored null
    removes a value inherited from defaults instead of meaning "not set".
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
    if clears_inherited:
        hint["clears_inherited"] = True
    return {_HINT_KEY: hint}


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
