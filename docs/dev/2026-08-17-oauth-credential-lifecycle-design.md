# OAuth Credential Lifecycle Design

This record explains why one SQLite database per credential scope owns the lifecycle of MindRoom-managed OAuth credentials, and which invariants that lifecycle must keep.

## Goal

MindRoom-managed OAuth credentials must remain correct when refresh grants rotate or fail, callbacks race resets, callers are cancelled, or the process stops unexpectedly.

## Why one store owns the lifecycle

Splitting OAuth state across credential JSON, generation metadata, reset intents, provider wrappers, and MCP sessions was rejected because each extra persistence boundary required recovery rules for partial commits.
One SQLite transaction per canonical scope holds the encoded credential payload, its revisions, and completed browser-reset receipts, so a state transition is atomic instead of being reconstructed after a partial multi-file write.

## Invariants

- Provider refresh and authorization-code exchange stay inside the scope's write transaction until the matching local commit succeeds, because the provider may rotate the refresh grant: later same-scope callers then observe the committed rotation and do not consume the same grant again.
  For the same reason, a commit blocked by a reader is retried inside the same transaction and never repeats provider I/O, and an admitted refresh runs to completion even when its caller is cancelled.
- Resetting never requires decoding the stored payload, so a corrupt or wrong-key credential stays resettable, and unreadable ciphertext stays in place so restoring the correct key recovers it.
- MCP credential-scope sessions are retired before a reset commits, so captured stale state cannot reconnect before the reset lands.

## Non-goals

This design does not revoke grants at external providers.
It does not redesign OAuth discovery or dynamic client registration.
It does not globally canonicalize Matrix identities.
It does not exempt the reset tool from normal operator-configured tool approval policy.
