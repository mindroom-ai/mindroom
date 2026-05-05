## Summary

No meaningful duplication found.
`src/mindroom/tools/matrix_message.py` is a thin tool-registry adapter whose behavior is limited to metadata registration and returning `MatrixMessageTools`.
The same wrapper pattern appears in neighboring Matrix tool adapters, but the per-tool metadata is intentionally distinct and a shared abstraction would add indirection without reducing active behavioral duplication.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
matrix_message_tools	function	lines 28-32	related-only	matrix_message_tools MatrixMessageTools register_tool_with_metadata native Matrix messaging tools return toolkit class	src/mindroom/tools/matrix_room.py:13; src/mindroom/tools/matrix_room.py:26; src/mindroom/tools/matrix_api.py:13; src/mindroom/tools/matrix_api.py:41; src/mindroom/tools/__init__.py:85; src/mindroom/tools/__init__.py:205; src/mindroom/custom_tools/matrix_message.py:18; src/mindroom/custom_tools/matrix_message.py:36
```

## Findings

No real duplication requiring refactor was found for `matrix_message_tools`.

Related pattern: `src/mindroom/tools/matrix_message.py:13` registers metadata and `src/mindroom/tools/matrix_message.py:28` returns the concrete toolkit class after a local import.
The same adapter shape exists in `src/mindroom/tools/matrix_room.py:13` and `src/mindroom/tools/matrix_room.py:26`, and in `src/mindroom/tools/matrix_api.py:13` and `src/mindroom/tools/matrix_api.py:41`.
These wrappers are structurally similar, but their behavioral contract is per-tool registry metadata: names, descriptions, icons, helper text, and function names differ.
The local import also keeps optional/custom toolkit import timing consistent with the existing tool registry style.

`src/mindroom/custom_tools/matrix_message.py:18` contains the actual Matrix messaging toolkit behavior.
It is not duplicated by the primary file; the primary file only exposes that toolkit to the registry.

## Proposed Generalization

No refactor recommended.
A generic helper for decorated toolkit-returning functions would save only a few lines per tool while making metadata registration less explicit.
For this primary file, preserving the current explicit adapter is simpler and easier to review.

## Risk/Tests

No production code changes were made.
If this wrapper were changed in the future, focused tests should verify that the tool registry still exposes `matrix_message`, its metadata, and the `MatrixMessageTools` class returned by `matrix_message_tools`.
