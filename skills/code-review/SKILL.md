---
name: code-review
description: "Structured code review with security, performance, and quality checklists"
user-invocable: true
---

# Code Review

Perform a structured code review following this process. Apply each section in order, report findings using the severity format at the end. Every finding must cite concrete evidence (code path, line reference, spec mismatch, or failing test). Do not speculate — if something seems wrong but you cannot verify it, note it as unverified.

## 1. Overview

Understand the change before reviewing details:
- What is the purpose of this change?
- What is the scope? (files touched, lines changed)
- Is there a linked issue, PR description, or commit message explaining intent?
- Does the change do what it claims to do?

## 2. Architecture

- Does the change fit existing patterns in the codebase?
- Is there unnecessary complexity or abstraction?
- Are responsibilities properly separated (no god functions/classes)?
- Does it introduce tight coupling between modules?
- Are there circular dependencies?
- Could existing utilities or helpers be reused instead of new code?

## 3. Security Checklist

- [ ] Input validation: All external input validated with context-appropriate controls (parameterization, output encoding, allowlists)
- [ ] Injection: No SQL injection, command injection, XSS, or template injection vectors
- [ ] Authentication/Authorization: Auth checks present where required
- [ ] Data exposure: No secrets, tokens, or PII in logs, errors, or responses
- [ ] File operations: Path traversal prevented, permissions checked
- [ ] Deserialization: No unsafe deserialization of untrusted data
- [ ] Dependencies: New dependencies checked for known vulnerabilities
- [ ] CORS/Headers: Proper security headers and CORS configuration

## 4. Performance

- Algorithmic complexity: Any O(n^2) or worse where O(n) or O(n log n) is possible?
- Unnecessary allocations: Objects created in loops, redundant copies?
- N+1 queries: Database calls inside loops?
- Caching: Should results be cached? Are caches invalidated correctly?
- Blocking operations: Synchronous I/O where async is expected?
- Resource cleanup: Files, connections, and handles properly closed?
- Pagination: Unbounded queries or list operations?

## 5. Error Handling

- Are errors propagated, not silently swallowed?
- Do error messages provide enough context for debugging?
- Are expected failure modes handled (network errors, missing data, timeouts)?
- Is there proper cleanup on error paths (finally blocks, context managers)?
- Are retries implemented where appropriate (with backoff)?
- Do errors use appropriate severity (exception vs. warning vs. log)?

## 6. Testing

- Are the changes tested?
- Do tests cover the happy path AND edge cases?
- Are failure scenarios tested?
- Is test quality good (not just asserting True)?
- Are tests isolated (no shared mutable state, no order dependency)?
- Do tests have clear names describing the scenario?
- Is there integration test coverage where unit tests are insufficient?
- Are mocks used appropriately (not over-mocked)?

## 7. Readability

- Are names clear and descriptive (variables, functions, parameters)?
- Is the code self-documenting or are complex parts commented?
- Is the style consistent with the rest of the codebase?
- Are magic numbers replaced with named constants?
- Is the control flow straightforward (early returns, guard clauses)?
- Are functions a reasonable length (single responsibility)?

## 8. Dependencies

- Are new dependencies justified? Could stdlib or existing deps cover it?
- Are dependency versions pinned?
- What is the maintenance status of new dependencies?
- Are there license compatibility concerns?
- What is the size impact (bundle size, install footprint)?

## Python-Specific Checks

When reviewing Python code, also check:
- **Type hints**: Are function signatures typed? Are complex types annotated?
- **Async patterns**: Proper await usage, no blocking calls in async functions, no unawaited coroutines
- **Dataclasses**: Used instead of plain dicts for structured data where appropriate
- **Context managers**: Used for resource management (files, connections, locks)
- **Import style**: Top-level imports preferred; function-level imports acceptable for optional dependencies or avoiding circular imports
- **f-strings**: Preferred over `.format()` for string building; `%s` formatting is acceptable in logging calls
- **Pathlib**: Preferred over `os.path` for file operations where practical
- **Comprehensions**: Used where clearer than loops, but not over-nested

## Output Format

Report each finding using this format:

```
### [SEVERITY] Short description

**File**: `path/to/file.py:42`
**Category**: Security | Performance | Error Handling | Testing | Readability | Architecture | Dependencies

Description of the issue and why it matters.

**Suggestion**: How to fix it (with code snippet if helpful).
```

### Severity Levels

- **BLOCKER**: Must fix before merge. Security vulnerabilities, data loss risks, broken functionality, crashes.
- **WARNING**: Should fix before merge. Performance issues, missing error handling, inadequate tests for critical paths.
- **SUGGESTION**: Consider fixing. Better patterns available, minor readability improvements, optional optimizations.
- **NITPICK**: Optional. Style preferences, naming alternatives, minor formatting.

### Summary Template

End the review with:

```
## Review Summary

**Files reviewed**: N
**Findings**: X blocker, Y warning, Z suggestion, W nitpick

### Blockers (must fix)
- ...

### Warnings (should fix)
- ...

### Suggestions (consider)
- ...

### What looks good
- ... (always include positive observations)
```
