# Explicit Turn Record Normalization

## Problem

`TurnRecord` is an immutable durable aggregate whose `__post_init__` method currently normalizes every field after construction.
The method spans roughly 95 lines and mutates the frozen dataclass through 23 `object.__setattr__` calls.
The same normalization also runs implicitly after every `dataclasses.replace()` call.
This makes construction and state transitions difficult to reason about because validation, coercion, and cross-field invariant repair are hidden behind ordinary dataclass operations.

## Goals

- Remove `TurnRecord.__post_init__` and its magic frozen-field assignments.
- Make permissive input normalization explicit at runtime and persistence boundaries.
- Keep `TurnRecord` immutable once constructed.
- Preserve the current durable ledger and run-metadata schemas.
- Preserve current malformed-record and recovery behavior.
- Make cross-field state transitions visible and independently testable.

## Non-Goals

- Change the meaning or lifecycle of handled turns.
- Change ledger persistence, locking, retry, or cleanup behavior.
- Split the flat persisted schema into nested records.
- Introduce a validation framework or new dependency.

## Design

`TurnRecord` becomes a plain frozen dataclass containing canonical values only.
It will not normalize itself after construction.

The model and its cohesive normalization helpers move from `handled_turns.py` into a focused `turn_record.py` module.
The existing `TurnRecord.create(...)` API remains the explicit permissive construction boundary for runtime values.
Its implementation delegates to pure helpers grouped around source identity and redaction, delivery state, dispatch receipt ordering, command state, and contextual metadata.
Those helpers compute canonical values before the generated dataclass constructor runs.

`TurnRecordCodec` remains responsible for strict persisted-data shape validation.
After validating the physical shape, it passes values through the explicit record factory so persisted and runtime construction share one canonicalization policy.
Malformed persisted records continue to return no record rather than entering runtime state.

Every `dataclasses.replace()` operation involving `TurnRecord` will be audited.
Independent changes that already provide canonical typed values may remain ordinary immutable replacements.
Changes involving coupled invariants will use named transition functions that construct a canonical result explicitly.
The coupled invariants include source and discovery identity, redaction pruning, visible-echo fallback linkage, dispatch receipt ordering, and command execution checkpoints.

The disk representation remains flat and byte-compatible at the schema level.
No migration or compatibility branch is required.

## Error Handling

Runtime factory inputs retain the current tolerant normalization behavior.
Invalid strings, timestamps, mappings, and contextual objects normalize exactly as they do today.
Persisted records retain their stricter outer-shape checks and fail closed when required fields are malformed.
Transition functions reject invalid domain operations where the current code already raises, such as non-positive STOP receipt orders.

## Testing

Focused tests will pin each normalization group and the invariants affected by record transitions.
Existing handled-turn persistence and reload tests will verify schema compatibility.
Turn-store, command execution, edit regeneration, visible-response reconciliation, and dispatch-obligation tests will cover affected integration paths.
The relevant lint, type, and import-boundary checks will run before the branch is published.

## Success Criteria

- `TurnRecord` has no `__post_init__` method.
- No `object.__setattr__` call is needed to construct or update a turn record.
- Input normalization occurs only at named boundaries.
- Invariant-sensitive updates are explicit in code.
- Existing durable records load without migration.
- Relevant tests and repository checks pass.
