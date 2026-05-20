# Agent Tool Documentation In The Agent Editor

## Context

The agent editor currently lets users select tools from configured, default, and setup-required sections.
Those rows mostly show tool names, setup state, customization state, and settings actions.
The backend already exposes canonical tool metadata through `ToolMetadata` and `/api/tools`.
That metadata includes display names, short descriptions, helper text, documentation URLs, config fields, agent override fields, dependencies, setup type, status, and function names.
The UI should use that existing metadata instead of creating a second frontend description registry.

## Goal

Make it easy to understand what each tool does while selecting agent tools.
Show a short inline summary in each tool row.
Provide a richer info popover for users who want precise details.
Keep `ToolMetadata` and the `/api/tools` response as the single source of truth.
Avoid duplicating tool descriptions in `AgentEditor`, `toolConfig.ts`, or hard-coded frontend maps.

## Non-Goals

This change does not rewrite every tool description.
This change does not change tool runtime behavior.
This change does not redesign the global Tools or Integrations page.
This change does not add model-facing tool documentation beyond what the existing runtime already provides.

## User Experience

Each selectable tool row shows the tool display name and a one-line summary from `tool.description`.
The summary is visually secondary and line-clamped so the tool list remains scannable.
Each row includes a small info icon button with an accessible label such as `Show Shell tool details`.
Opening the info control displays a popover with the tool display name, description, helper text when present, setup type, setup status, docs link when present, and a compact function list when present.
The existing Settings button remains focused on per-agent overrides and is shown only when settings or overrides exist.
Setup-required tools still show the setup-required badge and scope support messages.
Unavailable selected tools keep their warning treatment and can show the unavailable reason instead of the normal docs popover.

## Architecture

The backend remains the canonical metadata owner.
`src/mindroom/tool_system/metadata.py` already exports tool metadata through `export_tools_metadata`.
`src/mindroom/api/tools.py` already returns that exported metadata from `/api/tools`.
The frontend `ToolInfo` type in `frontend/src/hooks/useTools.ts` should include every metadata field used by the agent editor, including `function_names` and `default_execution_target`.
No frontend code should add separate descriptions for backend-registered tools.

The agent editor should extract duplicated tool-row rendering into a focused component named `ToolSelectionRow`.
It receives `tool`, `sectionKind`, `isChecked`, `isActive`, `hasOverrides`, `showSettings`, and callbacks for toggling the tool and opening settings.
The three existing sections can share the same row component while preserving their badges and section-specific helper text.

The info popover should be a separate component named `ToolInfoPopover`.
It receives one `ToolInfo` object and renders only fields that exist.
For an interactive docs link, the implementation should use a proper popover primitive rather than a hover-only tooltip.
The frontend already uses Radix primitives, so the implementation should add a small `frontend/src/components/ui/popover.tsx` wrapper around `@radix-ui/react-popover`.
The implementation should add `@radix-ui/react-popover` to the frontend dependencies through Bun.

## Data Flow

The dashboard calls `useTools(agentName, executionScope)`.
`useTools` fetches `/api/tools` with the current agent and execution scope preview.
`AgentEditor` groups the returned `ToolInfo` records into configured, default, setup-required, and selected-unavailable sections.
Each `ToolSelectionRow` renders `tool.description` inline and passes the whole `ToolInfo` record to `ToolInfoPopover`.
The popover renders details from the same record.
Selecting or unselecting a tool continues to update only the agent `tools` field.
Editing per-agent settings continues to use `ToolConfigPanel` and the existing override metadata.

## Error Handling And Empty States

If a tool has no description, the row should omit the summary rather than inventing one.
If helper text, docs URL, function names, dependencies, or execution target are missing, the popover should hide those rows.
If a docs URL is present, the link opens in a new tab and uses `rel="noreferrer"`.
If the tools request is loading or policy preview is unavailable, the existing loading and validation messages remain unchanged.
If an unknown selected tool is no longer in the registry, the unavailable row continues to show the removal warning.

## Accessibility

The info trigger must be a real button.
The trigger must have an accessible label that includes the tool display name.
The popover must be keyboard reachable, dismissible with Escape, and not require hover.
The inline summary must not become the checkbox label by itself.
The checkbox label should continue to include the display name so selection remains easy.

## Styling

Rows should remain compact and operational.
The inline summary should use muted text and a one or two line clamp.
Cards should not be nested inside other cards.
The popover should use a small bordered surface with the existing background, foreground, and shadow tokens.
Long function lists should be capped, with a `+N more` suffix after a small number of functions.
The implementation should avoid adding a large detail panel that pushes the tool list around during scanning.

## Testing

Add AgentEditor tests that verify a backend tool description appears inline in the tool list.
Add AgentEditor tests that verify the info trigger renders for a tool with metadata.
Add AgentEditor tests that verify helper text, docs URL, and function names are available in the details surface.
Add AgentEditor tests that verify tools without optional metadata still render without placeholder noise.
Update `useTools` type coverage only if TypeScript requires it.
Run the relevant frontend test file after implementation.
Run the broader frontend build or type-check before claiming implementation is complete.

## Implementation Notes

Prefer refactoring the repeated configured, default, and setup-required row markup before adding the docs UI.
Keep row behavior identical for checking, unchecking, activating settings, and displaying customization badges.
Do not duplicate backend descriptions into `frontend/src/types/toolConfig.ts`.
Do not manually edit generated metadata snapshots unless the implementation changes the generator or registry output.
Treat this as a dashboard UX change with a narrow backend surface.
