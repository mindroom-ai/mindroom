"""Built-in and plugin automations: a cron schedule, a cheap check in code, then a chain of visible prompts, each followed by a step in code."""

from mindroom.automations.registry import automation
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.automations.threads import automation_threads

__all__ = ["Ask", "AutomationContext", "Done", "automation", "automation_threads"]
