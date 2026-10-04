---
icon: lucide/history
---

# History & Compaction

## History Settings

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `num_history_runs` | int | `null` | Number of prior Agno runs to include as history context (`null` = all). Mutually exclusive with `num_history_messages` |
| `num_history_messages` | int | `null` | Max messages from history. Mutually exclusive with `num_history_runs` |
| `compress_tool_results` | bool | `null` | Compress tool results in history to save context. Inherits from `defaults.compress_tool_results` (default: `false`). On Anthropic and Vertex Claude models, setting this to `true` can mutate replayed tool messages and invalidate prompt-cache prefixes |
| `compaction` | object | `defaults.compaction` | Per-agent required-compaction overrides |
| `max_tool_calls_from_history` | int | `null` | Limit tool call messages replayed from history (`null` = no limit) |

## Context Window

Set `context_window` to the model provider's actual limit.
MindRoom uses it to budget persisted replay and required text compaction unless compaction config sets a smaller `replay_window_tokens` cap.
MindRoom always applies a final replay-fit step when the active runtime model has a known `context_window`.
That replay-fit step reduces or disables persisted replay for the current run when needed.
On `vertexai_claude` models, a known `context_window` also enables a request-time guard inside the provider call.
Before each asynchronous runtime request, including follow-up requests after tool results, MindRoom estimates the full provider payload and checks it against Vertex's exact token counter when it approaches the window.
When a request would exceed the window, MindRoom drops the oldest replayed history turns for that request only and logs a warning.
When the current turn alone cannot fit, the request fails with a clear provider error instead of being sent oversized.
Automatic compaction is enabled by default through `defaults.compaction`.
Supported routes use [native compaction](#native-compaction) during ordinary requests.
The portable text fallback runs before the reply when history exceeds the hard replay budget.
Set `enabled: false` in `defaults.compaction` or a per-agent/per-team `compaction` override to disable automatic native and pre-reply text compaction.

You can tune compaction behavior with these settings:

- Use `threshold_tokens` or `threshold_percent` to set the native provider trigger, or the soft planning threshold on text-only routes.
- Use `replay_window_tokens` to keep persisted replay and required-compaction planning within a smaller operational window without presenting that smaller value as the provider's request limit.
- Use `reserve_tokens` to leave hard-budget headroom for the current prompt and output.
- Use `model` to choose the summary model, and `fallback_model` to name a different model config retried once when the summary model refuses for safeguards; the same input is reused when it fits, otherwise it is rebuilt under the fallback model's own context budget, and after success that model serves the remaining chunks.
- Use `timeout_seconds` to bound each primary, retry, or fallback summary request; it defaults to 600 seconds, while an explicitly shorter provider timeout remains the stricter cap.

When the active runtime model window is known, replay safety uses the smaller of it and `replay_window_tokens`.
When that model window is unknown, an explicit `replay_window_tokens` still supplies the replay-planning window.
Each compaction summary input chunk is sized independently from the selected compaction model's real `context_window`, after reserve, prompt overhead, and a safety margin.
Text compaction requires the resolved summary input budget to exceed 2,000 tokens.
With the default `reserve_tokens`, this makes text compaction unavailable when the compaction model's context window is roughly 10,000 tokens or smaller; lowering `reserve_tokens` restores availability for such small windows.

Manual `compact_context` records a durable request that runs before the next reply in the same conversation scope.
Manual `compact_context` remains available when a compaction model and context window are configured and the resolved summary input budget exceeds 2,000 tokens.
It still uses the active runtime window for the final replay-fit step, while an explicit `compaction.model` can supply the summary-generation window subject to the same minimum summary-input budget.
If you set `compaction.model`, that summary model must also define its own `context_window` for the durable summary-generation pass.
`compaction.fallback_model` must also name a configured model with its own `context_window`; a fallback naming the summary model's alias, or another alias resolving to the same provider and model ID, is ignored because it would resend the refused request to the same model.
Required compaction runs before the reply with a Matrix lifecycle notice that is edited in place.
Otherwise MindRoom preserves canonical history and applies the selected replay strategy.

Replay planning uses a chars/4 approximation and reserves headroom for the current prompt and output.
Summary-input chunk sizing uses the model's tiktoken encoding when recognized.
Direct Anthropic, Vertex AI Claude, and Bedrock Claude summary models without a recognized encoding use one token per UTF-8 byte as a conservative upper bound.
Compaction chunk logs report `summary_input_estimate`, `summary_input_estimate_kind`, and `summary_input_budget_tokens` so tiktoken counts, o200k estimates, and UTF-8 byte upper bounds are never presented as the same measurement.
MindRoom does not mutate configured `num_history_runs` to fit the window.
Instead, it computes the replay plan that actually fits the current call and uses compaction to keep future replay healthy.
If needed, that replay plan can reduce raw replay, fall back to summary-only replay, or disable persisted replay entirely for the run.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5-5
    context_window: 1000000  # 1M tokens

defaults:
  compaction:
    replay_window_tokens: 200000  # Compact persisted replay around a smaller operational window
```

This is useful for models with smaller context windows or long-running conversations that accumulate persisted history.

An oversized saved summary requires a model with a larger context window or a smaller current prompt.
This can happen after switching models or when system instructions and the current message leave too little room for history.
Preparation does not silently omit or truncate the saved summary.

### Native compaction

MindRoom supports automatic [OpenAI Responses compaction](https://developers.openai.com/api/docs/guides/compaction) and [Claude compaction](https://platform.claude.com/docs/en/build-with-claude/compaction).
OpenAI native replay uses the official Responses endpoint or the Codex login backend with `store: false`.
When native mode is disabled, the authored storage setting is restored; canonical replay retains stateless reasoning and never chains to a response the provider did not store.
Automatic enablement covers GPT-5.3 Codex, GPT-5.4, and GPT-6 model families; other models retain the portable path.
An explicitly configured `store: true`, background mode, alternate OpenAI endpoint, or custom context-management request stays on the portable path.
Claude native compaction supports direct Anthropic and Vertex Claude on the supported Sonnet, Opus, Fable, and Mythos models; it is a provider beta and requires a trigger of at least 50,000 tokens.
An authored Claude `compact_20260112` policy keeps its own trigger and instructions; compatible checkpoints use the same replay and portable-fallback rules.
Changing that policy affects future summaries; existing compatible checkpoints remain conversation history.
Gemini, Chat Completions, and other provider adapters retain text compaction.

Native compaction requires automatic compaction to be enabled, all-history replay, no historical tool-call limit, and a trigger above the current prompt size and below the hard request limit.
Explicit `compaction.model`, scheduled history limits, bounded replay, unsupported models, and requests already exceeding the hard budget use the portable path.
Manual `compact_context` always requests portable text compaction.
Native checkpoints are tied to their provider, model, endpoint, and current portable summary, so changing that route or rewriting the summary rebuilds context from canonical history.
Native compaction itself never moves canonical runs; they stay stored in `session.runs` until portable text compaction moves older runs into the compaction archive.

Checkpoints and their following native output are persisted through the existing SQLite run storage and survive restarts.
The system instructions keep their shared prompt-cache prefix.
OpenAI checkpoints remain opaque; local budgeting conservatively estimates their serialized size rather than treating billed pre-compaction input as active context.
Claude usage includes every compaction iteration while context occupancy uses the final iteration.
Vertex token counting represents checkpoint contents as text and omits the compaction policy and beta header, which its counting endpoint rejects; generation still receives the native blocks and policy.
Canonical fallback discards thinking bound to a removed checkpoint on adaptive-thinking routes, while preserving the unchanged thinking required by legacy manual-thinking tool turns.
Claude `pause_after_compaction: true` is rejected because MindRoom's automatic path requires the provider to continue its response.

## Agent Compaction Settings

MindRoom uses native provider compaction when the active model and history policy support it, with portable text compaction as the fallback.
Per-agent compaction supports `enabled`, `threshold_tokens`, `threshold_percent`, `replay_window_tokens`, `reserve_tokens`, `model`, `fallback_model`, and `timeout_seconds`.
When the active runtime model has a known `context_window`, MindRoom always computes a per-run replay plan that reduces or disables persisted replay before the model call if needed.
Automatic compaction is enabled by default through `defaults.compaction`.
`threshold_tokens` and `threshold_percent` control the native provider trigger; on text-only routes they remain soft planning thresholds.
Native compaction runs inside ordinary provider requests and stores a provider-specific checkpoint alongside the original conversation.
The next request replays the latest compatible checkpoint and its following messages.
Canonical runs remain stored for model switching and portable text compaction.
See [native compaction eligibility](#native-compaction) for supported routes and fallback conditions.
Text compaction runs before the reply when history exceeds the hard replay budget or when explicitly requested.

You can tune compaction behavior with these settings:

- Use `replay_window_tokens` to cap persisted replay and required-compaction planning below the model's real context window without lowering the provider request limit.
- Use `reserve_tokens` to leave hard-budget headroom.
- Use `model` to choose the summary model.
- Use `fallback_model` to name a different model config retried once when the summary model refuses for safeguards; the same input is reused when it fits, otherwise it is rebuilt under the fallback model's own context budget, and after success that model serves the remaining chunks.
- Use `timeout_seconds` to bound each primary, retry, or fallback summary request; it defaults to 600 seconds, while an explicitly shorter provider timeout remains the stricter cap.
- Set `enabled: false` to disable automatic native and pre-reply text compaction for this agent.

When the active runtime model window is known, replay safety uses the smaller of it and `replay_window_tokens`.
When that model window is unknown, an explicit `replay_window_tokens` still supplies the replay-planning window.
Each compaction summary input chunk is sized independently from the selected compaction model's real `context_window`, after reserve, prompt overhead, and a safety margin.
Text compaction requires the resolved summary input budget to exceed 2,000 tokens.
With the default `reserve_tokens`, this makes text compaction unavailable when the compaction model's context window is roughly 10,000 tokens or smaller; lowering `reserve_tokens` restores availability for such small windows.
If you set `compaction.model`, that summary model must also define its own `context_window`, but only for the durable summary-generation pass.
`compaction.fallback_model` must also name a configured model with its own `context_window`; a fallback naming the summary model's alias, or another alias resolving to the same provider and model ID, is ignored because it would resend the refused request to the same model.
If the current reply needs required compaction to preserve usable history, MindRoom sends `Compacting history...`, compacts before the model call, and edits that same notice with the result.
Manual `compact_context` records a durable request that runs before the next reply in the same conversation scope.
Manual `compact_context` remains available when a compaction model and context window are configured and the resolved summary input budget exceeds 2,000 tokens.
MindRoom does not run a separate background post-response compaction path.
It always plans the replay that is safe for the current model call when the active runtime model has a known `context_window`.
That replay planner can keep configured replay, reduce raw replay, fall back to summary-only replay, or disable persisted replay for the run.
Portable text compaction rewrites the persisted Agno session in SQLite.
Older compacted runs move out of `session.runs` into the conversation database's compaction archive and are replaced in replay by the merged `session.summary`.
The archive keeps every compacted run and each intermediate summary, so compaction itself deletes no stored history; runs that releases before the archive deleted stay lost.
Redaction and conversation deletion are the only removals from the archive.
Redacting a Matrix event that a compacted run consumed rolls compaction back to just before that run, removing that run and everything after it, instead of clearing the whole conversation.
Summaries compacted before the archive existed record no per-run provenance, so redacting an event they may contain still clears that conversation's summaries, the runs archived after them, and its live runs.

## Team History and Compaction

`num_history_runs` and `num_history_messages` are mutually exclusive, just like the agent-level settings.
When a named team sets these fields, the team scope uses the team-owned policy instead of inheriting one member's history policy.

Team-scoped compaction supports `enabled`, `threshold_tokens`, `threshold_percent`, `replay_window_tokens`, `reserve_tokens`, `model`, `fallback_model`, and `timeout_seconds`.
When the active team model has a known `context_window`, MindRoom always computes a final replay plan for the shared team scope and reduces or disables persisted replay for the run when needed.
Automatic text compaction is enabled by default through `defaults.compaction`, but it runs only when raw history exceeds the hard replay budget for the next reply.
`threshold_tokens` and `threshold_percent` set a soft trigger budget for planning metadata and compaction notices.
Crossing that soft trigger while still within the hard budget leaves the stored session unchanged and relies on replay fitting.

You can tune team-scoped compaction behavior with these settings:

- Use `replay_window_tokens` to cap persisted replay and required-compaction planning below the model's real context window without lowering the provider request limit.
- Use `reserve_tokens` to leave hard-budget headroom.
- Use `model` to choose the summary model.
- Use `fallback_model` to name a different model config retried once when the summary model refuses for safeguards; the same input is reused when it fits, otherwise it is rebuilt under the fallback model's own context budget, and after success that model serves the remaining chunks.
- Use `timeout_seconds` to bound each primary, retry, or fallback summary request; it defaults to 600 seconds, while an explicitly shorter provider timeout remains the stricter cap.
- Set `enabled: false` to disable automatic pre-reply compaction for a team.

When the active team model window is known, replay safety uses the smaller of it and `replay_window_tokens`.
When that model window is unknown, an explicit `replay_window_tokens` still supplies the replay-planning window.
Each compaction summary input chunk is sized independently from the selected compaction model's real `context_window`, after reserve, prompt overhead, and a safety margin.
Text compaction requires the resolved summary input budget to exceed 2,000 tokens.
With the default `reserve_tokens`, this makes text compaction unavailable when the compaction model's context window is roughly 10,000 tokens or smaller; lowering `reserve_tokens` restores availability for such small windows.
If you set `compaction.model`, that summary model must also define its own `context_window`, but only for the durable summary-generation pass.
`compaction.fallback_model` must also name a configured model with its own `context_window`; a fallback naming the summary model's alias, or another alias resolving to the same provider and model ID, is ignored because it would resend the refused request to the same model.
Manual `compact_context` remains available when a compaction model and context window are configured and the resolved summary input budget exceeds 2,000 tokens.
Compaction uses an in-room lifecycle notice that is edited in place.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `num_history_runs` | No | `defaults.num_history_runs` | Number of prior team-scoped runs to replay |
| `num_history_messages` | No | `defaults.num_history_messages` | Max messages from team-scoped history replayed into the next run |
| `max_tool_calls_from_history` | No | `defaults.max_tool_calls_from_history` | Max tool call messages replayed from team-scoped history |
| `compaction` | No | `defaults.compaction` | Team-scoped required-compaction overrides |

Dynamic teams do not have a named `teams:` entry, so their history replay and compaction policy comes from `defaults`, not from any participating agent's overrides.
