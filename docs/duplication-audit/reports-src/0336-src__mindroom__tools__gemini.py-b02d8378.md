Summary: No meaningful duplication found.

`gemini_tools` follows the repository's common metadata-decorated toolkit factory pattern.
The closest matches are other `src/mindroom/tools/*` factories that lazily import and return an Agno toolkit class, but each function is bound to distinct metadata, dependencies, docs, config fields, and concrete toolkit type.
The shared behavior is too small and declarative to justify a production refactor.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
gemini_tools	function	lines 91-95	related-only	gemini_tools; GeminiTools; def *_tools returning Agno toolkit class; image/video generation toolkit factories	src/mindroom/tools/gemini.py:91; src/mindroom/tools/dalle.py:84; src/mindroom/tools/openai.py:119; src/mindroom/tools/lumalabs.py:77; src/mindroom/tools/cartesia.py:77
```

Findings:

No real duplication requiring consolidation.

Related pattern:

- `src/mindroom/tools/gemini.py:91` lazily imports `agno.tools.models.gemini.GeminiTools` and returns the class.
- `src/mindroom/tools/dalle.py:84`, `src/mindroom/tools/openai.py:119`, `src/mindroom/tools/lumalabs.py:77`, and `src/mindroom/tools/cartesia.py:77` use the same factory shape for their own Agno toolkit classes.
- The common behavior is only the registration-compatible factory shape.
  The module-specific decorator data is the important content, and replacing these tiny functions with a shared helper would mainly add indirection.

Proposed generalization: No refactor recommended.

Risk/tests:

- No production code was changed.
- If this pattern is ever generated or centralized, tests should verify tool registry discovery, lazy import behavior, metadata export, dependency declarations, and function-name filtering for representative tool modules.
