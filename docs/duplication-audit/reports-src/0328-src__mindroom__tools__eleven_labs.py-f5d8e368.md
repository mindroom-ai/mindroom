## Summary

The `eleven_labs_tools` factory duplicates the repository-wide built-in tool wrapper pattern: a metadata-decorated module exposes a zero-argument function that lazily imports an Agno toolkit class and returns the class object.
This pattern appears across many files in `src/mindroom/tools/`, including adjacent audio/TTS providers such as Cartesia, DesiVocal, OpenAI, and Groq.
The audio-provider metadata overlaps with those modules, but the required symbol's executable behavior is only the lazy-import factory.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
eleven_labs_tools	function	lines 91-95	duplicate-found	eleven_labs_tools lazy import return ElevenLabsTools; def *_tools return *Tools; text_to_speech audio provider factories	src/mindroom/tools/cartesia.py:77-81, src/mindroom/tools/desi_vocal.py:65-69, src/mindroom/tools/openai.py:119-123, src/mindroom/tools/groq.py:91-95, src/mindroom/tools/apify.py:46-50, src/mindroom/tools/dalle.py:84-88
```

## Findings

### Repeated Agno toolkit factory

- `src/mindroom/tools/eleven_labs.py:91-95` lazily imports `ElevenLabsTools` from `agno.tools.eleven_labs` inside `eleven_labs_tools` and returns the toolkit class.
- `src/mindroom/tools/cartesia.py:77-81` performs the same behavior for `CartesiaTools`.
- `src/mindroom/tools/desi_vocal.py:65-69` performs the same behavior for `DesiVocalTools`.
- `src/mindroom/tools/openai.py:119-123` performs the same behavior for `OpenAITools`.
- `src/mindroom/tools/groq.py:91-95` performs the same behavior for `GroqTools`.
- Broad searches also found the same pattern in many non-audio wrappers such as `src/mindroom/tools/apify.py:46-50` and `src/mindroom/tools/dalle.py:84-88`.

The behavior is duplicated because each function exists only to defer the Agno import until the tool registry calls the factory, then return the imported toolkit type.
Differences to preserve are the source module path, returned toolkit class, function name, docstring, type annotation, and the per-tool metadata decorator attached to each factory.

### Related audio/TTS metadata overlap

- `src/mindroom/tools/cartesia.py:14-75`, `src/mindroom/tools/desi_vocal.py:14-63`, `src/mindroom/tools/openai.py:14-117`, and `src/mindroom/tools/groq.py:14-89` define similar API-key setup and speech-related enable flags.
- This is related configuration shape rather than duplicated behavior in the required symbol.
- Provider-specific option names, defaults, dependencies, docs URLs, and function names differ enough that a shared metadata abstraction is not clearly justified by this symbol alone.

## Proposed Generalization

No refactor recommended for this task.
The repeated factory is intentional registry boilerplate and currently carries per-tool type annotations and docstrings.
A helper such as `toolkit_factory(import_path, class_name)` would remove two executable lines per wrapper but would make typing weaker or require additional generated stubs.

## Risk/tests

If this pattern were generalized later, tests should verify that built-in tool discovery still registers every factory with its metadata, optional Agno dependencies remain lazily imported, and missing optional packages still fail only when the specific tool is loaded.
No production code was changed for this audit.
