## Summary

Top duplication candidate: PostgreSQL URL redaction in `src/mindroom/matrix/cache/postgres_redaction.py` overlaps with generic URL credential and secret-query redaction in `src/mindroom/tool_system/tool_calls.py` and with Git URL userinfo redaction in `src/mindroom/knowledge/redaction.py`.
The overlap is real for URL userinfo masking and query secret masking, but the PostgreSQL module also supports libpq keyword/value conninfo strings and preserves non-secret query parameters, so a broad consolidation is not recommended without careful parameterization.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_redact_url_query	function	lines 17-26	duplicate-found	parse_qsl quote_plus urlencode password passfile sslpassword query redaction	src/mindroom/tool_system/tool_calls.py:275-286; tests/test_tool_calls.py:108-164; tests/test_event_cache_backends.py:986-995
_redact_url_conninfo	function	lines 29-42	duplicate-found	urlsplit urlunsplit userinfo redaction redact_url_credentials postgresql	src/mindroom/tool_system/tool_calls.py:261-290; src/mindroom/knowledge/redaction.py:22-41; tests/test_knowledge_manager.py:6429-6445; tests/test_event_cache_backends.py:986-995
_redact_libpq_conninfo	function	lines 45-46	none-found	libpq conninfo password= passfile= sslpassword= assignment regex	src/mindroom/tool_system/tool_calls.py:228-244; tests/test_tool_calls.py:178-264; tests/test_event_cache_backends.py:994-995
redact_postgres_connection_info	function	lines 49-53	related-only	redact_postgres_connection_info redacted_database_url database url redaction	src/mindroom/runtime_support.py:57-61; src/mindroom/matrix/cache/postgres_event_cache.py:374-376; src/mindroom/tool_system/tool_calls.py:293-300; src/mindroom/knowledge/redaction.py:44-70
```

## Findings

### 1. URL credential redaction is duplicated with different policy scopes

- Primary behavior: `src/mindroom/matrix/cache/postgres_redaction.py:17-42` parses PostgreSQL URLs, replaces any netloc userinfo with `***`, and redacts query keys `password`, `passfile`, and `sslpassword` while preserving the rest of the URL.
- Similar behavior: `src/mindroom/tool_system/tool_calls.py:261-290` parses HTTP(S) URLs, masks userinfo and secret query parameters, and rebuilds the URL only when a change occurred.
- Similar behavior: `src/mindroom/knowledge/redaction.py:22-41` parses any URL scheme, replaces all userinfo with `***`, and strips path params, query, and fragment for Git-safe display.

Why this is duplicated: all three functions perform the same core operation of parsing URLs, removing credential-bearing userinfo, and returning a log-safe URL string.
The tool-system helper also duplicates the query-loop shape from `_redact_url_query`: parse query items, check key secrecy, replace secret values, and re-encode.

Differences to preserve:

- PostgreSQL redaction applies to PostgreSQL connection URLs and treats only `password`, `passfile`, and `sslpassword` as query secrets.
- Tool failure redaction applies only to HTTP(S), preserves username when a `user:password@host` pair exists, uses `***redacted***`, and has broader cloud/API query-secret detection.
- Knowledge URL redaction intentionally removes query and fragment entirely because Git URLs are used as identities/display values, not connection strings.
- PostgreSQL redaction always rebuilds the query with `quote_plus`, even if no secret key was present.

### 2. libpq conninfo redaction is specialized and not duplicated

- Primary behavior: `src/mindroom/matrix/cache/postgres_redaction.py:45-46` redacts libpq keyword assignments for `password`, `passfile`, and `sslpassword`, including quoted values.
- Related behavior: `src/mindroom/tool_system/tool_calls.py:228-244` redacts generic secret assignments inside arbitrary failure text.

Why this is not a duplicate: the generic tool-system regex is broader and recursive through `sanitize_failure_text`, while `_redact_libpq_conninfo` is intentionally a small PostgreSQL-specific pass over libpq connection strings.
The primary helper also removes quotes around redacted quoted libpq values, as covered by `tests/test_event_cache_backends.py:994-995`, while tool-system assignment redaction preserves quotes for generic text.

### 3. Public PostgreSQL redaction dispatch is related to generic sanitizers, but not equivalent

- Primary behavior: `src/mindroom/matrix/cache/postgres_redaction.py:49-53` dispatches URL-style conninfo to URL redaction and otherwise uses libpq redaction.
- Related behavior: `src/mindroom/tool_system/tool_calls.py:293-300` and `src/mindroom/knowledge/redaction.py:44-70` expose higher-level text sanitizers that scan embedded URLs and auth headers.

Why this is related only: the PostgreSQL entry point accepts one connection string and must preserve it as a usable diagnostic location, while the generic sanitizers accept free-form error text and apply much broader redaction/truncation policies.

## Proposed Generalization

No immediate refactor recommended for this module.

If this duplication grows, the smallest safe generalization would be a private URL-redaction helper that accepts:

- a scheme policy, such as all schemes or only HTTP(S);
- a secret query-key predicate;
- a replacement token;
- a userinfo policy, such as redact all userinfo or preserve username;
- query/fragment handling, such as preserve, redact selected query values, or strip all.

That helper could live near the generic redaction utilities, but it should not absorb libpq conninfo parsing unless another PostgreSQL/libpq caller appears.

## Risk/tests

Behavior risks for any consolidation:

- Changing PostgreSQL query encoding could alter diagnostics for connection URLs with blank values, repeated parameters, or spaces.
- Reusing generic tool failure redaction would change the replacement token from `***` to `***redacted***` unless parameterized.
- Reusing knowledge URL redaction would incorrectly strip PostgreSQL query options like `sslmode=require`.
- Reusing generic assignment redaction for libpq strings could preserve quotes around redacted secrets, changing existing expected output.

Tests needing attention if refactored:

- `tests/test_event_cache_backends.py:986-995` for PostgreSQL URL and libpq redaction behavior.
- `tests/test_event_cache_backends.py:1006-1014` for runtime identity redaction.
- `tests/test_tool_calls.py:102-164` and `tests/test_tool_calls.py:178-264` for generic URL/query/assignment sanitization.
- `tests/test_knowledge_manager.py:6429-6445` for Git URL credential redaction.
