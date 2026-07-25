# Thread edit integrity status

Current PR: #1641.
Rejected exact head: `2429c69c411ca02087e47cd2050b7c05e003f704`.
Current production source: `+786/-608`, net `+178`; hard ceiling is net `+200`.

Fresh native Codex `gpt-5.6-sol` xhigh returned `CHANGES REQUIRED`.
An inline room-message replacement with `m.new_content.body` but no `m.new_content.msgtype` fails `valid_room_message_replacement` yet is accepted by `extract_edit_body`.
The defect reaches `EditRegenerator`, where the malformed body can become the regeneration prompt.
The claim is independently reproduced on the exact head.

The narrow correction is to run the supplied replacement validator for both inline and hydrated content.
The raw-versus-canonical replacement-relation equality check remains conditional on hydration.
An owning-seam `EditRegenerator` regression will prove malformed inline edits do not regenerate.

Exact-`2429c69c4` GitHub pytest, smoke, builds, plugins, security, and Tach passed before this blocker was found.
Those results and the review become stale after the correction.
Greptile on `2429c69c4` remains non-gating and will also become stale.

PR #1639 exact `09330793f` owns the heavy slot.
PR #1641 is next in the heavy queue and must remain light-only until explicit release.
Opus is advisory-only and is not queued.

Preserve the three untracked task prompts.
Remove this living handoff only in the final freeze commit before fresh exact-head review and validation.
Do not merge, amend, force-push, or use temporary worktrees or evidence.
