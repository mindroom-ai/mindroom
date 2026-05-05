## Summary

The only meaningful duplication candidate in `src/mindroom/tools/attachments.py` is the standard metadata-decorated toolkit factory pattern.
`attachments_tools` repeats the same lazy import and `return ToolkitClass` behavior used by many modules in `src/mindroom/tools/`.
This is real structural duplication, but it is intentionally shallow registration boilerplate and not attachment-specific business logic.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
attachments_tools	function	lines 38-42	duplicate-found	attachments_tools, register_tool_with_metadata, "Return * tools", "from mindroom.custom_tools" lazy toolkit import	src/mindroom/tools/matrix_room.py:13-30; src/mindroom/tools/matrix_message.py:13-32; src/mindroom/tools/thread_tags.py:13-30; src/mindroom/tools/thread_summary.py:13-30; src/mindroom/tools/scheduler.py:13-30; src/mindroom/tools/config_manager.py:19-38; src/mindroom/tools/coding.py:20-56; src/mindroom/tools/__init__.py:26-150
```

## Findings

### Metadata-decorated lazy toolkit factory boilerplate

- Primary behavior: `src/mindroom/tools/attachments.py:19-42` registers tool metadata, type-checks the toolkit class, lazy-imports `AttachmentTools` inside `attachments_tools`, and returns the class.
- Duplicated behavior:
  - `src/mindroom/tools/matrix_room.py:13-30` registers metadata, lazy-imports `MatrixRoomTools`, and returns it.
  - `src/mindroom/tools/matrix_message.py:13-32` registers metadata, lazy-imports `MatrixMessageTools`, and returns it.
  - `src/mindroom/tools/thread_tags.py:13-30` registers metadata, lazy-imports `ThreadTagsTools`, and returns it.
  - `src/mindroom/tools/thread_summary.py:13-30` registers metadata, lazy-imports `ThreadSummaryTools`, and returns it.
  - `src/mindroom/tools/scheduler.py:13-30` registers metadata, lazy-imports `SchedulerTools`, and returns it.
  - `src/mindroom/tools/config_manager.py:19-38` adds managed init args but uses the same factory shape.
  - `src/mindroom/tools/coding.py:20-56` adds config fields and worker target metadata but uses the same factory shape.
- Why this is duplicated: each module repeats the same three-part registration wrapper: a `TYPE_CHECKING` import, a `register_tool_with_metadata(...)` decorator, and a function whose runtime behavior is only a lazy import plus returning the toolkit class.
- Differences to preserve: each tool has distinct metadata, dependencies, managed init args, execution target, function names, icon settings, and toolkit class path.

## Proposed Generalization

No refactor recommended for this file alone.

If the project later wants to reduce tool registration boilerplate across many modules, the minimal helper would live near `mindroom.tool_system.metadata` or `mindroom.tools` and accept a toolkit import path plus metadata, returning a decorated factory.
That helper would need to preserve lazy imports so importing `mindroom.tools` does not eagerly import optional tool dependencies.
Given the current one-symbol task, changing this pattern would be a broad cross-module refactor with limited payoff and non-trivial registry/import risk.

## Risk/tests

Behavior risk for any future consolidation is mostly import-time behavior: optional dependencies, plugin/tool registry side effects, and `TYPE_CHECKING`-only imports must remain lazy.
Tests would need to cover tool metadata export and registry loading, especially `attachments`, `matrix_message`, `matrix_room`, and tools with managed init args such as `config_manager`.
No production code was edited for this audit.
