"""Workspace-authored automation loading primitives."""

from mindroom.workspace_automations.loader import AUTOMATIONS_RELATIVE_PATH, load_workspace_automations
from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationCheck,
    WorkspaceAutomationDefinition,
    WorkspaceAutomationFile,
    WorkspaceAutomationLoadError,
    WorkspaceAutomationLoadResult,
    WorkspaceAutomationTrigger,
)
from mindroom.workspace_automations.service import (
    AutomationKey,
    WorkspaceAutomationLoadedStatus,
    WorkspaceAutomationScanResult,
    WorkspaceAutomationService,
)
from mindroom.workspace_automations.targets import (
    WorkspaceAutomationTarget,
    iter_workspace_automation_targets,
    resolve_action_room,
)

__all__ = [
    "AUTOMATIONS_RELATIVE_PATH",
    "AutomationKey",
    "LoadedWorkspaceAutomation",
    "WorkspaceAutomationAction",
    "WorkspaceAutomationCheck",
    "WorkspaceAutomationDefinition",
    "WorkspaceAutomationFile",
    "WorkspaceAutomationLoadError",
    "WorkspaceAutomationLoadResult",
    "WorkspaceAutomationLoadedStatus",
    "WorkspaceAutomationScanResult",
    "WorkspaceAutomationService",
    "WorkspaceAutomationTarget",
    "WorkspaceAutomationTrigger",
    "iter_workspace_automation_targets",
    "load_workspace_automations",
    "resolve_action_room",
]
