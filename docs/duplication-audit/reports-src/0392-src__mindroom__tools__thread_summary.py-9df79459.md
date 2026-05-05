Summary: `register_thread_summary_tools` duplicates the standard lightweight tool-registration shim used across many `src/mindroom/tools/*` modules: metadata decorator, `TYPE_CHECKING` import, deferred runtime import, and returning the toolkit class.
This is real structural duplication, but it is intentionally shallow and currently clearer than a generated or registry-table abstraction.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
register_thread_summary_tools	function	lines 26-30	related-only	register_thread_summary_tools, thread_summary, def .*tools() -> type[, TYPE_CHECKING deferred import, register_tool_with_metadata	src/mindroom/tools/thread_summary.py:13, src/mindroom/tools/thread_tags.py:13, src/mindroom/tools/matrix_message.py:13, src/mindroom/tools/attachments.py:19, src/mindroom/tools/subagents.py:13, src/mindroom/tools/scheduler.py:13, src/mindroom/tools/csv.py:13, src/mindroom/tools/__init__.py:121
```

## Findings

### Related structural duplication: tool metadata registration shims

- `src/mindroom/tools/thread_summary.py:13` decorates a no-arg function with `register_tool_with_metadata`, uses a `TYPE_CHECKING`-only toolkit import at `src/mindroom/tools/thread_summary.py:9`, then imports and returns `ThreadSummaryTools` inside `register_thread_summary_tools` at `src/mindroom/tools/thread_summary.py:26`.
- The same behavior appears in sibling tool modules such as `src/mindroom/tools/thread_tags.py:13`, `src/mindroom/tools/matrix_message.py:13`, `src/mindroom/tools/attachments.py:19`, `src/mindroom/tools/subagents.py:13`, `src/mindroom/tools/scheduler.py:13`, and `src/mindroom/tools/csv.py:13`.
- These modules all register metadata for one toolkit while deferring the concrete toolkit import until the registration function is called.
- Differences to preserve are the metadata values, function names, concrete toolkit class, and occasionally extra metadata such as managed init args or config fields.

This is related-only rather than a refactor-worthy duplicate for the assigned symbol because `register_thread_summary_tools` contains no domain behavior beyond returning a concrete class.
The repeated pattern is also spread across many small catalog modules where explicit local metadata remains easy to inspect.

## Proposed Generalization

No refactor recommended for this primary file.

If the project later wants to reduce boilerplate across all tool catalog modules, the minimal viable helper would live near `mindroom.tool_system.metadata` and accept a module path plus class name to create a deferred class loader.
That would need to preserve static typing ergonomics and readable per-tool metadata, so it is not justified by this isolated function.

## Risk/tests

- Risk of refactoring this pattern is breaking lazy imports for optional dependencies or making tool metadata less discoverable.
- Tests to watch if a future generalized loader is introduced: tool registry loading, exported tool metadata snapshots, optional dependency import behavior, and `get_tool_by_name("thread_summary")`.
- No production code was edited for this audit.
