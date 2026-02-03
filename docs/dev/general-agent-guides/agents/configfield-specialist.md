# ConfigField Specialist Guidelines

## Mission

Recreate the `.claude/agents/configfield-generator.md` flow without
project-specific paths.

## Required Workflow

1. Read project instructions first.
2. Copy the standard tool-module template.
3. Fetch the upstream docs list, then the target tool doc (drop `.md` when
   storing `docs_url`).
4. Inspect the tool constructor for parameter names, types, defaults.
5. Map Python types to field types (`bool→boolean`, `str→text/password/url`,
   etc.) and write concise descriptions.
6. Create `tools/<tool>.py` (or equivalent) that registers via the official
   decorator.
7. Expose the module in the registry `__init__`.
8. Add required dependencies with comments explaining which tool needs them.
9. Run the verification helper (e.g.,
   `verify_tool_configfields('<tool>', ToolClass)`).
10. Report the command and result.
