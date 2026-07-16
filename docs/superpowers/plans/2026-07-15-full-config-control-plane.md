# Full Config Control Plane Plan

## Problem

MindRoom agents currently have a partial configuration control plane. The deferred
`config_manager` tool can discover agents, teams, models, and tools, but its write
operations expose only a subset of `config.yaml`. When a requested change falls
outside that subset, an agent has to infer whether it should edit YAML with shell,
send the user to a dashboard, or claim the change is unavailable.

An anonymized production-conversation audit found a recurring failure mode rather
than one bad prompt: the agent alternated between those paths, reported completion
before runtime state matched the request, and sometimes expanded a small request
into a much larger reconfiguration. The upstream gap is capability truthfulness:
the agent lacks one authoritative, schema-validated way to inspect and change the
active configuration, and its result does not clearly distinguish persistence
from runtime application.

## Goals

- Let `config_manager` inspect any authored section of the active authored
  configuration without putting the full configuration into every prompt.
- Let it apply an atomic batch of changes to any field accepted by MindRoom's
  `Config` schema, including explicit removal and YAML `null` values.
- Reuse MindRoom's runtime-aware validation and atomic persistence path so invalid
  changes never overwrite the active file.
- Return a compact, truthful receipt: config path, changed paths, validation and
  persistence status, and what runtime state can actually be confirmed.
- Keep existing `manage_agent` and `manage_team` convenience operations intact.
- Keep the control plane lazy: `config_manager` remains deferred, and inspection
  returns only the requested subtree.

## Non-Goals

- Matrix room creation, invitations, membership reconciliation, or other Matrix
  runtime administration.
- OAuth, external integration, credential, or agent-workspace file management.
- Removing shell access or forcing every administration task through this tool.
- Replacing the current dashboard or chat `!config` workflow.
- Editing a configuration composed with YAML `!include`; structured writes already
  reject that case to avoid flattening source files, and this first change will
  preserve that safety boundary with an explicit response on inspection and patch.
- Automatically broadening a user's request into related agent, room, or tool
  changes.

## Proposed Interface

Add one consolidated `manage_config` function to `ConfigManagerTools`:

```python
manage_config(
    operation="inspect" | "patch",
    path="/agents/writer",
    changes=[
        {"op": "replace", "path": "/agents/writer/model", "value": "default"},
        {"op": "remove", "path": "/agents/writer/rooms/0"},
    ],
    dry_run=False,
)
```

Both operations address the authored configuration document: the explicit values
that MindRoom will serialize back to `config.yaml`, with runtime overlays and
unset defaults excluded. An effective default can therefore be visible elsewhere
in MindRoom while absent from this document. The path syntax is RFC 6901 JSON
Pointer. Patch entries use the small relevant
subset of RFC 6902: `add`, `replace`, and `remove`. This avoids dotted-path
ambiguity for Matrix IDs and arbitrary mapping keys, supports multiple atomic
changes, and distinguishes removal from setting a value to `null`.

`add` supports the RFC 6902 `-` token to append to an array. `replace` requires
an existing authored target and its error should suggest `add` when the field is
currently unset.

For `inspect`, `path` defaults to the root but callers should request the smallest
useful subtree. The response identifies the view as authored configuration and
includes YAML plus the resolved active config path. Secret values are not generally
stored in `config.yaml`; nevertheless, inspection output must pass through the
existing shared `redact_sensitive_data` helper rather than a new redaction list.
If the rendered subtree exceeds 20,000 characters, the tool asks for a narrower
pointer instead of injecting an unbounded configuration into the model context.
When the config is composed with `!include`, inspection remains available but
explicitly warns that structured patching is unavailable.

For `patch`, `changes` must be non-empty. The tool will:

1. Load the active config from `runtime_paths.config_path`.
2. Copy `authored_model_dump()` so runtime-only overlays are never persisted.
3. Apply all patch operations to the copy, failing the entire batch on the first
   invalid pointer or operation.
4. Validate the candidate with `Config.validate_with_runtime`.
5. If `dry_run=True`, report the normalized changed paths without writing.
6. Otherwise persist through `validate_and_persist_config_payload`, which validates
   before atomic replacement and publishes matching API snapshots.
7. Return only paths changed by the requested operations. Do not infer or apply
   adjacent changes.

All paths accepted by the `Config` schema are in scope, including plugins, MCP,
tool approval, and authorization. This is capability parity with the existing
administrative agent, which already has shell fallback and can grant tools through
`manage_agent`; routing sensitive edits through validation is safer than refusing
them and forcing a raw file edit. The existing Mind guidance to ask before
authorization or credential changes remains the human-approval boundary. This PR
does not add a second policy layer inside `config_manager`.

## Runtime Receipt

The receipt must not claim that Matrix-side state or in-flight agent objects were
reconciled merely because YAML was saved. It will report:

- `config_path`
- `changed_paths`
- `validated`
- `persisted`
- whether a matching API snapshot was immediately published when that can be
  observed in-process
- a concise note that the orchestrator's normal config reload applies the change
  after the current response when immediate runtime replacement is not observable

No room, agent account, OAuth, or workspace state will be marked complete by this
tool.

## Implementation

1. Add small private JSON Pointer helpers for decoding, traversal, array index
   handling, and atomic application to copied authored data.
2. Reuse `redact_sensitive_data` for inspection output and never include changed
   values in patch receipts.
3. Register and document `manage_config` alongside the three existing consolidated
   functions. Update its catalog description to say that this is full-configuration
   control, then run the repository's tool-metadata synchronization.
4. Catch Pydantic/runtime validation errors with the existing shared invalid-config
   formatter and make all failures explicit that no changes were applied.
5. Preserve the existing include-composition rejection and surface its message.
6. Update the default Mind guidance to use `manage_config` for configuration
   changes and fall back to direct file editing only when the tool explicitly
   refuses an include-composed write.
7. Update focused config-manager tests and generated tool metadata through the
   repository's established workflow.

## Tests

- Inspect the root and nested mapping/list/scalar pointers.
- Escape `/` and `~` in mapping keys per RFC 6901.
- Append to an array with `-` and make replace-on-missing suggest `add`.
- Redact credential-like values from inspection output.
- Atomically add, replace, and remove fields across root config sections.
- Set an optional value to `null` without treating it as removal.
- Reject missing paths, invalid list indices, unsupported operations, and malformed
  patch entries without modifying the file.
- Reject a batch when the final `Config` is invalid and leave the original bytes
  untouched.
- Validate a dry run without modifying the file.
- Preserve runtime-injected overlays and existing inline tool overrides.
- Return an explicit include-composition error instead of flattening `!include`
  files.
- Keep existing `manage_agent`, `manage_team`, and `get_info` behavior passing.

## Review Gate

Before implementation, an independent Claude Code review must inspect this plan,
the relevant source and tests, and the anonymized production evidence. Any material
disagreement on scope, safety, interface, or runtime semantics must be reconciled
in a follow-up review. Implementation starts only once both reviews converge on
the same concrete design.

## Known Limitations

- Like `manage_agent` and `manage_team`, the tool loads and later persists without
  an optimistic generation guard. A concurrent writer can win between those steps.
  Adding concurrency control across non-API writers is separate lifecycle work.
- Config inspection tolerates plugin-load errors so an agent can diagnose a broken
  setup, while persistence validates strictly. A patch can therefore be rejected
  until an existing plugin error is fixed.

## Review Outcome

Claude Code Fable 5 independently audited the code and anonymized conversation
corpus. After one reconciliation round, both reviews converged on full-schema
coverage without an internal blocklist, shared redaction, authored-document
semantics, explicit include boundaries, truthful receipts, prompt alignment, and
the known limitations above.
