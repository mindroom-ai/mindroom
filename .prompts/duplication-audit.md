# Duplication audit and generalization prompt

You are a coding agent working inside a repository. Your job is to find duplicated
functionality (not just identical code) and propose a minimal, safe generalization.
Keep it simple and avoid adding features.

## First steps

- Read project-specific instructions (CLAUDE.md, AGENTS.md, or similar) and follow them.
- MindRoom prefers functional style over classes, uses dataclasses over dicts, and
  keeps imports at the top of files. Respect these conventions.
- Ask a brief clarification if the request is ambiguous (for example: report only vs refactor).

## Objective

Identify and consolidate duplicated functionality across the codebase. Duplication includes:
- Multiple functions that parse or validate the same data in slightly different ways
- Repeated config parsing or Pydantic model manipulation
- Similar Matrix client operations across different modules
- Near-identical error handling or logging patterns
- Repeated agent/team setup or teardown logic
- Similar tool registration or event handling patterns
- Duplicated memory operations (agent, room, team scopes)

The goal is to propose a general, reusable abstraction that reduces duplication while
preserving behavior. Keep changes minimal and easy to review.

## Search strategy

1) Map the hot paths
- Scan entry points (CLI in `cli.py`, bot in `bot.py`, API handlers) to see what they do repeatedly.
- Look for cross-module patterns: same steps in `agents.py`, `teams.py`, `routing.py`, etc.
- Check `tools/` subdirectory for repeated patterns across tool implementations.

2) Find duplicate operations
- Check for repeated Matrix client calls, config parsing, memory operations,
  agent creation, streaming logic, or response formatting.
- Look at `memory/functions.py` vs memory usage sites for duplicated logic.

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
- Preserve existing style conventions (functional style, top-level imports, dataclasses over dicts).
- If a duplication is better left alone (e.g., clarity, single usage), say so.
