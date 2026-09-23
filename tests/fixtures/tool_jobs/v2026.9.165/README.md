# Released tool-job snapshots

These frozen JSON fixtures use the native `BackgroundJob`, `_BackgroundDelivery`,
`ToolExecutionIdentity`, and `DelegationChild` dataclass definitions from tag
`v2026.9.165`. They were serialized once with synthetic identities and values.
They are not generated from the current writer during tests.

- `completed.json`: a completed ordinary job with an acknowledged notification,
  unread result, and the released constructor authority shape (no configuration signature).
- `paused.json`: execution held by the retired human-pause mechanism.
- `native.json`: a finished child whose result had not yet reached its parent job.

The old feature was removed in `v2026.9.166`. Current recovery preserves exact
execution receipts and available outcomes, without inventing missing source IDs
or constructor settings and without restarting a tool.
