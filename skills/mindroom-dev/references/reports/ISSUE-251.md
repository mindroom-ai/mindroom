# ISSUE-251 investigation: deferred toolkit instructions

## Status

Investigation complete before implementation.

The root cause is Agno instruction collection from toolkits that MindRoom must instantiate for native provider tool search even though every function in those toolkits is marked `defer_loading: true` on the wire.

## Live evidence

The latest matching openclaw request inspected in `llm-requests-2026-07-20.jsonl` was timestamped `2026-07-20T20:10:18.099046-07:00` and used `claude-fable-5`.

The log was parsed with `json.JSONDecoder().raw_decode` in a loop because one physical line may contain multiple concatenated JSON objects.

Its `system_prompt` was 80,431 characters and contained one copy each of the Gmail query, Gmail composition, Google Calendar, and Google Drive instruction blocks.

The four source instruction constants contain 2,988 characters, and Agno adds one trailing newline for each of the three toolkits, so suppressing those toolkits will remove 2,991 rendered prompt characters and produce a 77,440-character prompt for the same payload inputs.

Representative Gmail, Calendar, and Drive function definitions in the same request all had `defer_loading: true`, including `send_email`, `create_draft_email`, `create_event`, `check_availability`, `google_drive_search_files`, and `google_drive_upload_file`.

## Root-cause data flow

1. Per-agent tool entries are normalized into `ToolConfigEntry` values carrying `defer` and `initial` in `src/mindroom/config/models.py`.
2. `Config._agent_authored_deferred_tool_configs()` in `src/mindroom/config/main.py` exposes the authored deferred entries, including whether each entry is initially loaded.
3. `create_agent()` in `src/mindroom/agents.py` identifies models that support native provider tool search and sets `native_deferred_tools`.
4. `_resolve_agent_dynamic_tool_selection()` passes every deferred tool name as loaded to `visible_tool_surface()` for the native path because the provider request must contain every function schema before the provider can search it.
5. `visible_tool_surface()` preserves `defer` and `initial` on each returned `EffectiveToolConfig`, so defer state is available during agent toolkit assembly.
6. `_assemble_agent_toolkits()` builds and prunes each final `Toolkit`, adds its function names to `deferred_wire_tool_names` when its effective entry is deferred and not initial, and passes every built toolkit to Agno's `Agent`.
7. The Claude and OpenAI native-search installers use `deferred_wire_tool_names` to add `defer_loading: true` to matching wire definitions.
8. Independently, Agno's `agent._tools.parse_tools()` appends `tool.instructions` to `agent._tool_instructions` whenever `tool.add_instructions` is true.
9. Agno's `agent._messages` prompt renderer appends every collected tool instruction block to the system prompt without consulting provider defer metadata.

The metadata catalog is not the injection point because it stores descriptions and factories but does not render toolkit instructions.

The Gmail, Calendar, and Drive wrappers inherit Agno toolkit constructors whose default `add_instructions=True` values attach the observed instruction constants.

## Decision point and mixed-toolkit semantics

Prompt assembly can make the correct decision after toolkit construction because it has both the final post-pruning function names and the exact wire-level deferred function-name set.

The generic rule will disable Agno instruction injection only when a non-empty toolkit function-name set is a subset of the wire-level deferred function-name set.

If even one contributed function is active, the toolkit is mixed and its instruction block remains inline.

Initial deferred entries count as active because MindRoom deliberately excludes their function names from `deferred_wire_tool_names`.

This rule is toolkit-agnostic and does not inspect Gmail, Calendar, Drive, or any instruction text.

## Delivery-on-load trade-off

The homegrown dynamic-tools path already omits an unloaded deferred toolkit from the agent and rebuilds the agent after `load_tool`, so Agno makes its instructions available after load without additional machinery.

Native provider tool search loads deferred schemas inside a provider request, and the current provider integrations have no hook that can extend the already-sent system prompt when that happens.

The implementation will therefore retain the instruction string on the toolkit but set `add_instructions=False` for fully deferred native toolkits.

This intentionally drops those instructions from native-search prompts for now rather than adding a disproportionate provider-specific instruction-delivery protocol.

## Planned verification

Focused tests will cover a fully deferred toolkit, a mixed active/deferred toolkit, and the existing homegrown load path that restores instructions when a deferred toolkit becomes loaded.

The final report update will record scoped tests, the full test suite, pre-commit, and exact measured prompt counts.
