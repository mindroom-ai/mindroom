# Summary

The only required symbol, `cartesia_tools`, duplicates the common Agno toolkit factory pattern used by many modules in `src/mindroom/tools`.
The duplicated behavior is a lazy local import of one Agno toolkit class followed by returning that class object.
This is real but low-impact boilerplate, and the surrounding metadata is tool-specific enough that no immediate refactor is recommended for this file alone.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
cartesia_tools	function	lines 77-81	duplicate-found	cartesia_tools; "from agno.tools.* import *Tools"; "return *Tools"; def .*_tools() -> type	src/mindroom/tools/eleven_labs.py:91; src/mindroom/tools/desi_vocal.py:65; src/mindroom/tools/lumalabs.py:77; src/mindroom/tools/duckduckgo.py:98; src/mindroom/tools/wikipedia.py:42; src/mindroom/tools/trello.py:57; src/mindroom/tools/__init__.py:41
```

# Findings

## Repeated lazy Agno toolkit factory

- `src/mindroom/tools/cartesia.py:77` defines `cartesia_tools`, imports `CartesiaTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/eleven_labs.py:91` defines `eleven_labs_tools`, imports `ElevenLabsTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/desi_vocal.py:65` defines `desi_vocal_tools`, imports `DesiVocalTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/lumalabs.py:77` defines `lumalabs_tools`, imports `LumaLabTools` inside the function, and returns the toolkit class.
- The same shape appears broadly across tool modules such as `src/mindroom/tools/duckduckgo.py:98`, `src/mindroom/tools/wikipedia.py:42`, and `src/mindroom/tools/trello.py:57`.

These functions have the same runtime behavior: defer importing an optional Agno dependency until the registered tool factory is called, then expose the toolkit class to the central registry.
The differences to preserve are the concrete import path, toolkit class name, function name, return annotation, docstring, and the decorator metadata attached to each factory.

# Proposed Generalization

No refactor recommended for this isolated file.

If this pattern is generalized later across the tool catalog, the minimal helper would be a small lazy class resolver in `src/mindroom/tools/_lazy.py` or `src/mindroom/tool_system/metadata.py`, for example a helper that takes `module_path` and `class_name` and returns the toolkit class.
That helper would need to preserve optional dependency behavior, type-checking ergonomics, readable registered factory names, and decorator metadata on each public tool factory.

# Risk/tests

The main risk of deduplicating this pattern is changing when optional Agno integrations are imported.
Tests should cover that importing `mindroom.tools` does not require optional provider packages, and that calling each registered factory still returns the expected toolkit class after the dependency is installed.
No production code was edited.
