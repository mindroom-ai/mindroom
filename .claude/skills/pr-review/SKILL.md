---
name: pr-review
description: Zero-tolerance pull request review. Every issue is a blocker. Use when reviewing PRs for merge readiness.
---

Review the pull request with a **zero-tolerance standard**.
Every issue you find is a blocker — there is no such thing as a "minor issue" or "non-blocking suggestion".
Either the PR is flawless and ready to merge, or it has problems that MUST be fixed before merging.
Do not approve a PR with caveats like "ready to merge but consider..." or "minor nit:".
If you would report it as an issue, it must be fixed.

**Your verdict must be one of**:
- ✅ **APPROVE** — The code is near-perfect. No issues found. Merge immediately.
- ❌ **CHANGES REQUIRED** — Issues found. List every one. All must be fixed before re-review.

Never approve with suggestions.
Never say "looks good overall but...".
If there's a "but", it's CHANGES REQUIRED.
The only notes allowed beside either verdict are the **Edge cases (not blocking)** and **Out of scope** lists defined below.

## Realistic Issues Only

A bug or security finding is an issue only when it has a concrete, realistic scenario: who triggers it in normal use of a supported configuration or operator workflow, including config edits, upgrades, and restarts, or which boundary in [docs/architecture/security-posture.md](../../../docs/architecture/security-posture.md) it crosses.
State that scenario with each such finding.
A bug or security finding without such a scenario is an edge case, for example one that needs an unusual configuration plus unusual data, or deliberate operator misuse such as hand-editing internal state.
Attacks across a boundary the security posture protects are never edge cases, and findings that match an intentional behavior listed there are not issues at all.
Edge cases do not block; list them, one line each, under **Edge cases (not blocking)**.
Checklist items without a runtime trigger, such as duplication, structure, tests, and docs, keep the standard below.
A missing test for an edge case is itself an edge case.
Do not ask for mechanisms that no realistic scenario needs, such as new limits, caches, retries, fallbacks, or hardening.

## Out-of-Scope Problems

Report real problems that exist without this PR, in code it does not touch, and that the Scope and Refactor Standard below does not require it to fix, under **Out of scope** with evidence.
A problem the PR introduces or newly exposes is in scope even when the faulty line is unchanged.
Out-of-scope problems do not block this PR.
Whoever acts on the review records each one as soon as it is classified, in the task's existing tracking location such as its tracking file or GitHub issue, or in the final report when there is none, so it survives context compaction.

## Scope and Refactor Standard

Code touched by a PR must be merge-and-forget quality — no rough edges, no avoidable duplication, no unconventional idioms.
Do not require refactors of untouched code unless they have clear immediate ROI.

- Require a broader refactor only when it has clear immediate ROI:
  - It removes active duplication in current code paths.
  - It creates one clear consolidation point.
  - It reduces net complexity after the change.
  - It is validated by meaningful tests in the same PR.
- Do not require broad refactors for hypothetical future needs.

## Review checklist

- **Code cleanliness**: Is the implementation clean and well-structured?
- **DRY principle**: Does it avoid duplication?
- **Architectural smells**: Identify scattered logic or the same policy/resolution logic being defined in multiple places instead of one source of truth.
- **Code reuse**: Are there parts that should be reused from other places?
- **Organization**: Is everything in the right place?
- **Consistency**: Is it in the same style as other parts of the codebase?
- **Simplicity**: Is it not over-engineered? Remember KISS and YAGNI. No dead code paths and NO defensive programming. No unnecessary try-excepts.
- **No pointless wrappers**: Identify functions/methods that just call another function and return its result. Callers should call the underlying function directly instead of going through unnecessary indirection.
- **Functional style**: Does it prefer functions over classes where appropriate? Are dataclasses used instead of raw dicts?
- **Imports**: Are imports at file top, with function imports used to avoid cycles or defer heavy/optional dependencies until first use, as required by [AGENTS.md](../../../AGENTS.md#1-core-philosophy)?
- **Deferred imports**: Are these explicit (`from x import Y`), annotated with `# noqa: PLC0415` where needed, and consistent with `tests/test_import_graph.py`?
- **User experience**: Does it provide a good user experience?
- **PR**: Is the PR description and title clear and informative?
- **Docs**: Do docs changes follow the Documentation Policy in @AGENTS.md? A change to configuration, user-visible behavior, or an operator procedure without an update to its owning page is a blocker. Docs text that narrates a bug fix or describes implementation mechanics outside `docs/architecture/` is also a blocker; a fix that restores documented behavior needs no docs change.
- **Tests**: Are there tests, and do they cover the changes adequately? Are they testing something meaningful or are they just trivial? On NixOS, run them inside `nix-shell shell.nix` (or use `nix-shell shell.nix --run 'uv run pytest -x -n 0 --no-cov -v'`). If `<nixpkgs>` is unresolved, retry with `nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix`.
- **Live tests**: If feasible, test the changes with a local Matrix stack (`just local-matrix-up`) and the Matty CLI to verify agent behavior end-to-end.
- **Rules**: Does the code follow the project's coding standards and guidelines as laid out in @AGENTS.md?

## How to review

Look at `git diff origin/main..HEAD` for the changes made in this pull request.
