# Duplication Audit: `src/mindroom/knowledge/redaction.py`

## Summary

Top duplication candidates:

1. Credential-free Git URL normalization is duplicated across `knowledge/redaction.py`, `knowledge/manager.py`, and `config/main.py`.
2. URL credential redaction overlaps with tool-call failure sanitization and PostgreSQL connection-string redaction, but those modules preserve different details and should not be merged wholesale.
3. Free-form credential text redaction is related to `tool_system/tool_calls.py::sanitize_failure_text`, with different scope and redaction contracts.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_strip_path_params	function	lines 18-19	duplicate-found	path.split(';', 1)[0], params='', query='', fragment=''	src/mindroom/config/main.py:294; src/mindroom/knowledge/manager.py:366
redact_url_credentials	function	lines 22-41	duplicate-found	redact URL credentials, urlparse netloc @, urlunparse, strip query fragment params	src/mindroom/tool_system/tool_calls.py:261; src/mindroom/matrix/cache/postgres_redaction.py:29; src/mindroom/knowledge/manager.py:361; src/mindroom/config/main.py:289
redact_credentials_in_text	function	lines 44-70	related-only	free-form credential redaction, URL_PATTERN, Authorization Bearer Basic, sanitize_failure_text	src/mindroom/tool_system/tool_calls.py:293; src/mindroom/knowledge/manager.py:694; src/mindroom/knowledge/refresh_runner.py:540; src/mindroom/api/knowledge.py:261
redact_credentials_in_text.<locals>._redact_authorization_header	nested_function	lines 48-63	related-only	Authorization Basic Bearer redaction, b64decode token, decoded Basic secret replacement	src/mindroom/tool_system/tool_calls.py:37; src/mindroom/tool_system/tool_calls.py:253; src/mindroom/knowledge/manager.py:456; tests/test_knowledge_manager.py:5358
credential_free_url_identity	function	lines 73-95	duplicate-found	credential-free repo URL identity, passwordless ssh username, strip query fragment, sha256 repo identity	src/mindroom/knowledge/manager.py:361; src/mindroom/config/main.py:289; tests/test_knowledge_manager.py:6482
embedded_http_userinfo	function	lines 98-105	none-found	embedded HTTP userinfo, parsed.username parsed.password unquote, http https only	src/mindroom/knowledge/manager.py:474; src/mindroom/knowledge/utils.py:225; src/mindroom/knowledge/manager.py:431
```

## Findings

### 1. Git URL credential-free normalization is repeated

`src/mindroom/knowledge/redaction.py:73` computes a secret-free normalized repo URL and hashes it for durable identity.
The same normalization behavior appears as concrete URL-returning helpers in `src/mindroom/knowledge/manager.py:361` and `src/mindroom/config/main.py:289`.

The shared behavior is:

- Parse a repo URL with `urlparse`.
- Return raw input when no scheme or netloc exists.
- Strip `;params`, query, and fragment.
- Remove credential-bearing userinfo from `netloc`.
- Preserve passwordless SSH usernames because `ssh://git@example.com/repo.git` and `ssh://deploy@example.com/repo.git` are different clone identities.

Differences to preserve:

- `credential_free_url_identity()` lowercases scheme and host/userinfo where applicable and returns `repo-url-sha256:<digest>`.
- `knowledge.manager._credential_free_repo_url()` returns the cleaned URL for Git config and clone operations.
- `config.main._credential_free_repo_url_for_config_validation()` returns the cleaned URL for duplicate-root source validation and currently does not lowercase host/userinfo.

This is real duplication because tests assert the same passwordless-SSH and secret-bearing-userinfo rules in `tests/test_knowledge_manager.py:6429`, `tests/test_knowledge_manager.py:6456`, `tests/test_knowledge_manager.py:6466`, and `tests/test_knowledge_manager.py:6482`.

### 2. URL credential redaction overlaps with other sanitizers

`src/mindroom/knowledge/redaction.py:22` redacts userinfo for any parsed URL scheme and strips params, query, and fragment.
`src/mindroom/tool_system/tool_calls.py:261` also parses URLs and redacts userinfo for failure logging.
`src/mindroom/matrix/cache/postgres_redaction.py:29` does the same netloc `@` replacement for PostgreSQL URLs.

Differences to preserve:

- Knowledge Git redaction hides the entire userinfo as `***@host`, including usernames, and strips all query and fragment data.
- Tool-call redaction only handles `http` and `https`, preserves non-secret query parameters, redacts known query secret keys, and preserves a username when a password is present.
- PostgreSQL redaction is domain-specific and also redacts libpq assignment syntax and selected URL query keys.

This is duplicated behavior at the lower-level "remove credential-bearing URL userinfo" step, but the public sanitizers have intentionally different scope and output formats.

### 3. Free-form credential redaction is related to tool-call sanitization

`src/mindroom/knowledge/redaction.py:44` redacts credential-bearing URLs and `Authorization: Basic|Bearer` headers inside Git errors.
`src/mindroom/tool_system/tool_calls.py:293` sanitizes failure text more broadly, including URL credentials, bearer-token phrasing, API-key messages, token-like provider keys, and assignment expressions.

Differences to preserve:

- Knowledge redaction decodes valid Basic auth values and removes both `username:secret` and the secret from the rest of the text.
- Tool-call sanitization does not decode Basic auth headers and uses the `***redacted***` marker.
- Knowledge redaction returns `Authorization: <scheme> ***`, matching tests at `tests/test_knowledge_manager.py:5398`.

This is related but not a direct duplicate because the knowledge path is specifically protecting Git subprocess output produced by `src/mindroom/knowledge/manager.py:456`.

## Proposed Generalization

1. Add a small pure helper in `src/mindroom/knowledge/redaction.py`, for example `credential_free_repo_url(value: str, *, lowercase_identity: bool = False) -> str`, that returns the normalized URL before hashing.
2. Update `credential_free_url_identity()` to call that helper and hash its result.
3. Replace `knowledge.manager._credential_free_repo_url()` with an import of the shared helper, preserving its current non-hashed return contract.
4. Replace `config.main._credential_free_repo_url_for_config_validation()` with the same helper only if the current case sensitivity is acceptable or explicitly preserved by a parameter.
5. Leave tool-call and PostgreSQL sanitizers separate unless a later refactor extracts only a tiny `redact_url_userinfo(netloc, marker)` helper with explicit formatting parameters.

No production refactor was performed for this task.

## Risk/tests

Main risk is accidentally changing Git source identity semantics.
Tests should cover:

- Passwordless SSH usernames remain identity-bearing.
- Secret-bearing SSH, HTTP, HTTPS, and `git+https` userinfo are removed.
- URL params, query, and fragments are stripped for Git identity and knowledge Git redaction.
- Duplicate-root config validation still accepts and rejects the same URL combinations.
- Git subprocess errors still redact `Authorization: Basic ***`, decoded Basic credentials, and bearer values.

The URL sanitizer overlap with tool-call and PostgreSQL redaction should be left alone unless covered by both `tests/test_tool_calls.py` and PostgreSQL connection-info tests, because their redaction formats and query-preservation rules intentionally differ.
