Summary: No meaningful duplication found.
`KnowledgeAvailability` is a small shared enum and is already imported by the main knowledge registry, status, refresh, request-resolution, and API paths instead of being redefined elsewhere.
Related refresh-state string literals exist in `knowledge.registry` and `knowledge.status`, but they represent a different UI/persisted refresh state, not duplicate availability behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
KnowledgeAvailability	class	lines 8-15	none-found	KnowledgeAvailability; READY INITIALIZING STALE REFRESH_FAILED CONFIG_MISMATCH; refresh_failed config_mismatch initializing	src/mindroom/knowledge/status.py:16, src/mindroom/knowledge/status.py:26, src/mindroom/knowledge/registry.py:353, src/mindroom/knowledge/registry.py:567, src/mindroom/knowledge/utils.py:46, src/mindroom/knowledge/utils.py:262, src/mindroom/api/knowledge.py:714, src/mindroom/knowledge/refresh_runner.py:539
```

Findings:
No real duplication was found for `KnowledgeAvailability`.
The enum in `src/mindroom/knowledge/availability.py:8` is the single definition of request-path knowledge availability states.
`src/mindroom/knowledge/registry.py:567` derives `KnowledgeAvailability` values from persisted published-index metadata, while `src/mindroom/knowledge/utils.py:262` and `src/mindroom/knowledge/utils.py:418` consume those enum values for refresh scheduling and user-facing notices.
`src/mindroom/knowledge/status.py:16` defines `_KnowledgeRefreshState` as `Literal["none", "stale", "refreshing", "refresh_failed"]`, which overlaps textually with `STALE` and `REFRESH_FAILED` but models a separate UI refresh state returned by `published_index_refresh_state`.
That is related-only state vocabulary, not a duplicate enum, because refresh state includes `"none"` and `"refreshing"` and excludes `"ready"`, `"initializing"`, and `"config_mismatch"`.

Proposed generalization:
No refactor recommended.
The current split is appropriate: `KnowledgeAvailability` is already factored into its own module and shared by call sites that need request-path availability, while refresh-state literals remain local to the status/registry UI status surface.

Risk/tests:
No production change was made.
If this enum changes later, tests should cover `published_index_availability_for_state`, `resolve_agent_knowledge_access`, `format_knowledge_availability_notice`, refresh-runner failure/result paths, and API reindex error payloads because those are the active consumers checked in this audit.
