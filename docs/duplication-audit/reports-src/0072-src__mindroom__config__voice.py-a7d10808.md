## Summary

No meaningful duplication found.
`src/mindroom/config/voice.py` defines three small Pydantic schema classes with defaults for voice processing.
The closest related code is generic provider/model/API-key/host configuration in model and memory config modules, plus transcription model metadata in tool registration modules, but there is no duplicated parsing, validation, IO wrapping, or lifecycle behavior in this file.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_VoiceSTTConfig	class	lines 8-14	related-only	VoiceSTTConfig stt provider model api_key host whisper-1 transcription_model	src/mindroom/config/models.py:469; src/mindroom/config/models.py:482; src/mindroom/config/memory.py:14; src/mindroom/tools/openai.py:59; src/mindroom/tools/groq.py:31; src/mindroom/voice_handler.py:336
_VoiceLLMConfig	class	lines 17-20	related-only	VoiceLLMConfig intelligence model default RouterConfig model command recognition	src/mindroom/config/models.py:502; src/mindroom/config/memory.py:24; src/mindroom/voice_handler.py:379; src/mindroom/voice_handler.py:458
VoiceConfig	class	lines 23-35	related-only	VoiceConfig voice enabled visible_router_echo stt intelligence default_factory	src/mindroom/config/main.py:392; src/mindroom/config/memory.py:167; src/mindroom/turn_controller.py:719; src/mindroom/voice_handler.py:278
```

## Findings

No real duplication was found in the primary file.

Related schema shape: `_VoiceSTTConfig` in `src/mindroom/config/voice.py:8` resembles `EmbedderConfig` in `src/mindroom/config/models.py:469` and `ModelConfig` in `src/mindroom/config/models.py:482` because all carry provider/model-like endpoint settings such as model, API key, and host.
This is only related schema vocabulary, not duplicated behavior.
The field semantics differ: `_VoiceSTTConfig.model` defaults to the audio transcription model `whisper-1`, `EmbedderConfig.model` defaults to an embedding model, and `ModelConfig.id` is the canonical runtime LLM model identifier.

Related transcription defaults: `_VoiceSTTConfig.model` in `src/mindroom/config/voice.py:12` and the OpenAI tool metadata field `transcription_model` in `src/mindroom/tools/openai.py:59` both default to `whisper-1`.
These settings configure different surfaces: runtime Matrix voice-message transcription versus the optional OpenAI tool exposed to agents.
They are not currently a shared behavior path, and forcing a shared constant could couple unrelated configuration defaults.

Related model selector shape: `_VoiceLLMConfig` in `src/mindroom/config/voice.py:17` and `RouterConfig` in `src/mindroom/config/models.py:502` both expose a `model` field defaulting to `default`.
This is a common config idiom for selecting a named model, not enough duplication to justify a helper.

Related nested config shape: `VoiceConfig` in `src/mindroom/config/voice.py:23` follows the same Pydantic pattern used by other top-level config sections such as `MemoryConfig` in `src/mindroom/config/memory.py:167` and is mounted into the root config at `src/mindroom/config/main.py:392`.
The common behavior is Pydantic's own nested `default_factory` handling, so there is no local behavior to deduplicate.

## Proposed Generalization

No refactor recommended.
The overlap is limited to small declarative config fields and repeated default strings.
A shared provider endpoint base model or shared transcription default constant would add coupling without removing active duplicated behavior.

## Risk/Tests

No production code was edited.
If a future change adds validation or endpoint construction to `voice.py`, compare it with `src/mindroom/config/models.py` and `src/mindroom/voice_handler.py` before adding new helpers.
Tests that would need attention for any future refactor are config parsing/default tests for `VoiceConfig` and voice transcription tests around `config.voice.stt`.
