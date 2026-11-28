"""Built-in automation definitions, resolved beside a plugin snapshot's ``AutomationCatalog``."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.automations.dreaming import check_dreaming
from mindroom.automations.exploration import check_exploration
from mindroom.automations.prompt_curation import check_curation
from mindroom.automations.registry import automation_definition

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.automations.registry import AutomationCatalog, AutomationDefinition

BUILTIN_AUTOMATION_DEFINITIONS: Mapping[str, AutomationDefinition] = MappingProxyType(
    {
        definition.name: definition
        for definition in (
            automation_definition(check_curation),
            automation_definition(check_dreaming),
            automation_definition(check_exploration),
        )
    },
)


def resolve_automation_definition(name: str, catalog: AutomationCatalog) -> AutomationDefinition | None:
    """Return the built-in definition for ``name``, else the plugin definition in ``catalog``."""
    if (builtin := BUILTIN_AUTOMATION_DEFINITIONS.get(name)) is not None:
        return builtin
    return catalog.get(name)


__all__ = ["BUILTIN_AUTOMATION_DEFINITIONS", "resolve_automation_definition"]
