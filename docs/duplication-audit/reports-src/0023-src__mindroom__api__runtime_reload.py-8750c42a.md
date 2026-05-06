## Summary

Top duplication candidates for `src/mindroom/api/runtime_reload.py`:

- `_published_snapshot` is actively duplicated in `src/mindroom/api/main.py` and `src/mindroom/api/config_lifecycle.py`.
  The three helpers all construct a new `ApiSnapshot`, increment generation, preserve unspecified fields with `_UNSET` semantics, and selectively replace cached config fields.
- `reload_api_runtime_config` partially duplicates runtime-swap cache invalidation from `initialize_api_app` and stale-snapshot rejection from config write helpers.
  It also repeats the two-phase load-then-publish pattern used by `load_config_into_app`, but its runtime-rebind semantics make it only related rather than a direct duplicate.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_published_snapshot	function	lines 22-48	duplicate-found	_published_snapshot ApiSnapshot generation _UNSET config_load_result auth_state	src/mindroom/api/main.py:210; src/mindroom/api/config_lifecycle.py:308; src/mindroom/api/config_lifecycle.py:242; src/mindroom/api/config_lifecycle.py:624
reload_api_runtime_config	function	lines 51-100	related-only	reload_api_runtime_config expected_snapshot mutate_runtime config_lock _load_config_result api_auth_account_id stale snapshot runtime swap clear_worker_validation_snapshot_cache	src/mindroom/api/main.py:250; src/mindroom/api/config_lifecycle.py:334; src/mindroom/api/config_lifecycle.py:352; src/mindroom/api/config_lifecycle.py:609; src/mindroom/api/config_lifecycle.py:613; tests/api/test_api.py:590
```

## Findings

### Duplicated API snapshot publishing helper

`src/mindroom/api/runtime_reload.py:22` duplicates the snapshot-copying behavior in `src/mindroom/api/main.py:210` and `src/mindroom/api/config_lifecycle.py:308`.
All three functions preserve existing `ApiSnapshot` fields unless replacements are explicitly supplied, use `_UNSET` to distinguish "leave existing optional value alone" from "set optional value to None", and advance the generation when publishing.

The differences to preserve are small but important.
`main._published_snapshot` also accepts `increment_generation=False` and can replace `runtime_paths` and `auth_state`.
`runtime_reload._published_snapshot` can replace `runtime_paths` and `auth_state`, but always increments.
`config_lifecycle._published_snapshot` only updates config payload, runtime config, and load result, and uses `dataclasses.replace`.
Consolidating this would remove three sources of truth for generation and optional-field semantics.

### Related runtime rebind and config load flow

`src/mindroom/api/runtime_reload.py:51` overlaps with `src/mindroom/api/main.py:250` in how a runtime swap preserves cached auth/config state only when `current_snapshot.runtime_paths == target_runtime_paths`, otherwise it clears `auth_state`, `config_data`, `runtime_config`, and `config_load_result`.
It also overlaps with `src/mindroom/api/config_lifecycle.py:609` by calling `_load_config_result`, then publishing `validated_payload`, `runtime_config`, and `ConfigLoadResult` into the committed app snapshot.

This is not a direct duplicate because `reload_api_runtime_config` performs an API-request-style runtime rebind with `expected_snapshot` conflict protection, optional `mutate_runtime`, `api_auth_account_id` refresh, and worker validation cache clearing only after `raise_for_config_load_result` succeeds.
`load_config_into_app` discards stale off-lock load results after a runtime swap at `src/mindroom/api/config_lifecycle.py:617`, while `reload_api_runtime_config` intentionally publishes the target runtime before loading, then stores the failed load result if validation fails.
The stale-write exception string is duplicated with `_stale_snapshot_error` at `src/mindroom/api/config_lifecycle.py:334`, but sharing that helper alone would be minor.

## Proposed generalization

Move one canonical snapshot publisher into `src/mindroom/api/config_lifecycle.py`, because `ApiSnapshot`, `_UNSET`, and most snapshot mutation call sites already live there.
A minimal helper should support optional `runtime_paths`, `auth_state`, and `increment_generation` parameters so `main.initialize_api_app`, `runtime_reload.reload_api_runtime_config`, and existing config lifecycle commit paths can use the same implementation.

No broader refactor is recommended for `reload_api_runtime_config`.
The runtime-rebind flow has enough distinct behavior that extracting it now would likely obscure the concurrency contract.
If refactoring, limit it to a small helper for "preserve snapshot caches only when runtime paths match" and keep the reload/load/error/cache-clear order unchanged.

## Risk/tests

Snapshot publishing is concurrency-sensitive.
Tests should cover generation increments, preserving old optional fields when arguments are omitted, explicitly clearing optional fields with `None`, and preserving or clearing `auth_state` and config caches across same-runtime and changed-runtime swaps.

Existing relevant tests include `tests/api/test_api.py:407`, `tests/api/test_api.py:454`, `tests/api/test_api.py:479`, `tests/api/test_api.py:545`, and `tests/api/test_api.py:590`.
Any consolidation should also run API config tests that exercise config replacement and mutation call sites using `config_lifecycle._published_snapshot`.
