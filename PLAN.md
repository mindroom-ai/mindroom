# ISSUE-250 Plan

1. Record the current source and real wire-description sizes, then identify tests and documentation that depend on the tool docstrings.
2. Relocate the complete cleaned `matrix_message` docstring verbatim to `docs/tools/matrix-message.md` and replace it with a concise model-facing description that retains every required safety rule and useful parameter guidance.
3. Condense the `matrix_voice_message` docstring below 800 characters while preserving its targeting, companion-message, and parameter semantics.
4. Add size and behavioral-guidance regression tests, update the tool docs index, run scoped and full tests plus pre-commit, and record the final counts in the requested report.
