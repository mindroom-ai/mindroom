---
name: duplication-audit
description: Find duplicated functionality across the codebase and propose minimal, safe generalizations.
argument-hint: "[path or module to audit]"
---

# Duplication audit and generalization prompt

You are a coding agent working inside a repository. Your job is to find duplicated
functionality (not just identical code) and propose a minimal, safe generalization.
Keep it simple and avoid adding features.

## First steps

- Read project-specific instructions (CLAUDE.md, AGENTS.md, or similar) and follow them.
- Ask a brief clarification if the request is ambiguous (for example: report only vs refactor).

## Objective

Identify and consolidate duplicated functionality across the codebase. Duplication includes:
- Multiple functions that parse or validate the same data in slightly different ways
- Repeated config parsing or data model manipulation
- Similar external service calls across different modules
- Near-identical error handling or logging patterns
- Repeated setup/teardown logic
- Similar data transforms that can become a shared helper

The goal is to propose a general, reusable abstraction that reduces duplication while
preserving behavior. Keep changes minimal and easy to review.

## Search strategy

1) Map the hot paths
- Scan entry points (CLI, main loops, API handlers) to see what they do repeatedly.
- Look for cross-module patterns: same steps, different files.

2) Find duplicate operations
- Use fast search tools to find repeated keywords and patterns.
- Check for repeated parsing, IO, validation, or response formatting.

3) Validate duplication is real
- Confirm the functional intent matches (not just similar code).
- Note any subtle differences that must be preserved.

4) Propose a minimal generalization
- Suggest a shared helper, utility, or wrapper.
- Avoid over-engineering. If only two call sites exist, keep the helper small.
- Prefer pure functions. Keep IO at the edges.

## Deliverables

Provide a concise report with:

1) Findings
- List duplicated behaviors with file references and a short description of the
  shared functionality.
- Explain why these are functionally the same (or nearly the same).

2) Proposed generalizations
- For each duplication, propose a shared helper and where it should live.
- Outline any behavior differences that need to be parameterized.

3) Impact and risk
- Note any behavior risks, test needs, or migration steps.

If the user asked you to implement changes:
- Make only the minimal edits needed to dedupe behavior.
- Keep the public API stable unless explicitly requested.
- Add small comments only when the logic is non-obvious.
- Summarize what changed and why.
- Run `pytest` to verify nothing broke.

## Output format

- Start with a short summary of the top 1-3 duplications.
- Then provide a list of findings, ordered by impact.
- Include a small proposed refactor plan (step-by-step, no more than 5 steps).
- End with any questions or assumptions.

## Guardrails

- Do not add new features or change behavior beyond deduplication.
- Avoid deep refactors without explicit request.
- Preserve existing style conventions and import rules.
- If a duplication is better left alone (e.g., clarity, single usage), say so.
