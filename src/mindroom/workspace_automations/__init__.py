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

__all__ = [
    "AUTOMATIONS_RELATIVE_PATH",
    "LoadedWorkspaceAutomation",
    "WorkspaceAutomationAction",
    "WorkspaceAutomationCheck",
    "WorkspaceAutomationDefinition",
    "WorkspaceAutomationFile",
    "WorkspaceAutomationLoadError",
    "WorkspaceAutomationLoadResult",
    "WorkspaceAutomationTrigger",
    "load_workspace_automations",
]
