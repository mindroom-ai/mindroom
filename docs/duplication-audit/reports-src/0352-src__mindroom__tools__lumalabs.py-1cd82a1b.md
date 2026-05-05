## Summary

`src/mindroom/tools/lumalabs.py` follows the repeated built-in Agno toolkit wrapper pattern used by many files under `src/mindroom/tools`: a metadata decorator, optional `TYPE_CHECKING` import, and a tiny lazy factory that imports and returns one toolkit class.
The closest functional duplicates are other AI media/voice wrappers such as `cartesia_tools`, `eleven_labs_tools`, `modelslabs_tools`, and `dalle_tools`.
This duplication is real but low-risk and mostly structural; no immediate refactor is recommended unless the tool registry is redesigned to support declarative class import targets.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
lumalabs_tools	function	lines 77-81	duplicate-found	"def *_tools() -> type", "from agno.tools", "return *Tools", "LumaLabTools", "luma/lumalabs"	src/mindroom/tools/cartesia.py:77, src/mindroom/tools/eleven_labs.py:91, src/mindroom/tools/modelslabs.py:103, src/mindroom/tools/dalle.py:84, src/mindroom/tools/__init__.py:83, src/mindroom/tool_system/metadata.py:749
```

## Findings

### Repeated lazy Agno toolkit factory wrappers

- Primary behavior: `src/mindroom/tools/lumalabs.py:77` defines `lumalabs_tools`, imports `LumaLabTools` inside the function, and returns the toolkit class at `src/mindroom/tools/lumalabs.py:79`.
- Duplicate behavior: `src/mindroom/tools/cartesia.py:77`, `src/mindroom/tools/eleven_labs.py:91`, `src/mindroom/tools/modelslabs.py:103`, and `src/mindroom/tools/dalle.py:84` define the same kind of factory wrapper for an Agno toolkit class.
- Why it is duplicated: each function exists primarily so `register_tool_with_metadata` can store metadata and a callable factory while delaying optional dependency imports until the tool is requested.
- Differences to preserve: each wrapper has a different Agno import path, return class, decorator metadata, dependencies, config fields, docs URL, and function names.
- Registry context: `src/mindroom/tool_system/metadata.py:749` stores the decorated function as `ToolMetadata.factory`, so the small wrapper function is currently part of the registration contract.

## Proposed Generalization

No refactor recommended for this file alone.

If this pattern is generalized later, the minimal option would be extending `register_tool_with_metadata` or adding a small adjacent helper in `src/mindroom/tool_system/metadata.py` that accepts a module path and class name and builds the lazy class factory.
That helper would need to preserve lazy imports for optional dependencies and keep per-tool metadata explicit at each call site.
Because the current wrappers are tiny, readable, and tied to decorator registration, changing only `lumalabs_tools` would not reduce meaningful complexity.

## Risk/Tests

- Main risk of generalization: importing optional Agno dependencies too early would break tools whose packages are not installed until runtime.
- Registry tests would need to verify that metadata registration still records the correct factory and that calling the factory imports and returns the expected toolkit class.
- Tool metadata export tests should cover unchanged names, config fields, dependencies, docs URLs, and function names for representative wrappers.
