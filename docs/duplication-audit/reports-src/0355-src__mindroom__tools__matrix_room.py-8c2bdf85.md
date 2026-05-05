## Summary

No meaningful duplication found for Matrix-room-specific behavior.
The only repeated behavior is the standard tool registry factory pattern used by many `src/mindroom/tools/*` modules: metadata decorator, optional `TYPE_CHECKING` import, lazy runtime import, and returning a toolkit class.
That pattern is broad and low-value to refactor for this file alone because each module carries distinct metadata and the factory keeps optional imports lazy.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
matrix_room_tools	function	lines 26-30	related-only	matrix_room_tools; MatrixRoomTools; def *_tools return *Tools; register_tool_with_metadata Matrix communication tools	src/mindroom/tools/matrix_api.py:41; src/mindroom/tools/matrix_message.py:28; src/mindroom/tools/thread_tags.py:26; src/mindroom/tools/scheduler.py:26; src/mindroom/tools/__init__.py:84; src/mindroom/tools/__init__.py:206; src/mindroom/custom_tools/matrix_room.py:36
```

## Findings

No real Matrix-room-specific duplicated behavior was found.

Related-only pattern:

- [src/mindroom/tools/matrix_room.py:13](../../src/mindroom/tools/matrix_room.py) registers a tool with metadata and [src/mindroom/tools/matrix_room.py:26](../../src/mindroom/tools/matrix_room.py) lazily imports and returns `MatrixRoomTools`.
- [src/mindroom/tools/matrix_api.py:13](../../src/mindroom/tools/matrix_api.py) and [src/mindroom/tools/matrix_api.py:41](../../src/mindroom/tools/matrix_api.py) use the same registration/factory shape for `MatrixApiTools`.
- [src/mindroom/tools/matrix_message.py:13](../../src/mindroom/tools/matrix_message.py) and [src/mindroom/tools/matrix_message.py:28](../../src/mindroom/tools/matrix_message.py) use the same shape for `MatrixMessageTools`.
- [src/mindroom/tools/thread_tags.py:13](../../src/mindroom/tools/thread_tags.py) and [src/mindroom/tools/thread_tags.py:26](../../src/mindroom/tools/thread_tags.py) use the same shape for `ThreadTagsTools`.
- [src/mindroom/tools/scheduler.py:13](../../src/mindroom/tools/scheduler.py) and [src/mindroom/tools/scheduler.py:26](../../src/mindroom/tools/scheduler.py) show the same pattern is project-wide, not Matrix-room-specific.

The repeated behavior is functionally similar: expose a lightweight registration entrypoint while delaying the custom toolkit import until the tool class is requested.
The differences to preserve are all metadata values: tool name, display name, category, icon, helper text, dependencies, and function names.
The actual Matrix room toolkit implementation is unique at [src/mindroom/custom_tools/matrix_room.py:36](../../src/mindroom/custom_tools/matrix_room.py).

## Proposed Generalization

No refactor recommended for this task.

A generic factory/decorator helper could reduce boilerplate across many tool modules, but it would have to preserve lazy imports and per-tool metadata while touching a broad registry surface.
For a single five-line factory, that would increase review risk without simplifying Matrix room behavior.

## Risk/Tests

No production code was changed.
If the broader registry pattern were ever generalized, tests should cover registry metadata export, lazy import behavior for optional dependencies, `src/mindroom/tools/__init__.py` exports, and at least one native custom toolkit such as `matrix_room`.
