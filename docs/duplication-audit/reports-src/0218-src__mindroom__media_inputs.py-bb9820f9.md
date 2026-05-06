## Summary

No meaningful duplication found.
`MediaInputs` is the central shared media carrier under `src`, and the surrounding modules consistently import and use it instead of reimplementing a competing audio/image/file/video container.
The only related repetition is normal call-site behavior: defaulting optional media to `MediaInputs()` and checking `has_any()` before attaching inline media.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MediaInputs	class	lines 15-42	related-only	MediaInputs, audio images files videos, media carrier	src/mindroom/ai.py:69, src/mindroom/ai_runtime.py:20, src/mindroom/teams.py:67, src/mindroom/response_runner.py:70, src/mindroom/inbound_turn_normalizer.py:32, src/mindroom/bot.py:88
MediaInputs.from_optional	method	lines 24-38	none-found	from_optional, tuple(audio or ()), tuple(images or ()), resolve_attachment_media	src/mindroom/inbound_turn_normalizer.py:382, src/mindroom/media_inputs.py:34
MediaInputs.has_any	method	lines 40-42	related-only	has_any(), audio or images or files or videos, attach_media_to_run_input	src/mindroom/media_fallback.py:29, src/mindroom/ai_runtime.py:70, src/mindroom/teams.py:1656, src/mindroom/teams.py:1884, src/mindroom/teams.py:2268
```

## Findings

No real duplication found.

`MediaInputs` in `src/mindroom/media_inputs.py:15` is used as the shared typed carrier by AI response generation, team execution, response runner lifecycle code, inbound turn normalization, and bot orchestration.
The checked call sites pass the same four media collections through this dataclass rather than defining parallel containers.

`MediaInputs.from_optional` in `src/mindroom/media_inputs.py:24` appears to be the only helper that normalizes optional media sequences to tuples.
The main caller is `src/mindroom/inbound_turn_normalizer.py:382`, after `resolve_attachment_media` returns attachment audio, images, files, and videos.
I did not find another function under `src` repeating the same optional-to-tuple normalization for all four media fields.

`MediaInputs.has_any` in `src/mindroom/media_inputs.py:40` centralizes the "any inline media present" predicate.
Related call sites in `src/mindroom/media_fallback.py:29`, `src/mindroom/teams.py:1656`, `src/mindroom/teams.py:1884`, and `src/mindroom/teams.py:2268` use the helper directly.
Repeated `media or MediaInputs()` defaults in `src/mindroom/ai.py:938`, `src/mindroom/ai.py:1427`, `src/mindroom/ai_runtime.py:340`, `src/mindroom/teams.py:1588`, `src/mindroom/teams.py:1872`, `src/mindroom/response_runner.py:1469`, and `src/mindroom/bot.py:1936` are related setup, but not enough to justify another abstraction.

## Proposed Generalization

No refactor recommended.
The existing module is already the generalization for this behavior.
Adding another helper around `media or MediaInputs()` would obscure simple call-site intent and would not remove meaningful functional duplication.

## Risk/tests

No production code was changed.
If future changes altered `MediaInputs`, tests should cover attachment normalization in inbound turn dispatch, inline-media fallback retry decisions, and media attachment to Agno run inputs.
