---
name: simplification-scan
description: Use when running a cleanup scan over a pull request or an architecture simplification scan in the MindRoom repository
---

# Simplification scan in MindRoom

Follow the baspowers skill [`scanning-for-simplifications`](https://github.com/basnijholt/baspowers/tree/main/skills/scanning-for-simplifications) for the process.
This file lists what MindRoom adds to its behavior contract and its gates.

## Behavior that stays intact

Append these to the scanner brief's behavior line, and hold verifiers and reviewers to them too:

- The guarantee of every `LEGACY_COMPAT:` and `AGNO_COMPAT:` rule; remove a workaround only under its policy's removal criteria in AGENTS.md.
- The intentional behaviors in `docs/architecture/security-posture.md` and the accepted limitations in `docs/architecture/reply-messages.md`; neither counts as a bug.
- `mindroom.plugin_api` and any other name the plugins in `.github/plugin-fleet.json` import.

## Gates after applying proposals, before ready

1. Full core suite as AGENTS.md "NixOS Test Shell" describes: `env -u TMPDIR -u TEMP -u TMP uv run pytest`, plus `just test-front`, `just test-saas-backend`, or `just test-saas-frontend` when those trees change.
2. `uv run pre-commit run --all-files` and `uv run tach check --dependencies --interfaces`, with `tach.toml` updated in the same PR when a Tach-governed boundary changes.
3. Tool changes: `tests/tool_system/` passes, and no file under `tests/fixtures/agno_parity/` changes.
4. Deleted names leave `vulture_whitelist.py`, and added, renamed, or removed key modules update their `docs/architecture/code-map.md` rows.
5. Plugin-facing changes: the plugin checks in CI pass, or each failing plugin gets a companion PR that targets the latest MindRoom with no version gate or compatibility shim, listed in the PR body with the merge order.
6. Changes on the inbound path, the reply path, journal reads or writes, or anything awaited per message: a paired load benchmark against the merge base, with at least 10 alternating pairs that each start below a 1-minute load of a quarter of the host's cores.
   No harness ships in this repository, so the PR body names the harness and workload.
   The gate fails when a metric is 5% or more slower at p < 0.05, and the PR body reports each metric's median change with its 95% interval.
7. Turn, reply, journal, dispatch, Matrix ingress, or shutdown changes: `just test-live-journal-gate`, as `scripts/README.md` describes, plus `uv run python scripts/testing/fuzz_live_matrix.py --profile chaos --seed N` for N = 42, 7, and 1.

## At merge

For each `LEGACY_COMPAT:` marker this PR adds for a format it retires, set "Last legacy release" to the newest release tag right before merge, because that release still writes the old format.
A commit that changes only that comment needs no new review round.
Markers for unreleased or untagged formats keep their wording.
