## Summary

The only meaningful duplication candidate in `src/mindroom/tools/zoom.py` is the standard Agno toolkit registration pattern: a metadata-decorated zero-argument factory imports one toolkit class lazily and returns the class unchanged.
This is repeated across many files in `src/mindroom/tools`, including `webex`, `linear`, `slack`, and `spotify`.
No Zoom-specific duplicate meeting-management behavior was found elsewhere under `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
zoom_tools	function	lines 62-66	duplicate-found	zoom_tools; agno.tools.zoom; def *_tools() -> type[*Tools]; return *Tools; video conferencing tools	src/mindroom/tools/webex.py:56-60; src/mindroom/tools/linear.py:44-48; src/mindroom/tools/slack.py:160-164; src/mindroom/tools/spotify.py:71-75; src/mindroom/tools/__init__.py:139; src/mindroom/tools/__init__.py:259
```

## Findings

### Repeated metadata-decorated Agno toolkit factory

- `src/mindroom/tools/zoom.py:62-66` defines `zoom_tools`, lazily imports `ZoomTools`, and returns the class.
- `src/mindroom/tools/webex.py:56-60` defines the same shape for `WebexTools`.
- `src/mindroom/tools/linear.py:44-48` defines the same shape for `LinearTools`.
- `src/mindroom/tools/slack.py:160-164` defines the same shape for `SlackTools`.
- `src/mindroom/tools/spotify.py:71-75` defines the same shape for `SpotifyTools`.

The behavior is functionally the same: each function exists to be discovered/exported as a tool provider, avoid importing the optional Agno toolkit at module import time except under `TYPE_CHECKING`, and return the toolkit class unchanged.
The metadata differs per provider and must remain provider-specific.
The factory body itself is duplicated across most simple tool wrappers.

### No duplicated Zoom-specific behavior found

Searches for `ZoomTools`, `agno.tools.zoom`, `ZOOM_`, and `zoom` under `./src` found only the primary module, generated metadata, and package exports.
There is no second implementation of Zoom meeting scheduling, token retrieval, recording lookup, or meeting listing in source code.

## Proposed Generalization

A minimal helper could live near tool metadata registration, for example in `src/mindroom/tool_system/metadata.py` or a small sibling module such as `src/mindroom/tool_system/toolkit_factory.py`.
It would create a lazy toolkit-class factory from a module path and class name while preserving the existing decorated function registration metadata.

No refactor is recommended for `zoom_tools` alone.
This pattern is broad across many tool wrapper modules, so changing only Zoom would make the code less consistent.
If this is addressed, migrate several simple wrappers together and keep the helper limited to the repeated lazy import and return-class behavior.

## Risk/Tests

The main risk is weakening static typing or tool discovery if generated factories lose stable function names such as `zoom_tools`.
Any refactor should verify metadata registration still exposes the same tool names, docs URLs, dependencies, config fields, and function names.
Focused tests should cover tool registry discovery and at least one lazy optional-dependency import path.
