"""Automation definitions: the `@automation` decorator, discovery in plugin modules, and the compiled catalog."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from types import ModuleType

    from mindroom.automations.steps import Ask, AutomationContext
    from mindroom.config.plugin import PluginEntryConfig

    type CheckFn = Callable[[AutomationContext], Ask | None]

_BUILTIN_AUTOMATION_NAMES = frozenset({"prompt_curation", "dreaming"})
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_METADATA_ATTR = "__mindroom_automation__"


@dataclass(frozen=True)
class _AutomationMetadata:
    name: str
    requires_file_memory: bool


def automation(name: str, *, requires_file_memory: bool = False) -> Callable[[CheckFn], CheckFn]:
    """Mark a synchronous check as the automation ``name``, which agents enable under ``automations:``."""
    if not _NAME.fullmatch(name):
        msg = f"Automation name must match [a-z][a-z0-9_]*: {name!r}"
        raise ValueError(msg)

    def decorator(check: CheckFn) -> CheckFn:
        if inspect.iscoroutinefunction(check):
            msg = f"Automation check {name!r} must be synchronous; it already runs off the event loop"
            raise TypeError(msg)
        setattr(check, _METADATA_ATTR, _AutomationMetadata(name, requires_file_memory))
        return check

    return decorator


def _metadata(check: object) -> _AutomationMetadata | None:
    metadata = getattr(check, _METADATA_ATTR, None)
    return metadata if isinstance(metadata, _AutomationMetadata) else None


def _automation_name(check: object) -> str | None:
    """Return the automation name a check was registered under, or None for an undecorated value."""
    metadata = _metadata(check)
    return metadata.name if metadata is not None else None


def iter_module_automations(module: ModuleType) -> list[CheckFn]:
    """Return every decorated check defined on one module, once each."""
    found: dict[int, CheckFn] = {}
    for value in vars(module).values():
        if _metadata(value) is not None:
            found.setdefault(id(value), value)
    return list(found.values())


class _AutomationPlugin(Protocol):
    """The loaded-plugin fields compilation reads."""

    name: str
    entry_config: PluginEntryConfig
    discovered_automations: tuple[CheckFn, ...]


@dataclass(frozen=True)
class AutomationDefinition:
    """One registered automation: its check, its needs, and the plugin it came from."""

    name: str
    check: CheckFn
    requires_file_memory: bool
    plugin_name: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)


def _definition(check: CheckFn, plugin_name: str | None, settings: Mapping[str, Any]) -> AutomationDefinition:
    metadata = _metadata(check)
    assert metadata is not None
    return AutomationDefinition(metadata.name, check, metadata.requires_file_memory, plugin_name, dict(settings))


@cache
def _builtin_definitions() -> dict[str, AutomationDefinition]:
    # why-lazy: the built-ins import memory and runtime modules, and plugin snapshots are built at import time.
    from mindroom.automations.dreaming import check_dreaming  # noqa: PLC0415
    from mindroom.automations.prompt_curation import check_curation  # noqa: PLC0415

    definitions = [_definition(check, None, {}) for check in (check_curation, check_dreaming)]
    return {definition.name: definition for definition in definitions}


@dataclass(frozen=True)
class AutomationCatalog:
    """The automations loaded plugins provide, beside the built-ins, and the names registered more than once."""

    plugin_definitions: Mapping[str, AutomationDefinition] = field(default_factory=dict)
    collisions: tuple[str, ...] = ()

    def get(self, name: str) -> AutomationDefinition | None:
        """Return the definition for ``name``, or None when nothing provides it."""
        if name in _BUILTIN_AUTOMATION_NAMES:
            return _builtin_definitions()[name]
        return self.plugin_definitions.get(name)


def compile_automations(plugins: Iterable[_AutomationPlugin]) -> AutomationCatalog:
    """Compile every plugin automation; the first registration of a name wins and built-in names are reserved."""
    definitions: dict[str, AutomationDefinition] = {}
    collisions: set[str] = set()
    for plugin in plugins:
        for check in plugin.discovered_automations:
            name = _automation_name(check)
            if name is None:
                continue
            if name in _BUILTIN_AUTOMATION_NAMES or name in definitions:
                collisions.add(name)
                continue
            definitions[name] = _definition(check, plugin.name, plugin.entry_config.settings)
    return AutomationCatalog(definitions, tuple(sorted(collisions)))
