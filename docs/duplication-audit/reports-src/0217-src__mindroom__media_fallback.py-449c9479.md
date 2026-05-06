Summary: No meaningful duplication found. The primary file already centralizes inline-media provider error detection and one-time fallback prompt text; other `./src` call sites consume these helpers instead of duplicating their internals.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_media_validation_error_text	function	lines 17-24	none-found	source.base64 media_type input is not supported image was specified media validation	src/mindroom/ai.py:456; src/mindroom/ai.py:1065; src/mindroom/ai.py:1084; src/mindroom/teams.py:1692; src/mindroom/teams.py:1717; src/mindroom/teams.py:2311; src/mindroom/teams.py:2353
should_retry_without_inline_media	function	lines 27-31	related-only	should_retry_without_inline_media retry without inline media media_inputs.has_any validation error	src/mindroom/ai.py:443; src/mindroom/ai.py:456; src/mindroom/ai.py:1065; src/mindroom/ai.py:1084; src/mindroom/teams.py:1692; src/mindroom/teams.py:1717; src/mindroom/teams.py:2311; src/mindroom/teams.py:2353
append_inline_media_fallback_prompt	function	lines 34-43	related-only	Inline media unavailable fallback prompt attachment IDs inspect files append prompt	src/mindroom/ai_runtime.py:84; src/mindroom/ai_runtime.py:89; src/mindroom/teams.py:1705; src/mindroom/teams.py:1736; src/mindroom/teams.py:2322; src/mindroom/teams.py:2364; src/mindroom/attachments.py:146; src/mindroom/inbound_turn_normalizer.py:371
```

## Findings

No real duplication was found for the regex-based provider error classifier in `src/mindroom/media_fallback.py:17`.
The retry decision in `src/mindroom/media_fallback.py:27` is used repeatedly by `src/mindroom/ai.py` and `src/mindroom/teams.py`, but those call sites delegate to the shared helper rather than repeating its behavior.

`src/mindroom/ai_runtime.py:84` is a related wrapper around `append_inline_media_fallback_prompt`.
It applies the prompt helper to the last `Message` in a model run input and clears inline media fields at `src/mindroom/ai_runtime.py:90`.
That is not duplicate behavior because it handles `ModelRunInput` transformation while `media_fallback.py` handles plain prompt text.

`src/mindroom/attachments.py:146` and `src/mindroom/inbound_turn_normalizer.py:371` contain related attachment-guidance prompt text.
They are not duplicate inline-media fallback behavior: they announce available attachment IDs during normal media handling, while `append_inline_media_fallback_prompt` adds a one-time marker after a provider rejects inline media.
The shared phrase "Use tool calls to inspect or process them" is related wording, not enough shared behavior to justify a refactor in this primary-file audit.

## Proposed Generalization

No refactor recommended.
The existing `media_fallback.py` module is already the appropriate small shared helper for the duplicated retry and fallback concerns.

## Risk/Tests

No production changes were made.
If this area is changed later, tests should preserve the one-retry behavior, marker idempotence, regex coverage for provider validation strings, and the distinction between normal attachment-ID guidance and inline-media fallback guidance.
