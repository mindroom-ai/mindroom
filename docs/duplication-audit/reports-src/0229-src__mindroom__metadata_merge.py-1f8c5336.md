## Summary

No meaningful duplication found.
`deep_merge_metadata` is the shared implementation for recursive, non-mutating metadata merges and is already reused by AI, team, OpenAI-compatible API, and history compaction paths.
The nearest related helper is `src/mindroom/codex_model.py::_merge_dict_data`, but it intentionally performs in-place response-delta accumulation with list extension semantics rather than recursive metadata replacement.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
deep_merge_metadata	function	lines 9-31	related-only	deep_merge metadata merge merge.*metadata isinstance dict deepcopy for key value items	src/mindroom/codex_model.py:418; src/mindroom/dispatch_handoff.py:224; src/mindroom/history/storage.py:307; src/mindroom/history/compaction.py:663; src/mindroom/ai.py:1023; src/mindroom/teams.py:1513; src/mindroom/api/openai_compat.py:1505
```

## Findings

No real duplicated recursive metadata merge was found under `src`.

Related but distinct behavior:

- `src/mindroom/codex_model.py:418` defines `_merge_dict_data`, which merges response delta dictionaries into an existing `ModelResponse`.
  It overlaps with `deep_merge_metadata` only at the broad level of combining optional dictionaries.
  The behavior is different because `_merge_dict_data` mutates `target_data`, appends lists in place, returns the original target when the delta is `None`, and does not recursively merge nested dictionaries.
- `src/mindroom/dispatch_handoff.py:224` defines `merge_payload_metadata`, but this fills missing fields on a typed `DispatchPayloadMetadata` object and conditionally trusts hydrated internal metadata.
  It is field-specific reconciliation, not generic dictionary metadata merging.
- `src/mindroom/history/storage.py:307` defines `metadata_with_merged_seen_event_ids`, which unions Matrix seen-event state after a generic metadata merge has already happened.
  Its call site at `src/mindroom/history/compaction.py:663` correctly composes it with `deep_merge_metadata` instead of duplicating recursive merge behavior.

Existing reuse of `deep_merge_metadata` appears appropriate:

- `src/mindroom/ai.py:1023`, `src/mindroom/ai.py:1188`, `src/mindroom/ai.py:1212`, `src/mindroom/ai.py:1518`, `src/mindroom/ai.py:1741`, and `src/mindroom/ai.py:1765` use it to combine Matrix run metadata with prepared-history metadata content.
- `src/mindroom/teams.py:1513` uses it for the analogous team execution path.
- `src/mindroom/api/openai_compat.py:1505` uses it while combining metadata content for OpenAI-compatible API requests.
- `src/mindroom/history/compaction.py:664` uses it before preserving seen-event ID unions.

## Proposed Generalization

No refactor recommended.
`src/mindroom/metadata_merge.py` is already the focused helper module for recursive metadata merging.
The related helpers preserve domain-specific behavior that should not be folded into `deep_merge_metadata` without changing semantics.

## Risk/Tests

No code change is recommended, so no production test changes are needed.
If future work changes `deep_merge_metadata`, focused tests should cover `None` handling, non-mutating deep copies, nested dictionary recursion, replacement of non-dict values, and preservation of history compaction seen-event ID merging.
