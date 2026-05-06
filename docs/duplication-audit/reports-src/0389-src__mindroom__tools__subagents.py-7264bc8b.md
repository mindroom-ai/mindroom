Summary: No meaningful duplication found.

The primary file only registers the `subagents` toolkit and returns the custom toolkit class through the repository's standard lazy-import factory pattern.
Several neighboring `src/mindroom/tools/*.py` modules use the same registration/factory shape, but this is a conventional metadata registration pattern rather than duplicated sub-agent behavior that should be generalized from this file.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
subagents_tools	function	lines 26-30	related-only	subagents_tools; SubAgentsTools; register_tool_with_metadata lazy import toolkit factory; def *_tools() -> type	src/mindroom/tools/subagents.py:13; src/mindroom/tools/scheduler.py:13; src/mindroom/tools/scheduler.py:26; src/mindroom/tools/matrix_message.py:13; src/mindroom/tools/matrix_message.py:28; src/mindroom/tools/attachments.py:19; src/mindroom/tools/attachments.py:38; src/mindroom/tools/__init__.py:118; src/mindroom/custom_tools/subagents.py:492
```

## Findings

No real duplication found for `subagents_tools`.

`src/mindroom/tools/subagents.py:13` decorates a factory with metadata, and `src/mindroom/tools/subagents.py:26` lazily imports and returns `SubAgentsTools`.
The same structural pattern appears in `src/mindroom/tools/scheduler.py:13` and `src/mindroom/tools/scheduler.py:26`, `src/mindroom/tools/matrix_message.py:13` and `src/mindroom/tools/matrix_message.py:28`, and `src/mindroom/tools/attachments.py:19` and `src/mindroom/tools/attachments.py:38`.
Those candidates are related registration boilerplate, not duplicated sub-agent orchestration behavior.

The actual sub-agent behavior lives in `src/mindroom/custom_tools/subagents.py:492`, where `SubAgentsTools` defines `agents_list`, `sessions_send`, `sessions_spawn`, and `list_sessions`.
I did not find another toolkit registration or wrapper in `./src` that duplicates that sub-agent-specific behavior.

## Proposed Generalization

No refactor recommended.

Extracting a generic metadata-decorated lazy factory would need to preserve per-tool metadata, type annotations, `TYPE_CHECKING` imports, and import-time registration side effects.
For this single primary symbol, that would reduce only a few lines of intentional boilerplate and would make registration less explicit.

## Risk/Tests

No production-code change is recommended.
If a future refactor centralizes toolkit factory registration, tests should cover import-time registration through `mindroom.tools`, metadata visibility through `resolved_tool_state_for_runtime`, and lazy loading of `SubAgentsTools` without importing `mindroom.custom_tools.subagents` during type checking only.
