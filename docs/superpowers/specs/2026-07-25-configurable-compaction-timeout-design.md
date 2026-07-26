# Configurable Compaction Timeout

## Goal

Allow deployments and individual history scopes to choose how long one compaction summary request may run.

The default timeout is 600 seconds so large-context Opus compactions can finish without forcing smaller summary chunks.

## Configuration

Add `timeout_seconds` to both `CompactionConfig` and `CompactionOverrideConfig`.

`defaults.compaction.timeout_seconds` defaults to `600.0`.

Agent and team `compaction.timeout_seconds` values inherit through the existing compaction override mechanism.

Values must be greater than zero.

Example:

```yaml
defaults:
  compaction:
    timeout_seconds: 600
```

## Runtime Behavior

The resolved history execution plan carries the effective timeout to the compaction runtime.

Every summary attempt in that compaction operation uses the same resolved timeout for provider tuning, local timeout enforcement, and structured timing logs.

An explicitly shorter timeout already authored in a provider model remains a stricter provider-level cap.

No summary-input sizing, chunk selection, retry count, or fallback-model behavior changes.

## Validation

Tests cover the 600-second default, positive-value validation, scope inheritance and override, runtime propagation into summary generation, and preservation of shorter provider timeouts.

Configuration documentation and generated skill references list the new option.
