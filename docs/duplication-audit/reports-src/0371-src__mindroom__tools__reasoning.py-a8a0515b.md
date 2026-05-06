# Summary

No meaningful duplication found.
The `reasoning_tools` function follows the same lazy toolkit factory shape used by many `src/mindroom/tools/*` modules, but this is intentional registry boilerplate around distinct tool metadata and distinct imported toolkit classes.
Refactoring this one wrapper would reduce two lines of local code while making import behavior and metadata registration less explicit.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
reasoning_tools	function	lines 79-83	related-only	reasoning_tools, ReasoningTools, register_tool_with_metadata factories, lazy import return Toolkit class	src/mindroom/tools/reasoning.py:79; src/mindroom/tools/calculator.py:27; src/mindroom/tools/sleep.py:42; src/mindroom/tools/website.py:35; src/mindroom/tools/matrix_message.py:28; src/mindroom/tools/__init__.py:101; src/mindroom/tool_system/metadata.py:760
```

# Findings

No real duplicated behavior requiring a refactor was found for `reasoning_tools`.

Related pattern:

- `src/mindroom/tools/reasoning.py:79` lazily imports and returns `agno.tools.reasoning.ReasoningTools`.
- `src/mindroom/tools/calculator.py:27`, `src/mindroom/tools/sleep.py:42`, and `src/mindroom/tools/website.py:35` use the same small factory pattern for different Agno toolkit classes.
- `src/mindroom/tools/matrix_message.py:28` uses the same shape for a MindRoom custom toolkit class.
- `src/mindroom/tool_system/metadata.py:760` registers each decorated factory as tool metadata via the `factory` field, so the repeated function shape is part of the public registration convention.

Why this is only related:

- The shared mechanics are just lazy import plus returning a toolkit class.
- Each module carries unique tool metadata, config fields, docs URL, dependencies, and function names.
- A generic import-by-string helper would need to preserve type-checking imports, metadata factory identity, and lazy optional dependency behavior, while removing very little code.

# Proposed Generalization

No refactor recommended.

If this pattern becomes a maintenance issue across the whole tools package, consider a repository-wide design change to generate simple toolkit factories from typed module/class descriptors.
That would be broader than this primary-file audit and should be evaluated across all tool registration modules, not introduced for `reasoning_tools` alone.

# Risk/Tests

No production changes were made.

If a future refactor centralizes these factories, tests should cover:

- Built-in tool metadata registration still records the correct factory for `reasoning`.
- `reasoning_tools()` still performs a lazy import of `agno.tools.reasoning.ReasoningTools`.
- Tool discovery through `src/mindroom/tools/__init__.py` still exposes `reasoning_tools`.
