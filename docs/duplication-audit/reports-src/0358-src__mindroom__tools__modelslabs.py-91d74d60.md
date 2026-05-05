Summary: The only meaningful duplication is the standard tool-registration factory pattern repeated across `src/mindroom/tools`.
The media-generation metadata overlaps with `dalle`, `openai`, `gemini`, `replicate`, and `fal`, but the fields map to provider-specific Agno constructor options, so that overlap is related rather than directly duplicated behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
modelslabs_tools	function	lines 103-107	duplicate-found	"modelslabs_tools; from agno.tools.* import *Tools; return *Tools; generate_media; image/video/audio generation tool metadata"	src/mindroom/tools/dalle.py:84; src/mindroom/tools/openai.py:119; src/mindroom/tools/gemini.py:91; src/mindroom/tools/replicate.py:56; src/mindroom/tools/fal.py:63; src/mindroom/tools/desi_vocal.py:65
```

Findings:

1. Registered lazy toolkit factory boilerplate is repeated across tool wrapper modules.
   `src/mindroom/tools/modelslabs.py:103` lazy-imports `ModelsLabTools` and returns the toolkit class.
   The same behavior appears in `src/mindroom/tools/dalle.py:84`, `src/mindroom/tools/openai.py:119`, `src/mindroom/tools/gemini.py:91`, `src/mindroom/tools/replicate.py:56`, `src/mindroom/tools/fal.py:63`, and `src/mindroom/tools/desi_vocal.py:65`.
   Each function exists to provide a registry callback while avoiding runtime imports until the tool is selected.
   Differences to preserve: each factory imports a different Agno toolkit class and keeps a precise return annotation for static typing and registry metadata.

2. Media-generation tool metadata is related but not enough for a shared abstraction.
   `src/mindroom/tools/modelslabs.py:14` exposes `generate_media` and configurable media parameters such as `file_type`, `model_id`, dimensions, and wait timing.
   `src/mindroom/tools/replicate.py:14` and `src/mindroom/tools/fal.py:14` also expose media generation with API key, model, and enable flags.
   `src/mindroom/tools/dalle.py:14`, `src/mindroom/tools/openai.py:14`, and `src/mindroom/tools/gemini.py:14` cover overlapping image/video/audio generation surfaces.
   The shared behavior is user-facing metadata for configured media-generation providers, but the constructor fields and function names differ by provider, so merging fields would likely obscure provider-specific behavior.

Proposed generalization: No refactor recommended for this file.
The factory duplication is real but intentionally tiny, typed, and local to each provider module.
A helper such as `lazy_toolkit_factory(import_path, class_name)` would reduce three lines per file while weakening direct type annotations and making imports less explicit.
The metadata overlap should stay provider-specific unless a broader tool-metadata schema cleanup is already planned.

Risk/tests:

- If the factory pattern is generalized later, verify tool registration still stores callable factories, imports remain lazy, and each returned class is the exact Agno toolkit class.
- Tests should cover at least metadata registry loading for `modelslabs`, factory invocation, and construction with configured fields such as `api_key`, `file_type`, dimensions, and wait settings.
- No production code was edited for this audit.
