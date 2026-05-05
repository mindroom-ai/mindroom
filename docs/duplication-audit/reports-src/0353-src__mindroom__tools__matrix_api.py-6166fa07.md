## Summary

No meaningful duplication found for the `matrix_api` tool registration itself.
The `matrix_api_tools` function follows the same lazy-import factory pattern used by many `src/mindroom/tools/*.py` registry modules, but its specific registration metadata and returned toolkit are unique to `MatrixApiTools`.
This is structural boilerplate rather than duplicated domain behavior worth consolidating from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
matrix_api_tools	function	lines 41-45	related-only	matrix_api_tools MatrixApiTools register_tool_with_metadata function_names matrix_api lazy import return Tools	src/mindroom/tools/matrix_api.py:13; src/mindroom/tools/matrix_message.py:13; src/mindroom/tools/matrix_room.py:13; src/mindroom/tools/thread_tags.py:13; src/mindroom/tools/thread_summary.py:13; src/mindroom/tools/calculator.py:13; src/mindroom/tools/__init__.py:84
```

## Findings

No real duplication found.

Related boilerplate exists in `src/mindroom/tools/matrix_api.py:41`, `src/mindroom/tools/matrix_message.py:28`, `src/mindroom/tools/matrix_room.py:26`, `src/mindroom/tools/thread_tags.py:26`, `src/mindroom/tools/thread_summary.py:26`, and many other tool config modules such as `src/mindroom/tools/calculator.py:27`.
Each function is a zero-argument lazy factory that imports one toolkit class inside the function and returns it, paired with a `register_tool_with_metadata` decorator.
The behavior is mechanically similar, but the metadata payload is intentionally per-tool and the returned class differs at every call site.

The closest Matrix-domain candidates are `matrix_message_tools`, `matrix_room_tools`, `thread_tags_tools`, and `register_thread_summary_tools`.
They share category/setup/dependency/docs fields with `matrix_api_tools`, but expose different model-facing tool surfaces and distinct `function_names`.
No other source file registers `name="matrix_api"` or returns `MatrixApiTools`.

## Proposed Generalization

No refactor recommended.

A generic helper for lazy toolkit factories would add indirection without removing meaningful behavior from this primary file.
The existing repeated shape is easy to inspect, keeps metadata beside each tool, and avoids centralizing heterogeneous imports and type annotations.

## Risk/tests

No production change was made.
If a future broad cleanup intentionally deduplicates tool registration boilerplate, tests should verify metadata export and registry loading, especially `src/mindroom/tools_metadata.json` generation expectations and `ensure_tool_registry_loaded` behavior.
