Summary: `src/mindroom/tools/desi_vocal.py` follows the same metadata-registration and lazy toolkit-class return pattern used by many Agno wrapper modules.
The closest related behavior is in other voice/audio providers (`cartesia`, `eleven_labs`, `openai`, and `groq`), but their field names, defaults, dependencies, and exposed functions are provider-specific.
No meaningful production refactor is recommended for this primary file.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
desi_vocal_tools	function	lines 65-69	related-only	desi_vocal_tools; DesiVocalTools; text-to-speech; voice_id; enable_get_voices; enable_text_to_speech; Return .*tools for text-to-speech; def .*_tools\(\); return .*Tools	src/mindroom/tools/desi_vocal.py:65; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/eleven_labs.py:91; src/mindroom/tools/openai.py:119; src/mindroom/tools/groq.py:91
```

Findings:

No real duplication requiring consolidation was found.

Related-only pattern:
`desi_vocal_tools` at `src/mindroom/tools/desi_vocal.py:65` lazily imports `agno.tools.desi_vocal.DesiVocalTools` and returns the toolkit class.
The same class-provider shape appears in `src/mindroom/tools/cartesia.py:77`, `src/mindroom/tools/eleven_labs.py:91`, `src/mindroom/tools/openai.py:119`, and `src/mindroom/tools/groq.py:91`.
This is functionally related because each module registers provider metadata at import time and exposes a zero-argument function returning the concrete Agno toolkit class.
It is not a strong duplication candidate because the actual behavior in this file is only a two-line lazy import wrapper, while the meaningful configuration is provider-specific metadata.

The closest domain overlap is voice and speech synthesis metadata.
`desi_vocal.py` exposes `voice_id`, `enable_get_voices`, and `enable_text_to_speech`.
`eleven_labs.py` has similar voice and text-to-speech fields, but also includes `target_directory`, `model_id`, `output_format`, and sound-effect generation.
`cartesia.py` uses `default_voice_id`, `model_id`, voice localization, and `list_voices`.
`openai.py` and `groq.py` overlap only at the broader speech/audio feature level and use different function names and settings.
Those differences should remain explicit in each provider module.

Proposed generalization:

No refactor recommended.
Introducing a generic "return toolkit class" helper would remove only two lines per module while making lazy imports and type-checking less direct.
A shared voice-field helper is also not recommended from this file alone because the provider-specific names and defaults would still need to be passed explicitly, leaving little net simplification.

Risk/tests:

If a future refactor centralizes these wrapper patterns, tests should verify that metadata registration still exposes the same tool names, dependencies, config fields, default values, and function names for `desi_vocal`, `cartesia`, and `eleven_labs`.
The highest risk would be accidentally normalizing provider-specific constructor argument names such as `voice_id` versus `default_voice_id`.
