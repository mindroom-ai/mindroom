Summary: No meaningful duplication found.
The only behavior in `src/mindroom/tools/scheduler.py` is a metadata-decorated lazy factory that returns `mindroom.custom_tools.scheduler.SchedulerTools`.
Several other tool modules use the same registration/factory convention, but the scheduler registration itself is unique and should remain explicit for registry discoverability.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
scheduler_tools	function	lines 26-30	related-only	scheduler_tools, SchedulerTools, name="scheduler", function_names=("cancel_schedule", "edit_schedule", "list_schedules", "schedule"), def .*_tools\(	src/mindroom/tools/__init__.py:106; src/mindroom/tools/__init__.py:227; src/mindroom/custom_tools/scheduler.py:22; src/mindroom/tools/calculator.py:27; src/mindroom/tools/matrix_room.py:26; src/mindroom/tools/thread_tags.py:26; src/mindroom/tools/sleep.py:42
```

Findings:
No real duplication was found for `scheduler_tools`.
The function uniquely registers the `scheduler` tool metadata and lazily imports `SchedulerTools`.
`src/mindroom/custom_tools/scheduler.py:22` contains the actual scheduler toolkit implementation, not a duplicate registration factory.
`src/mindroom/tools/__init__.py:106` imports `scheduler_tools`, and `src/mindroom/tools/__init__.py:227` exports it; these are registry wiring rather than duplicated behavior.
`src/mindroom/tools/calculator.py:27`, `src/mindroom/tools/matrix_room.py:26`, `src/mindroom/tools/thread_tags.py:26`, and `src/mindroom/tools/sleep.py:42` share the same lazy factory pattern, but their metadata and returned toolkit classes differ.
This appears to be an intentional convention across tool registration modules, not a duplication worth extracting for this file.

Proposed generalization:
No refactor recommended.
A generic helper for these one-line factories would likely obscure type checking, lazy imports, and per-tool metadata while saving little code.

Risk/tests:
No production code was changed.
If this registration were ever refactored, tests should cover tool registry loading, metadata export for `scheduler`, and that configured agents receive the four scheduler functions: `schedule`, `edit_schedule`, `list_schedules`, and `cancel_schedule`.
