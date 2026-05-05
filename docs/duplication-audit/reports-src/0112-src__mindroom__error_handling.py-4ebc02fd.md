## Summary

One related duplication candidate exists: `src/mindroom/api/openai_compat.py` knows the exact emoji prefixes emitted by `get_user_friendly_error_message()` so it can convert agent text failures into OpenAI-compatible error responses.
No meaningful duplicated implementation was found for provider extraction or the avatar marker exceptions.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
AvatarGenerationError	class	lines 10-11	related-only	AvatarGenerationError AvatarSyncError RuntimeError marker exceptions avatar generation errors	src/mindroom/avatar_generation.py:670; src/mindroom/cli/main.py:211
AvatarSyncError	class	lines 14-15	related-only	AvatarSyncError AvatarGenerationError RuntimeError marker exceptions avatar sync errors	src/mindroom/avatar_generation.py:450; src/mindroom/avatar_generation.py:461; src/mindroom/avatar_generation.py:495; src/mindroom/cli/main.py:232
_extract_provider_from_error	function	lines 18-26	none-found	__module__ module.split known_providers openai anthropic google groq cerebras httpx provider from error	none
get_user_friendly_error_message	function	lines 29-61	duplicate-found	user_friendly friendly error Authentication failed Rate limited Request timed out emoji error prefixes raw provider error	src/mindroom/api/openai_compat.py:596; src/mindroom/api/openai_compat.py:601; src/mindroom/cli/config.py:320; src/mindroom/media_fallback.py:18
```

## Findings

### Friendly agent error classification is mirrored by the OpenAI-compatible API

`get_user_friendly_error_message()` in `src/mindroom/error_handling.py:29` formats auth, rate-limit, timeout, and generic failures with the prefixes `❌`, `⏱️`, `⏰`, and `⚠️`.
`_is_error_response()` in `src/mindroom/api/openai_compat.py:596` repeats knowledge of those exact prefixes in `error_prefixes` at `src/mindroom/api/openai_compat.py:601`, including the bracketed agent-name prefix shape.
This is not duplicate formatting code, but it is duplicated classification knowledge: changing the friendly error prefixes requires a coordinated update in the API adapter.

Differences to preserve: `openai_compat` also detects raw provider errors through `_looks_like_raw_provider_error()` at `src/mindroom/api/openai_compat.py:633`.
That logic is API-adapter specific and should stay separate from user-facing error formatting.

### Avatar errors are related marker exceptions, not duplicated behavior

`AvatarGenerationError` and `AvatarSyncError` are simple marker exceptions in `src/mindroom/error_handling.py:10` and `src/mindroom/error_handling.py:14`.
They are raised from distinct avatar-generation and avatar-sync failure paths in `src/mindroom/avatar_generation.py:450`, `src/mindroom/avatar_generation.py:461`, `src/mindroom/avatar_generation.py:495`, and `src/mindroom/avatar_generation.py:670`, then caught by separate CLI commands in `src/mindroom/cli/main.py:211` and `src/mindroom/cli/main.py:232`.
The shared base class is already `RuntimeError`; there is no repeated behavior to generalize.

### Provider extraction appears unique

`_extract_provider_from_error()` in `src/mindroom/error_handling.py:18` is the only code under `src/mindroom` found to infer a provider from an exception class module via `type(error).__module__`.
Other provider lists in `src/mindroom/cli/config.py` and `src/mindroom/config_template.yaml` are configuration/provider-preset concerns, not exception introspection.

## Proposed Generalization

If this duplication is worth tightening, expose a small predicate from `error_handling.py`, for example `is_user_friendly_error_message(text: str) -> bool`, backed by the same friendly error prefixes used by `get_user_friendly_error_message()`.
`src/mindroom/api/openai_compat.py` could call that predicate before its existing raw-provider-error detection.

No refactor is recommended for the avatar marker exceptions or provider extraction.

## Risk/tests

The main risk is API behavior drift if friendly error prefixes change without updating `_is_error_response()`.
Tests to cover a refactor would be focused on `src/mindroom/api/openai_compat.py` error detection for bare emoji-prefixed messages, bracketed agent-prefix messages, and raw provider error strings.
No production code was edited for this audit.
