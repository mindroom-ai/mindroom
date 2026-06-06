# Room Authorization Identity Report

## Scope

Reviewed Matrix room identity and authorization lookup in `src/mindroom/authorization.py`.
Reviewed focused tests in `tests/test_authorization.py`, `tests/test_matrix_room_access.py`, `tests/test_tool_execution_identity_payloads.py`, and `tests/test_stale_stream_cleanup.py`.

## Validated Findings

### AUTHZ-AUTHZ-1

Confirmed.
`is_authorized_sender` accepted `room.canonical_alias` as an authorization permission key source.
Matrix canonical aliases are mutable room state, so an arbitrary room could claim a protected alias such as `#lobby:example.com` and unlock `room_permissions` keyed by that alias or its managed room key.

Fix applied.
Room authorization now checks stable `room_id` first and derives room key/full-alias permission keys only from persisted MindRoom `MatrixState` for the same room ID.
Direct alias targets such as `#lobby:example.com` still work because that is the target identifier, not mutable room state from an unrelated room.

### AUTHZ-AUTHZ-3

Validated as residual operator-policy risk, not attacker-controlled lookup bypass.
Glob matching is limited to `authorization.agent_reply_permissions`, after bridge aliases resolve to canonical user IDs.
The only wildcard that grants universal reply permission is explicit operator config entry `"*"`.
Domain patterns such as `"*:example.com"` remain intentional behavior and have tests.

Residual risk.
Operators can still write overly broad glob patterns.
That is configuration policy risk rather than attacker-controlled identity mapping.

### AUTHZ-AUTHZ-4

Confirmed local issue in the shared helper.
`get_effective_sender_id_for_reply_permissions` promoted `com.mindroom.original_sender` for any current internal sender without requiring trusted `source_kind`.
`TurnController` already had a stronger source-kind/requester guard, but `stale_stream_cleanup` calls the shared helper directly.

Fix applied.
The helper now requires `source_kind_allows_trusted_original_sender(source_kind_from_content(content))` before promoting `original_sender`.
Existing trusted relay behavior remains for voice, hook, scheduled, hook dispatch, and trusted internal relay source kinds.
The stale stream cleanup test fixture now models trusted relayed edit sidecar content with `SOURCE_KIND_KEY=trusted_internal_relay`.

## Bridge And Federation Identity

Bridge alias mapping still resolves only through operator-authored `authorization.aliases`.
Cross-domain internal-looking Matrix IDs remain rejected by existing tests.
Persisted current internal accounts remain trusted by exact current Matrix ID, including username drift and non-default domains.
No new federation trust is added.

## Tests

Red tests were observed before the fix:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest tests/test_authorization.py::test_room_specific_permissions_ignore_unmanaged_canonical_alias tests/test_authorization.py::test_effective_sender_ignores_original_sender_without_trusted_source_kind -q
```

Expected failures:

```text
test_room_specific_permissions_ignore_unmanaged_canonical_alias: assert not True
test_effective_sender_ignores_original_sender_without_trusted_source_kind: '@alice:example.com' != '@mindroom_assistant:example.com'
```

Green verification:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest tests/test_authorization.py -n 0 --no-cov -q
```

Result:

```text
54 passed in 0.41s
```

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest tests/test_authorization_config_update.py tests/test_matrix_room_access.py tests/test_tool_execution_identity_payloads.py -n 0 --no-cov -q
```

Result:

```text
44 passed in 0.56s
```

Final combined verification after docstring cleanup:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest tests/test_authorization.py tests/test_authorization_config_update.py tests/test_matrix_room_access.py tests/test_tool_execution_identity_payloads.py -n 0 --no-cov -q
```

Result:

```text
98 passed in 0.62s
```

Final combined verification after stale cleanup fixture update:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest tests/test_authorization.py tests/test_authorization_config_update.py tests/test_matrix_room_access.py tests/test_tool_execution_identity_payloads.py tests/test_stale_stream_cleanup.py -n 0 --no-cov -q
```

Result:

```text
151 passed in 3.28s
```

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run ruff check src/mindroom/authorization.py tests/test_authorization.py tests/test_stale_stream_cleanup.py
```

Result:

```text
All checks passed!
```

Isolated PR worktree verification was rerun from `/Users/bas.nijholt/.codex/worktrees/room-authz-identity-2/mindroom`:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv sync --all-extras
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest tests/test_authorization.py tests/test_authorization_config_update.py tests/test_matrix_room_access.py tests/test_tool_execution_identity_payloads.py tests/test_stale_stream_cleanup.py -n 0 --no-cov -q
/Users/bas.nijholt/Library/Python/3.9/bin/uv run ruff check src/mindroom/authorization.py tests/test_authorization.py tests/test_stale_stream_cleanup.py
```

Result:

```text
uv sync --all-extras: success
151 passed in 3.28s
All checks passed!
```

Pre-commit was feasible to run but did not pass because of unrelated repo/environment blockers:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pre-commit run --all-files
```

Result:

```text
ruff-check: src/mindroom/mcp/toolkit.py ANN401 on value: Any and return Any
typescript-check: /run/current-system/sw/bin/bash: line 1: bun: command not found
check-bun-lock: /run/current-system/sw/bin/bash: line 1: bun: command not found
```

Full-suite check was attempted:

```bash
/Users/bas.nijholt/Library/Python/3.9/bin/uv run pytest -n auto --no-cov --tb=short
```

Result before the stale cleanup fixture update:

```text
53 failed, 7704 passed, 60 skipped
```

The stale-stream cleanup failure from that run was fixed and covered by `tests/test_stale_stream_cleanup.py`.
The remaining full-suite failures were outside this worker scope and concentrated in API auth/config/plugin/scheduling/sandbox tests already dirty or unrelated to Matrix room identity authorization.

## Environment Notes

`uv` was installed under `/Users/bas.nijholt/Library/Python/3.9/bin/uv` but was not on `PATH`.
`/Users/bas.nijholt/.codex/worktrees/c31e/mindroom/.git` points to a different worktree root, so `git status` from this directory does not reliably reflect these filesystem edits.
`git-lfs` was not on `PATH`; initial worktree checkout and exact-path restore both reported the post-checkout hook error after completing file operations.
The shared git config has `core.worktree=/Users/bas.nijholt/.codex/worktrees/security-memory-context-framing/mindroom`, so branch status/add/commit commands for the PR worktree used explicit `--git-dir` and `--work-tree`.
