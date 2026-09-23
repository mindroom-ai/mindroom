"""JSON-schema annotations that let the dashboard render config fields."""

from typing import Literal, get_args

from pydantic.json_schema import JsonDict

type ReferenceKind = Literal["model", "agent", "room", "tool"]
REFERENCE_KINDS = frozenset(get_args(ReferenceKind.__value__))
HINT_KEY = "x-mindroom"


def dashboard_hint(
    *,
    reference: ReferenceKind | None = None,
    key_reference: ReferenceKind | None = None,
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
    return {HINT_KEY: hint}
