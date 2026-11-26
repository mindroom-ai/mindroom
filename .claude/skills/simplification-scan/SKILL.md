---
name: simplification-scan
description: Use when running a cleanup, cruft, or duplicated-functionality scan over a pull request, or an architecture simplification scan, in the MindRoom repository
---

# Simplification scan in MindRoom

Follow the baspowers skill [`scanning-for-simplifications`](https://github.com/basnijholt/baspowers/tree/main/skills/scanning-for-simplifications) for the process.
This file lists what MindRoom adds to its behavior contract and its gates.

## Behavior that stays intact

Append these to the scanner brief's behavior line, and hold verifiers and reviewers to them too:

- The guarantee of every `LEGACY_COMPAT:` and `AGNO_COMPAT:` rule; remove a workaround only under its policy's removal criteria in AGENTS.md.
- The intentional behaviors in `docs/architecture/security-posture.md` and the decisions and accepted limitations in `docs/architecture/reply-messages.md`; none of them counts as a bug.
- `mindroom.plugin_api` and any other name the plugins in `.github/plugin-fleet.json` import.

## Gates after applying proposals, before ready

1. Full core suite as AGENTS.md "NixOS Test Shell" describes: `env -u TMPDIR -u TEMP -u TMP uv run pytest`, plus `just test-front`, `just test-saas-backend`, or `just test-saas-frontend` when those trees change.
2. `uv run pre-commit run --all-files` and `uv run tach check --dependencies --interfaces`, with `tach.toml` updated in the same PR when a Tach-governed boundary changes.
3. Cleanup commits rewrite no golden file: nothing under `tests/fixtures/` and not `tests/tool_system/tool_placement_golden.txt`, because a golden diff is a behavior change.
4. Added, renamed, or removed key modules update their `docs/architecture/code-map.md` rows.
5. The plugin checks in CI pass.
   A cleanup proposal that breaks a plugin is dropped; a plugin-facing change the PR intends gets a companion PR for each failing plugin, targeting the latest MindRoom with no version gate or compatibility shim, listed in the PR body with the merge order.
6. Changes on the inbound path, the reply path, journal reads or writes, or anything awaited per message: a paired load benchmark against the merge base, with at least 10 alternating pairs that each start below a 1-minute load of a quarter of the host's cores.
   No harness ships in this repository, so the PR body names the harness and workload.
   The gate fails when a metric is 5% or more slower at p < 0.05, and the PR body reports each metric's median change with its 95% interval.
7. Turn, reply, journal, dispatch, Matrix ingress, or shutdown changes: `just test-live-journal-gate`, as `scripts/README.md` describes, plus `uv run python scripts/testing/fuzz_live_matrix.py --profile chaos --seed N` for N = 42, 7, and 1.

## At merge

For each `LEGACY_COMPAT:` marker this PR adds for a format it retires, check in history that the newest release tag still writes that format.
If it does, set "Last legacy release" to that tag and the replacement to the release after it, right before merge.
MindRoom policy: a commit that changes only that comment keeps the approvals.
