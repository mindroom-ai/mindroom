"""Automation registration: the decorator, discovery in plugin modules, and the compiled catalog."""

from __future__ import annotations

import subprocess
import sys
import types
from typing import TYPE_CHECKING

import pytest

from mindroom.automations import Ask, AutomationContext, automation
from mindroom.automations.builtins import BUILTIN_AUTOMATION_DEFINITIONS, resolve_automation_definition
from mindroom.automations.registry import (
    AutomationCatalog,
    automation_reference_errors,
    compile_automations,
    iter_module_automations,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.automations import BUILTIN_AUTOMATION_NAMES
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
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
    assert definition.settings == {"key": "value"}
    assert catalog.is_provided("weekly_digest")
    assert resolve_automation_definition("weekly_digest", catalog) is definition
    assert catalog.collisions == ("dreaming", "weekly_digest")


def test_built_ins_resolve_without_any_plugin() -> None:
    """Built-in definitions stay outside the plugin catalog but are always available to run."""
    catalog = compile_automations([])

    assert frozenset(BUILTIN_AUTOMATION_DEFINITIONS) == frozenset(BUILTIN_AUTOMATION_NAMES)
    assert catalog.get("dreaming") is None
    assert catalog.is_provided("dreaming")
    assert not catalog.is_provided("weekly_digest")
    for name in ("prompt_curation", "dreaming", "exploration"):
        definition = resolve_automation_definition(name, catalog)
        assert definition is not None
        assert definition.name == name
        assert definition.requires_file_memory is (name != "exploration")
    assert resolve_automation_definition("weekly_digest", catalog) is None


def test_automation_and_hook_registries_do_not_import_domain_backends() -> None:
    """Plugin loading and built-in name validation do not need FastAPI or automation implementations."""
    probe = """
import sys

import mindroom.hooks.registry
import mindroom.tool_system.plugins
from mindroom.automations.registry import automation_reference_errors, compile_automations
from mindroom.config.main import Config

config = Config(defaults={"automations": ["dreaming"]})
assert automation_reference_errors(config, compile_automations([])) == []

forbidden = (
    "fastapi",
    "mindroom.memory",
    "mindroom.knowledge_graph",
    "mindroom.contacts",
    "mindroom.session_storage",
    "mindroom.automations.dreaming",
    "mindroom.automations.exploration",
    "mindroom.automations.prompt_curation",
)
loaded = sorted(
    name for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in forbidden)
)
assert not loaded, loaded
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=120)
    assert result.returncode == 0, result.stderr


def test_reference_errors_cover_defaults_explicit_lists_and_collisions() -> None:
    """Every authored entry is checked, built-ins always resolve, and a name registered twice is reported."""
    config = Config(
        defaults={"automations": [{"name": "nightly", "cron": "0 1 * * *"}]},
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                memory_backend="file",
                automations=["dreaming", {"name": "weekly_digest", "cron": "0 9 * * 1"}],
            ),
        },
        router=RouterConfig(model="default"),
    )

    assert automation_reference_errors(config, AutomationCatalog(collisions=("dup",))) == [
        "defaults.automations: 'nightly' is not provided by any loaded plugin",
        "agents.mind.automations: 'weekly_digest' is not provided by any loaded plugin",
        "automation 'dup' is registered more than once",
    ]
