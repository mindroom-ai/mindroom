# Configurable Compaction Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an inherited `compaction.timeout_seconds` option with a 600-second default and use it for every compaction summary attempt.

**Architecture:** Extend the existing concrete and override compaction models, carry the resolved value in `ResolvedHistoryExecutionPlan`, and pass it through the history runtime to the existing summary-call timeout seam. Keep provider-authored shorter timeouts as stricter caps and leave input sizing, chunking, retries, and fallback selection unchanged.

**Tech Stack:** Python 3.13, Pydantic configuration models, asyncio, pytest, MkDocs source documentation.

## Global Constraints

The default compaction summary timeout is `600.0` seconds.

`defaults.compaction.timeout_seconds` is inherited by agent and team compaction overrides.

An agent or team may override the timeout with any value greater than zero.

Every attempt in one compaction operation uses the resolved timeout for provider tuning, local timeout enforcement, and structured logs.

Explicit shorter provider timeouts remain stricter caps.

No summary-input sizing, chunk selection, retry count, or fallback-model behavior changes.

---

### Task 1: Configuration schema and inheritance

**Files:**
- Modify: `src/mindroom/config/models.py`
- Modify: `tests/test_config_entity_view.py`
- Modify: `tests/test_history_replay_planning.py`

**Interfaces:**
- Consumes: Existing `CompactionConfig`, `CompactionOverrideConfig`, and `Config.resolve_entity`.
- Produces: `CompactionConfig.timeout_seconds: float` and `CompactionOverrideConfig.timeout_seconds: float | None`.

- [ ] **Step 1: Write failing schema and inheritance tests**

Add assertions that the concrete default is `600.0`, non-positive values fail validation, the defaults-only and inheriting scopes resolve the authored default, and an agent override wins:

```python
assert CompactionConfig().timeout_seconds == 600.0
with pytest.raises(ValidationError):
    CompactionConfig(timeout_seconds=0)

defaults = CompactionConfig(timeout_seconds=420.0)
override = CompactionOverrideConfig(timeout_seconds=75.0)
assert config.resolve_entity("inheriting_agent").compaction_config.timeout_seconds == 420.0
assert config.resolve_entity("overriding_agent").compaction_config.timeout_seconds == 75.0
```

- [ ] **Step 2: Run tests and verify the missing fields fail**

Run:

```bash
uv run pytest --cache-clear -x --lf tests/test_config_entity_view.py tests/test_history_replay_planning.py -n 0 --no-cov -q
```

Expected: failure because the compaction models do not accept or expose `timeout_seconds`.

- [ ] **Step 3: Add the validated fields**

Add these fields:

```python
class CompactionOverrideConfig(BaseModel):
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Maximum seconds allowed for each compaction summary request",
    )


class CompactionConfig(BaseModel):
    timeout_seconds: float = Field(
        default=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
        gt=0,
        description="Maximum seconds allowed for each compaction summary request",
    )
```

Import `MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS` beside the existing constants import.

The existing `model_dump(exclude_unset=True)` merge path should inherit and override the new scalar without special-case logic.

- [ ] **Step 4: Run schema and inheritance tests**

Run:

```bash
uv run pytest --cache-clear -x --lf tests/test_config_entity_view.py tests/test_history_replay_planning.py -n 0 --no-cov -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/config/models.py tests/test_config_entity_view.py tests/test_history_replay_planning.py
git commit -m "Add configurable compaction timeout"
```

### Task 2: Runtime timeout propagation

**Files:**
- Modify: `src/mindroom/history/types.py`
- Modify: `src/mindroom/history/policy.py`
- Modify: `src/mindroom/history/runtime.py`
- Modify: `src/mindroom/history/compaction.py`
- Modify: `src/mindroom/constants.py`
- Modify: `tests/test_compaction_invariants.py`
- Modify: `tests/test_history_replay_planning.py`

**Interfaces:**
- Consumes: `CompactionConfig.timeout_seconds` from Task 1.
- Produces: `ResolvedHistoryExecutionPlan.compaction_timeout_seconds: float` and a `summary_timeout_seconds` keyword flowing into `generate_compaction_summary(..., timeout_seconds=...)`.

- [ ] **Step 1: Write failing plan and summary-attempt tests**

Assert that the resolved plan carries an authored timeout and that a compaction attempt passes the same value to summary generation:

```python
assert execution_plan.compaction_timeout_seconds == 420.0

assert generate_summary.await_args.kwargs["timeout_seconds"] == 420.0
assert request_log["timeout_seconds"] == 420.0
```

Keep the existing provider tests, but assert that a model with a 3,600-second provider timeout is capped at the 600-second default while a model authored with 300 seconds remains at 300 seconds.

- [ ] **Step 2: Run tests and verify propagation is missing**

Run:

```bash
uv run pytest --cache-clear -x --lf tests/test_compaction_invariants.py tests/test_history_replay_planning.py -n 0 --no-cov -q
```

Expected: failure because the execution plan and compaction call do not carry the configured timeout.

- [ ] **Step 3: Carry the timeout through the execution plan**

Add the field and populate it:

```python
@dataclass(frozen=True)
class ResolvedHistoryExecutionPlan:
    compaction_timeout_seconds: float


return ResolvedHistoryExecutionPlan(
    ...
    compaction_timeout_seconds=compaction_config.timeout_seconds,
)
```

- [ ] **Step 4: Pass the timeout through runtime and compaction**

Add `summary_timeout_seconds: float` at the compaction boundary and thread it through `_rewrite_working_session_for_compaction` and `_generate_compaction_summary_with_retry`.

Use the value for every attempt:

```python
summary = await generate_compaction_summary(
    model=model,
    summary_input=summary_input,
    summary_prompt=summary_prompt,
    timeout_seconds=summary_timeout_seconds,
)
```

Replace compaction log uses of the global constant with `summary_timeout_seconds`.

- [ ] **Step 5: Run compaction tests**

Run:

```bash
uv run pytest --cache-clear -x --lf tests/test_history_summary_call.py tests/test_compaction_invariants.py tests/test_history_replay_planning.py -n 0 --no-cov -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/constants.py src/mindroom/history/types.py src/mindroom/history/policy.py src/mindroom/history/runtime.py src/mindroom/history/compaction.py tests/test_compaction_invariants.py tests/test_history_replay_planning.py
git commit -m "Use resolved timeout for compaction summaries"
```

### Task 3: Documentation and verification

**Files:**
- Modify: `docs/configuration/agents.md`
- Modify: `docs/configuration/teams.md`
- Modify: `docs/dev/agent_configuration.md`
- Modify generated files selected by the documentation hook under `skills/mindroom-docs/references/`

**Interfaces:**
- Consumes: The final `timeout_seconds` configuration contract.
- Produces: User-facing configuration examples and generated skill references.

- [ ] **Step 1: Document the option**

Add `timeout_seconds: 600` to default and scoped compaction examples.

State that the value limits each summary request, defaults to 600 seconds, applies to retries and fallback attempts, and does not override a shorter provider-level timeout.

- [ ] **Step 2: Run focused documentation hooks**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: hooks pass and generated documentation references are updated.

- [ ] **Step 3: Run the complete test suite**

Run:

```bash
uv run pytest --cache-clear -x --lf -n auto --no-cov -q
```

Expected: complete suite passes with no failures.

- [ ] **Step 4: Review the exact diff**

Run:

```bash
git diff --check
git diff origin/main...HEAD --stat
git status --short
```

Expected: only the timeout implementation, tests, specification, plan, and documentation are changed.

- [ ] **Step 5: Commit documentation and generated references**

```bash
git add docs/configuration/agents.md docs/configuration/teams.md docs/dev/agent_configuration.md skills/mindroom-docs/references/
git commit -m "Document compaction timeout setting"
```
