## Summary

The only behavioral symbol in `src/mindroom/tools/fal.py` is `fal_tools`, a small lazy-import factory that returns the Agno `FalTools` class.
That factory shape is duplicated across most files in `src/mindroom/tools/`, including closely related media-generation providers.
The duplication is intentional registry boilerplate and does not justify a refactor by itself.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
fal_tools	function	lines 63-67	related-only	fal_tools; FalTools; return *Tools; generate_media; image_to_image; media generation factory	src/mindroom/tools/dalle.py:84; src/mindroom/tools/openai.py:119; src/mindroom/tools/replicate.py:56; src/mindroom/tools/modelslabs.py:103; src/mindroom/tools/gemini.py:91; src/mindroom/tools/lumalabs.py:77
```

## Findings

No meaningful duplication found that should be generalized from `fal_tools` alone.

`fal_tools` in `src/mindroom/tools/fal.py:63` follows the same lazy factory pattern as many other tool modules: import the concrete Agno toolkit inside the registered function and return the class.
Examples include `src/mindroom/tools/dalle.py:84`, `src/mindroom/tools/openai.py:119`, `src/mindroom/tools/replicate.py:56`, `src/mindroom/tools/modelslabs.py:103`, `src/mindroom/tools/gemini.py:91`, and `src/mindroom/tools/lumalabs.py:77`.
The shared behavior is real but very small, and each module still needs provider-specific metadata, config fields, dependencies, docs URL, type-checking import, function names, and concrete Agno import path.
The differences to preserve are the concrete toolkit class, import module, docstring, and metadata registration.

`fal.py` also overlaps conceptually with other media-generation tool configs.
`src/mindroom/tools/replicate.py:13` and `src/mindroom/tools/modelslabs.py:13` expose `generate_media`, while `src/mindroom/tools/gemini.py:13`, `src/mindroom/tools/dalle.py:13`, and `src/mindroom/tools/openai.py:13` cover image generation.
This is product-domain similarity rather than duplicated behavior in `fal_tools`; the config fields and provider semantics differ enough that a shared media-generation metadata builder would add indirection without reducing meaningful logic.

## Proposed Generalization

No refactor recommended.

If the repository later wants to reduce boilerplate across all tool registration modules, the smallest viable abstraction would be a helper in `mindroom.tool_system.metadata` that builds a registered lazy toolkit factory from a module path and class name.
That would affect many files and should be considered as a separate broad cleanup, not from this single-symbol audit.

## Risk/Tests

The main risk in generalizing this pattern is weakening import-time behavior, static typing, or tool metadata registration order.
Tests would need to cover tool registry discovery, optional dependency handling, metadata serialization, and instantiation for representative media tools such as Fal, Replicate, DALL-E, and Gemini.
