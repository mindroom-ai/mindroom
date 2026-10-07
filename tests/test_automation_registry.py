"""Automation registration: the decorator, discovery in plugin modules, and the compiled catalog."""

from __future__ import annotations

import types
from typing import TYPE_CHECKING

import pytest

from mindroom.automations import Ask, AutomationContext, automation
from mindroom.automations.registry import compile_automations, iter_module_automations
from mindroom.config.plugin import PluginEntryConfig

if TYPE_CHECKING:
    from collections.abc import Callable


class _Plugin:
    def __init__(self, name: str, *checks: object, settings: dict[str, object] | None = None) -> None:
        self.name = name
        self.entry_config = PluginEntryConfig(path=name, settings=settings or {})
        self.discovered_automations = checks


def _check() -> Callable[[AutomationContext], Ask | None]:
    """Return a fresh undecorated check, since the decorator marks the function it is given."""

    def check(ctx: AutomationContext) -> Ask | None:  # noqa: ARG001
        return None

    return check


def test_decorated_checks_are_discovered_once() -> None:
    """A check bound to two module names is one automation, and undecorated values are ignored."""
    check = automation("weekly_digest")(_check())
    module = types.ModuleType("plugin_hooks")
    module.check = check
    module.alias = check
    module.other = len

    assert iter_module_automations(module) == [check]


@pytest.mark.parametrize("name", ["Weekly", "1digest", "weekly-digest", ""])
def test_a_badly_named_automation_is_rejected(name: str) -> None:
    """Names are lowercase identifiers, so they read the same in config, logs, and hook sources."""
    with pytest.raises(ValueError, match="Automation name"):
        automation(name)


def test_an_async_check_is_rejected() -> None:
    """Checks already run off the event loop, so they are synchronous."""

    async def check(ctx: AutomationContext) -> Ask | None:  # noqa: ARG001
        return None

    with pytest.raises(TypeError, match="synchronous"):
        automation("weekly_digest")(check)


def test_plugin_automations_compile_with_their_plugin_settings() -> None:
    """The first plugin to register a name provides it; later ones and built-in names are collisions."""
    digest = automation("weekly_digest")(_check())
    shadow = automation("dreaming")(_check())

    catalog = compile_automations([_Plugin("a", digest, shadow, settings={"key": "value"}), _Plugin("b", digest)])

    definition = catalog.get("weekly_digest")
    assert definition is not None
    assert definition.plugin_name == "a"
    assert definition.settings == {"key": "value"}
    assert catalog.collisions == ("dreaming", "weekly_digest")


def test_built_ins_resolve_without_any_plugin() -> None:
    """The built-ins are always available, and an empty catalog loads them only when one is looked up."""
    catalog = compile_automations([])

    for name in ("prompt_curation", "dreaming"):
        definition = catalog.get(name)
        assert definition is not None
        assert definition.plugin_name is None
        assert definition.requires_file_memory
    assert catalog.get("weekly_digest") is None
