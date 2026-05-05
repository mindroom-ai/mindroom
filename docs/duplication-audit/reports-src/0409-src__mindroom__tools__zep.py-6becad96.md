Summary: `zep_tools` duplicates the standard lazy Agno toolkit factory pattern used by many `src/mindroom/tools/*` registration modules.
No meaningful production refactor is recommended because the duplicated behavior is only a two-line import/return shim tied to per-tool metadata decorators.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
zep_tools	function	lines 98-102	related-only	zep_tools ZepTools def *_tools lazy import return Toolkit register_tool_with_metadata	src/mindroom/tools/mem0.py:105; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/linear.py:44; src/mindroom/tools/__init__.py:138
```

Findings:

- Related-only: `zep_tools` in `src/mindroom/tools/zep.py:98` follows the same lazy factory shape as `mem0_tools` in `src/mindroom/tools/mem0.py:105`, `cartesia_tools` in `src/mindroom/tools/cartesia.py:77`, and `linear_tools` in `src/mindroom/tools/linear.py:44`.
  Each function exists primarily as a decorated registry entry and imports the concrete Agno toolkit inside the function before returning the toolkit class.
  The behavior is functionally similar, but each module carries distinct metadata fields, dependency lists, documentation URLs, function names, and type-only imports.
  `src/mindroom/tools/__init__.py:138` imports `zep_tools` into the package-level registry surface, matching the same export pattern used for neighboring tools.

Proposed generalization:

No refactor recommended.
The duplicated runtime behavior is too small to justify a helper, and the decorator-based metadata makes each factory a useful explicit registration point.
A shared helper would likely add indirection without reducing meaningful maintenance risk.

Risk/tests:

- If this pattern were generalized later, tests should verify tool registry loading, optional dependency behavior, function-name metadata export, and package-level `__all__` imports.
- Main risk would be changing import timing for optional Agno tool dependencies.
