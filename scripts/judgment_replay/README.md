# Offline judgment replay

Phase 1 only: freeze evidence, collect independent labels, replay saved TypeSafe responses and report coverage.
No runtime imports this harness; no participation, notice, configuration or response-pipeline behavior changes.
The candidate contract pins `jev-1.13.0`; this is not a claim that its real-world quality has been evaluated.

## Privacy and network boundary

`extract`, `label`, `report` and fixture evaluation deny socket creation and subprocess execution with a process-wide Python audit hook.
No command sources environment files or prints message bodies, credentials or provider exception text.
Raw source copies remain private, potentially sensitive evidence: directory mode `0700`, file mode `0600`, outside the live source tree and outside Git.
Configure a local Git exclusion before extraction if evidence lives inside a worktree.
Never commit evidence, annotations or real predictions.
Symlinks are refused, including parent components; symlink entries discovered during traversal are counted and skipped.
Mutable regular files are accepted only if their identity, size and modification time stay stable through copying; SQLite databases use a consistent read-only backup including WAL state.
Failed copies are removed and reported.

Live `evaluate` requires explicit `--allow-network`, a reviewed input file and its exact SHA-256, complete verified case provenance, a credential, a dated tariff, and explicit request and dollar caps.
This flag was **never used against an external transport in Phase 1**.
Bas has not authorized redacted conversation text to leave the host.
Best-effort credential redaction does not establish permission to send other private text.
The current historical corpus has no verified pending membership, so its cases cannot pass live input validation.

## Freeze and inspect

Commands run from the repository root.
Choose a new persistent private destination for each snapshot; existing snapshots are never overwritten.

```sh
uv run python -m scripts.judgment_replay extract \
  --source /path/to/mindroom_data \
  --evidence .claude/evidence/issue-268/snapshot \
  --source-revision SOURCE_GIT_COMMIT

uv run python -m scripts.judgment_replay report \
  --evidence .claude/evidence/issue-268/snapshot \
  --output .claude/evidence/issue-268/report.md
```

Extraction writes `manifest.json`, `cases.jsonl`, `coverage.json`, `exclusions.json`, an empty unauthorized `outgoing-manifest.json`, and frozen `corpus/` copies.
The manifest hashes sources and derived artifacts; subsequent commands verify those hashes before consuming them.
It records source revision, source modification times, snapshot method, UTC extraction time and split cutoffs.
Parser exclusions include frozen-file paths and byte offsets.
Complete adjacent JSON objects are supported; malformed tails are quarantined without scanning nested text for another object.

The exact `event == "queued_message_notice_injected"` records in `logs/mindroom_*.log` are the only B truth set.
Invalid correlation is excluded from joins but remains in the exact-event inventory.
Fresh current-owner provider markers supply projected boundary context; persisted markers in later requests cannot create cases or fresh boundary observations.
Repeated provider observations do not prove distinct eligible tool boundaries.
Exports count as joined evidence only when room, thread and triggering event match, and provide visible text and outcomes only.
Exports never reconstruct pending membership or pre-edit decision input from final assistant edit times.
Session markers supply position evidence; journal joins supply ownership and receipt ordering.
Neither supplies the historical memory-only pending set.

`development`, `validation` and `holdout` are chronological partitions, defaulting to 60%/80% event-time cutoffs.
Explicit UTC cutoffs may be supplied with `--validation-after` and `--holdout-after`.
Whole threads crossing a cutoff are excluded, with a recorded reason, so correlated follow-ups cannot leak into another split.
Tiny corpora with identical cutoffs remain development-only.
Surface A extraction counts exact participation-instruction candidates, but cannot promote them to eligibility without historical config, authorization and ownership evidence.
The report explicitly records A's missing verified cohort.

## Independent labels

Create one blind template per human labeler; predictions never enter this command.

```sh
uv run python -m scripts.judgment_replay label \
  --evidence .claude/evidence/issue-268/snapshot \
  --labeler HUMAN_A --pass 1 \
  --output .claude/evidence/issue-268/pass-1-template.jsonl
```

Templates contain case hashes, private source references and a null label, not automatic ground truth.
Fill each row locally and validate it with the same command plus `--annotations FILLED_FILE`, writing a fresh output path.
A second independent human uses pass 2 and a different identity.
The rubric `queued-v1` uses exactly these labels:

- `finish_correct`: every pending message is known, and completing the active work before handling the whole batch is appropriate.
- `wrap_up_needed`: correction, redirection, withdrawn intent, changed arguments, a new dependency, or ambiguity requires handling the follow-up first.
- `insufficient_evidence`: active input, complete pending membership or ordering cannot be established.

A requested data or credential paste is not automatically safe to ignore; the task may depend on it.
Set `severe: true` for revocation, urgent correction or another harmful-continuation case.
Actual notice delivery, observed obedience and task completion remain separate facts.
Two matching human passes are required for scoring; disagreements and insufficient evidence remain exclusions.
No labels were fabricated for the real Phase 1 corpus.

## Recorded evaluation

Explicit caps are mandatory even when replaying fixtures.
Each saved-response row contains `case_id`, exact `request_hash`, `response_json` preserving original JSON bytes as a string, and recorded `latency_ms`.
Synthetic input rows identify a known case ID, declare `synthetic: true`, and provide `input.active`, `input.queued`, optional `input.context`, and `input.tool_names` using the leaf state schema.
The harness builds and hashes that input locally, strictly validates the saved response through the shared decoder, and marks every resulting prediction synthetic.
Synthetic predictions cannot contribute to real quality metrics.

```sh
uv run python -m scripts.judgment_replay evaluate \
  --evidence .claude/evidence/issue-268/snapshot \
  --inputs SYNTHETIC_INPUTS.jsonl \
  --recorded-responses SAVED_RESPONSES.jsonl \
  --max-requests 20 --max-cost-usd 0.10 \
  --input-price-per-million 0.042 --price-date 2026-09-20 \
  --output .claude/evidence/issue-268/fixture-predictions.jsonl
```

The example tariff comes from the public [TypeSafe model contract](https://docs.typesafe.ai/models), checked on 2026-09-20; verify pricing again before any future authorized live evaluation.
Fixture costs are estimates from fixture token counts, never actual spending evidence.
Each attempt reserves the full documented 64,000 input-token ceiling before dispatch; actual returned usage replaces the reservation, while missing usage keeps the worst-case charge.
Errors and timeouts consume attempts; no retry occurs.
The leaf client also applies immediate shared global/per-owner concurrency caps, a total deadline, strict model-drift refusal and a 64 KiB response bound.
Returned confidence is retained exactly; thresholds use `probabilities.finish`.

## Reports and experiment limits

Use `report --labels PASS1 PASS2 --predictions PREDICTIONS` to include independently labeled diagnostic curves.
Reports keep splits separate, expose precision, recall, confusion counts, unsafe case hashes, severe-case vetoes and 95% Wilson intervals, plus thread-level unsafe-finish uncertainty.
Threshold sweep points are illustrative; no threshold or tolerated harmful-continuation risk is selected automatically.
Missing predictions and unknown labels must not imply success.
The zero-classifier baseline costs $0.

Context/whole-queue ablations, adversarial quality, alias-versus-pin quality, measured latency, first-boundary readiness and suppression-age selection remain explicitly unmeasured until suitable evidence and permission exist.
The 306-versus-51 export-window ratio is a warning about export heuristics, not a measured false-positive rate.
Historical replay cannot establish causal work saved or prove notice obedience.
A documented no-go is the complete Phase 1 outcome; it does not authorize production wiring.
