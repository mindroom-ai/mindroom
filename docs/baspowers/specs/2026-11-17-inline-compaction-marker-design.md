# Inline compaction marker ("seam") design

Status: approved by Bas on 2026-11-17 ("C · Seam ... build this exactly like that").
Paired PRs: `mindroom-ai/mindroom` (backend: record compactions on the reply) and `mindroom-ai/mindroom-chat` (client: render them). This file is the single event contract for both.

## Problem

Every history compaction posts its own `m.notice` ("Compacting history...", edited to "📦 Compacted 24 runs: 366,804 → 6,336 / 351,792 history budget"). Mid-turn compaction (between two model requests inside one reply) therefore shows up as a separate message below the reply, far from where it happened. The client has no renderer for it, so it is plain italic text, and it moves the thread card.

## What Bas picked

Variant C, "Seam", from the mockup `~/.mindroom-chat/mindroom_data/agents/mindroom_dev/workspace/mockups/compaction-inline/mockup.html` (render `?v=c&on=all`, PNG `variant-c.png` beside it). The mockup is the visual source of truth: its CSS (`.seamrow`, `.seam`, `.mk`, `.card`, `.gauge`, `.kv`, `.tag`, the running `slide` animation) and its JS `restLabel`, `card`, `gauge` functions give the exact copy, layout and states. Build all scenes as drawn:

1. Mid-turn, tool calls open: a seam row right after the tool call it followed, splitting the tool run in two. Hover (tap on touch) opens the card.
2. Tool calls hidden by the cog: the seam stays visible between the texts (it is a notice, not a tool call).
3. Finished long reply folded behind "Worked for": the seam is part of the folded work and shows only when the fold is opened. No extra mark on the Worked-for row.
4. Pre-turn: while the reply is still the pending placeholder, the seam shows "Compacting context… 12 s" at the top; the finished reply keeps "Compacted before replying · 348k → 42k tokens" at its top.
5. Two compactions in one reply: one seam per compaction, each at its own spot.
6. States: running (ticking seconds, animated hairline), success, failed, timed out (amber, dashed hairline, "kept full history").
7. Manual compaction and every old `io.mindroom.compaction` notice in history: drawn as the same quiet seam row in place of the italic notice text.

Not in v1 (Bas left these optional extras unticked): "Read the summary" (the card has no summary row), the mark on the Worked-for row, the header chip, the 📦 emoji instead of the line icon, keeping the separate notice for automatic compactions.

## Event contract

New key on the agent's reply event, on the original send and on every `m.replace` edit's `m.new_content` (beside `io.mindroom.reasoning`):

```json
"io.mindroom.compactions": [
  {
    "id": "3f9c...",              // opaque, unique within the reply
    "phase": "pre_turn",          // "pre_turn" | "mid_turn"
    "mode": "auto",               // "auto" | "manual" (v1 only ever writes "auto" here)
    "after_tool": 0,              // visible tool calls started in this reply before the compaction began
    "status": "running",          // "running" | "success" | "failed" | "timeout"
    "started_at": 1794940102256,  // ms since epoch, backend clock
    "duration_ms": 8400,          // optional; elapsed so far while running, final otherwise
    "summary_model": "haiku",
    "before_tokens": 366804,
    "after_tokens": 6336,         // optional; present on success (and progress)
    "history_budget_tokens": 351792, // optional
    "runs_before": 24,
    "compacted_run_count": 24,    // optional; present on success (and progress)
    "runs_remaining": 15,         // optional; only while running
    "failure_reason": "..."       // optional; only on failed/timeout
  }
]
```

- The list is in start order. Optional keys are omitted, never `null`.
- `after_tool` uses the same counting as `io.mindroom.reasoning.segments[].after_tool`: `len(tool_trace)` when the reply shows tool calls, else 0. It is read once, when the compaction starts.
- Live frames (`io.mindroom.live`) carry the same list as `compactions` in snapshots and, whole-list replace, in deltas (like `segments`). Frame version stays 1 (additive).
- Split (segmented) replies carry the key on the first segment only.
- The old key `io.mindroom.compaction` (singular) keeps its meaning: a standalone `m.notice`. It is still sent for `mode == "manual"` compactions, and old events keep it.

Client tolerance: parse defensively. Drop an entry that lacks a string `id`, a known `status`, a non-negative integer `after_tool`, or a number `before_tokens`; ignore unknown keys; a missing or malformed list means no seams. An entry still `running` on a reply whose `io.mindroom.stream_status` is terminal (or no longer streaming) renders as interrupted (failed styling, label "Compaction interrupted · kept full history", card reason "The reply ended before the summary finished."), never ticking forever.

## Backend (mindroom)

Detailed wiring with file:line evidence: `~/.mindroom-chat/mindroom_data/agents/mindroom_dev/workspace/compaction-inline/backend-wiring.md` ("Recommended wiring", T1-T9). Summary:

- `CompactionLifecycleStart` gains `phase: CompactionPhase = "pre_turn"`; `compact_scope_mid_turn` passes `"mid_turn"`.
- New `history/reply_compactions.py`: `ReplyCompactions` (per-reply recorder; `bind(tool_count, changed)`, `start`, `update`, `content`) and `ReplyCompactionLifecycle` (implements `CompactionLifecycle`; `mode == "manual"` delegates to `MatrixCompactionLifecycle`, which still sends the notice; everything else records into the recorder and sends no notice).
- `response_runner._deliver_turn` creates one recorder per reply, hands it to the lifecycle and to streamed and collected delivery; `final_extra_content()` adds the key.
- `StreamingResponse` adds the key to every durable write (progress and final), nudges a refresh when the recorder changes (same as worker warm-up progress), lets a compaction change justify an otherwise empty progress edit and a live checkpoint, and puts the list into `LiveDocs`.
- Prompt cache: no impact. Content keys never enter the prompt; `COMPACTION_NOTICE_CONTENT_KEY` stays in `_NON_TURN_NOTICE_CONTENT_KEYS`, and the plural key must not make a reply look like a notice.
- Unchanged: quiet/contact/participation turns and callers without a lifecycle stay silent; run-local summaries never use the lifecycle.
- Known limits (same as reasoning today): entries are not carried across approval resumes or restart replays; ledger-rendered terminal writes (cancel/error) drop the key.

## Client (mindroom-chat)

- **Reader** `src/app/matrix/protocol/compactions.ts`: `readReplyCompactions(content): ReplyCompaction[]` (camelCase fields) for the reply key, and `readCompactionNotice(content)` for the old singular notice (status, mode, before/after tokens, budget, runs, model, duration from `io.mindroom.compaction` metadata v3). Live frames: extend `LiveDocs`/`LiveDelta` in `liveFrame.ts` with optional `compactions`, and expose it through `useLiveReplyContent` under a live key, like `LIVE_REASONING_SEGMENTS_KEY`.
- **Marker** `src/app/messages/agent-run/CompactionSeam.tsx` (+ `.css.ts`): the seam row (icon, label, hairline) and the hover/tap card, reusing `ThreadSummaryMarker`'s trigger, `PopOut`/`Tooltip`, Escape, outside-click and scroll handling. Sizes in rem, theme colors from the app's color vars, motion via `Motion.css.ts` tokens; under reduced motion the hairline does not animate. The running label ticks once per second from `started_at` (or the notice's origin ts for old notices) with one interval per running seam, cleared on unmount and when the status leaves running.
- **Placement** inside the reply, by `after_tool`, with the same rule as reasoning segments (`withReasoning` in `agent-run/Reasoning.tsx`, the run split in `html/HtmlBlocks.tsx`): after the greatest rendered tool marker index `<= after_tool`, else at the top (above the body, below the reasoning header line). A seam between two consecutive tool markers splits that run. Seams render whether or not the cog hides tool calls, and whether or not the bulb shows thinking. Inside the Worked-for fold (`finishedWorkSplit`), a seam moves into the fold with the block that holds its run; a top seam goes in the fold too. When both a thinking segment and a seam share a spot, the seam comes first (compaction happens before the next model request writes its thinking).
- **Old/manual notices**: a MindRoom agent's `m.notice` carrying `io.mindroom.compaction` renders as the seam row (no card body text duplication; the card is built from the notice metadata) instead of italic text. It stays its own timeline row. It should be a quiet marker for the reply-quote fold (`isQuietMarker`, see `timelineRowFold.ts` and `useTimelineMessageFeature.tsx`, like the skill-review marker).

### Copy (from the mockup; put in `locales/additions/en.json` and every other overlay)

Rest label: running "Compacting context… {n} s"; success "Compacted context · {before} → {after} tokens" (mid-turn), "Compacted before replying · …" (pre-turn), "Compacted on request · …" (manual notice); failed "Compaction failed · kept full history"; timeout "Compaction timed out · kept full history"; interrupted (see contract). Token counts at rest use `k` rounding (`367k`), in the card full grouping (`366,804`).

Card title: "Context compacted" / "Compacting context… {n} s" / "Compaction failed" / "Compaction timed out" / "Compaction interrupted". Tag: "before the reply" / "mid-turn" / "manual". A `pre_turn` entry with `after_tool > 0` (a continuation attempt re-prepared after earlier tools) uses the mid-turn tag and subtitle, since it did not happen before the reply's first request. Subtitle: mid-turn "Between two model requests, after tool call {n}."; pre-turn "Before this reply's first model request."; manual "Requested with the compact command."; failure states "The reply went on with its full history." Gauge plus label: success "was {p}% of budget" / "now {q}%"; running "{p}% of budget"; failure "{p}% of budget, unchanged"; no gauge without a budget. Rows: success History, Saved ("{n} tokens ({p}%)"), Budget, Folded ("{n} runs into 1 summary"), Summarized by ("{model} in {secs}"), When; running History, Budget, Folding ("{n} runs, {done} done" with n = runs_before and done = compacted_run_count when present), Summary model, Started; failure Reason, History, Budget, Summary model, Gave up after, When. Times use the app's time format with seconds.

## Testing

Backend: unit tests for the recorder and lifecycle, streaming tests for `after_tool` anchoring (tools shown and hidden), live frames, the placeholder progress edit during pre-turn, the final event, segmented first-segment-only, partial-reply context (plural key not skipped), mid-turn `phase`, and the runner test that auto sends no notice while manual still does.

Client: reader tests (valid, malformed, legacy notice), placement tests (top, after a marker, splitting a run, cog hidden, inside the Worked-for fold, two seams, seam plus thinking segment at one spot), state tests (ticking stops, interrupted on a terminal reply), live frame parse/apply, the quiet-marker fold, and an e2e spec that renders scenes 1-7 on desktop and phone (390 px), dark and light, with the card open, for the PR screenshots.

## Merge and deploy order

Either order is safe. An old client shows nothing for the new key (the separate notice is gone for automatic compactions until the client ships, which is acceptable because Bas only uses MindRoom Chat). A new client against an old backend renders the old notices as seam rows.
