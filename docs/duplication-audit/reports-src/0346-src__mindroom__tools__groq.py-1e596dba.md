## Summary

No meaningful duplication found.
The `groq_tools` function is a standard lazy toolkit class factory, and similar factories exist across provider tool modules, but the repeated behavior is intentionally local metadata registration boilerplate rather than Groq-specific duplicated functionality.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
groq_tools	function	lines 91-95	related-only	groq_tools; GroqTools; agno.tools.models.groq; def *_tools() returning toolkit class	src/mindroom/tools/groq.py:91; src/mindroom/tools/openai.py:119; src/mindroom/tools/gemini.py:91; src/mindroom/tools/eleven_labs.py:91; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/__init__.py:77
```

## Findings

No real duplication requiring refactor was found for `groq_tools`.

`src/mindroom/tools/groq.py:91` lazily imports and returns `agno.tools.models.groq.GroqTools`.
The same factory shape appears in many tool registration modules, including `src/mindroom/tools/openai.py:119`, `src/mindroom/tools/gemini.py:91`, `src/mindroom/tools/eleven_labs.py:91`, and `src/mindroom/tools/cartesia.py:77`.
These are related because each function delays optional dependency import until the registered toolkit is requested.
They are not a strong duplication candidate because each function has a different return type, import path, metadata decorator, dependency package, docs URL, and function list.

No other Groq-specific tool wrapper, audio transcription wrapper, translation wrapper, or speech-generation wrapper was found under `src/mindroom`.
Other Groq references are model/provider configuration or diagnostics, such as `src/mindroom/model_loading.py:12`, `src/mindroom/model_loading.py:121`, `src/mindroom/constants.py:1014`, `src/mindroom/error_handling.py:23`, and `src/mindroom/cli/doctor.py:147`.
Those references are not behavior duplicates of the toolkit factory.

## Proposed Generalization

No refactor recommended.
A generic lazy toolkit factory helper would save only two lines per module and would make type annotations and direct imports less explicit.
It would also not address the larger repeated decorator metadata, which is intentionally provider-specific.

## Risk/tests

No production change was made.
If this area were refactored later, tests should verify that the `groq` tool remains registered with the same metadata, optional dependency import remains lazy, and `groq_tools()` still returns `GroqTools` without instantiating it.
