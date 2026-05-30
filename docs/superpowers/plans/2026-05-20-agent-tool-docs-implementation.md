# Agent Tool Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add short inline tool summaries and richer metadata popovers to the agent editor without duplicating tool descriptions outside backend tool metadata.

**Architecture:** Extend the frontend tool metadata type, add a small Radix Popover UI wrapper, and extract the repeated agent-editor tool row into reusable `ToolSelectionRow` and `ToolInfoPopover` components. Keep `/api/tools` as the source of truth and preserve all existing selection, settings, badge, and unavailable-tool behavior.

**Tech Stack:** React 19, TypeScript, Vite, Vitest, React Testing Library, Tailwind, Radix UI primitives, Bun.

---

### Task 1: Metadata-Driven Tool Docs Tests

**Files:**
- Modify: `frontend/src/components/AgentEditor/AgentEditor.test.tsx`

- [x] **Step 1: Write failing tests for inline summaries and detail popovers**

Add tests near the existing tool-section tests.
Use the mocked `useTools` hook to return tools with `description`, `helper_text`, `docs_url`, `function_names`, and optional metadata omitted for another tool.
Assert that the row description appears inline, the info trigger is accessible by name, clicking it shows helper text, docs link, setup details, and capped function names, and a minimal tool does not render placeholder noise.

- [x] **Step 2: Run the focused AgentEditor test to verify RED**

Run: `cd frontend && bun run test -- src/components/AgentEditor/AgentEditor.test.tsx`
Expected: FAIL because the agent editor does not yet render backend tool descriptions or info popovers.

### Task 2: Tool Metadata Types And Popover Primitive

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/bun.lock`
- Modify: `frontend/src/hooks/useTools.ts`
- Create: `frontend/src/components/ui/popover.tsx`

- [x] **Step 1: Add the Radix Popover dependency**

Run: `cd frontend && bun add @radix-ui/react-popover`
Expected: `frontend/package.json` and `frontend/bun.lock` include `@radix-ui/react-popover`.

- [x] **Step 2: Add a shared Popover wrapper**

Create `frontend/src/components/ui/popover.tsx` using the same style as `frontend/src/components/ui/tooltip.tsx`.
Export `Popover`, `PopoverTrigger`, and `PopoverContent`.
Use existing `cn` utility and the same border, popover background, foreground, shadow, and animation token style.

- [x] **Step 3: Extend `ToolInfo`**

Add optional fields to `frontend/src/hooks/useTools.ts` for metadata consumed in the agent editor:
`default_execution_target?: string | null` and `function_names?: string[] | null`.
Keep existing fields unchanged.

### Task 3: Reusable Tool Row And Info Popover

**Files:**
- Create: `frontend/src/components/AgentEditor/ToolInfoPopover.tsx`
- Create: `frontend/src/components/AgentEditor/ToolSelectionRow.tsx`
- Modify: `frontend/src/components/AgentEditor/AgentEditor.tsx`

- [x] **Step 1: Implement `ToolInfoPopover`**

Render an icon button with accessible label `Show ${tool.display_name} tool details`.
Render a popover with the tool name, description, helper text, setup type, status, default execution target, docs link, dependencies, and a capped function list.
Hide each optional row when the corresponding metadata is absent.
Cap function names at six visible names and show `+N more` when there are more.

- [x] **Step 2: Implement `ToolSelectionRow`**

Move the repeated row layout into a single component.
Render checkbox, display name, muted line-clamped `tool.description`, customized badge, section-specific setup badge/message, info popover, and settings button.
Preserve the current behavior where checked tools with settings can use the name as a button to toggle the settings panel.

- [x] **Step 3: Replace duplicated AgentEditor tool row markup**

In `AgentEditor.tsx`, render `ToolSelectionRow` for configured, default, and setup-required sections.
Keep the section grouping logic and `ToolConfigPanel` placement.
Preserve unavailable selected-tool rendering as-is, except do not add normal docs popovers to those warning rows.

### Task 4: Verification And PR Prep

**Files:**
- Verify all modified files.

- [x] **Step 1: Run focused frontend test**

Run: `cd frontend && bun run test -- src/components/AgentEditor/AgentEditor.test.tsx`
Expected: PASS.

- [x] **Step 2: Run frontend type-check**

Run: `cd frontend && bun run type-check`
Expected: PASS.

- [x] **Step 3: Run frontend build**

Run: `cd frontend && bun run build`
Expected: PASS.

- [x] **Step 4: Run required backend test gate**

Run: `uv run pytest`
Expected: PASS, or report the exact blocking failure.
Result: Blocked before tests start because the frontend build contains unresolved Git LFS pointer assets: `logo-square.png`, `favicon.png`, and `logo.png`.

- [x] **Step 5: Run pre-commit**

Run: `uv sync --all-extras && uv run pre-commit run --all-files`
Expected: PASS, or report the exact blocking failure.
Result: `uv sync --all-extras` and `uv run pre-commit run --all-files` both fail before hooks for the same unresolved Git LFS pointer assets.

- [x] **Step 6: Commit and open PR**

Stage only the implementation, dependency, test, spec, and plan files.
Do not stage unrelated image changes or `.superpowers/brainstorm` mockup artifacts.
Commit with a focused message.
Push `codex/agent-tool-docs-design`.
Create a GitHub PR summarizing the metadata-driven inline summaries and popovers, with verification commands and results.
