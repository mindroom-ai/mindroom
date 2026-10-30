---
name: simplification-scan
description: Use when running a cleanup scan over a pull request or an architecture simplification scan in the MindRoom repository
---

# Simplification scan in MindRoom

Follow the baspowers skill `scanning-for-simplifications` for the process.
This file lists what MindRoom adds to its behavior contract and its gates.

## Behavior that stays intact

Scanners, verifiers, and reviewers also keep these unchanged:

- Every `LEGACY_COMPAT:` and `AGNO_COMPAT:` rule and the code it marks, under the AGENTS.md Legacy and Agno Compatibility Policies.
- The intentional behaviors in `docs/architecture/security-posture.md` and the accepted limitations in `docs/architecture/reply-messages.md`; neither counts as a bug.
- Names the plugin fleet imports.

## Gates after applying proposals, before ready

1. Full suite in the NixOS test shell, as AGENTS.md "NixOS Test Shell" describes: `env -u TMPDIR -u TEMP -u TMP uv run pytest`.
2. `uv run pre-commit run --all-files` and `uv run tach check --dependencies --interfaces`, with `tach.toml` updated in the same PR.
3. Tool changes: the schema and coercion goldens in `tests/tool_system/` pass unchanged.
4. Deleted names leave `vulture_whitelist.py`; added, renamed, or removed modules update their `docs/architecture/code-map.md` rows.
5. Plugin-facing changes: the plugin checks in CI pass, or each failing plugin gets a companion PR that targets the latest MindRoom with no version gate or compatibility shim, listed in the PR body with the merge order.
6. Changes on the inbound path, the reply path, journal reads or writes, or anything awaited per message: a paired load benchmark against the merge base, alternating runs that each start below a 1-minute load of 8.
   No metric may be 5% or more slower at p < 0.05.
7. Turn, reply, journal, or Matrix changes: the live chaos fuzz, `scripts/testing/fuzz_live_matrix.py --profile chaos --steps 200 --clients 4 --rooms 2`, passing on seeds 42, 7, and 1.
8. Right before merge, bump every "Last legacy release" this PR added to the newest release tag.
