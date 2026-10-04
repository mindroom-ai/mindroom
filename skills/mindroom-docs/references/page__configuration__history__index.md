# History & Compaction

This page covers how much earlier conversation an agent or team sees on each reply, how a model's `context_window` keeps that history within the provider limit, and how compaction summarizes old history so long conversations keep working.
Use it to tune replay depth, set up or disable compaction, or explain why an agent forgot earlier context or posted a `Compacting history...` notice.

## History Settings

Set these fields in `defaults` or on an agent; teams use the same fields except `compress_tool_results` (see [Team History and Compaction](#team-history-and-compaction)).

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `num_history_runs` | int, >= 1 | `null` | Number of prior runs to replay as history (`null` = all). Mutually exclusive with `num_history_messages` |
| `num_history_messages` | int, >= 1 | `null` | Maximum messages replayed from history (`null` = no limit). Mutually exclusive with `num_history_runs` |
| `compress_tool_results` | bool | `null` | Compress tool results in history to save context. Inherits `defaults.compress_tool_results` (default `false`). On Anthropic and Vertex AI Claude models, `true` can invalidate prompt caching |
| `max_tool_calls_from_history` | int, >= 0 | `null` | Maximum tool-call messages replayed from history. `null` inherits `defaults.max_tool_calls_from_history`; no limit when that is also `null` |
| `compaction` | object | `defaults.compaction` | Compaction overrides; see [Agent Compaction Settings](#agent-compaction-settings) |

An agent or team that leaves both `num_history_runs` and `num_history_messages` at `null` inherits both from `defaults`; setting either one replaces both defaults.
History replay is unlimited only when the inherited values are also `null`.

## Context Window

Set `context_window` on each model to the provider's actual limit (see [Models](https://docs.mindroom.chat/configuration/models/)).
When the active model has a known `context_window`, MindRoom fits replayed history to it before each reply, reducing raw replay, replaying only the saved summary, or skipping saved history for that run when needed.
Your configured `num_history_runs` stays unchanged; the reduction applies only to the run that needs it.
Without a known `context_window` (and without `compaction.replay_window_tokens`), history is not fitted and automatic text compaction is unavailable.

On `vertexai_claude` models, a known `context_window` also checks every provider request, including follow-ups after tool results.
A request that would exceed the window drops its oldest replayed history turns for that request only and logs a warning.
When the current turn alone cannot fit, the request fails with a provider error instead of being sent oversized.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5-5
    context_window: 1000000  # 1M tokens

defaults:
  compaction:
    replay_window_tokens: 200000  # Keep replayed history around a smaller operational window
```

## Agent Compaction Settings

Compaction is enabled by default through `defaults.compaction`.
It summarizes older history so later replies keep useful context instead of losing it to replay fitting.
Supported OpenAI and Claude routes use [native compaction](#native-compaction) inside ordinary requests; every other route uses portable text compaction.
Text compaction runs before a reply only when history exceeds the hard replay budget, or when [requested manually](#manual-compaction).
While it runs, the agent posts `Compacting history...` and edits that notice with the result.
If compaction fails, the notice reads `Compaction failed; continuing with trimmed history.` with the reason, and the reply continues with fitted history.

Set these fields under `defaults.compaction`, an agent's `compaction`, or a team's `compaction`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | `false` disables automatic native and pre-reply text compaction for the scope |
| `threshold_tokens` | int, >= 1 | `null` | Native provider trigger in tokens; on text-only routes a soft planning threshold that does not compact by itself while history still fits. Mutually exclusive with `threshold_percent` |
| `threshold_percent` | float, > 0 and < 1 | `null` | The same trigger as a fraction of the effective replay window. When neither threshold is set, the trigger is 80% of that window |
| `replay_window_tokens` | int, >= 1 | `null` | Cap replayed history and compaction planning below the model's real `context_window` without lowering the provider request limit. The smaller of the two applies; when the model window is unknown, this value alone sets the replay window |
| `reserve_tokens` | int, >= 0 | `16384` | Headroom kept free for the current prompt, tool definitions, and output |
| `model` | string | `null` | Model config name used to write summaries; `null` uses the active model. Must define its own `context_window` |
| `fallback_model` | string | `null` | Different model config retried once when the summary model refuses for safeguards; after success it writes the remaining summaries for that compaction. Must define its own `context_window` |
| `timeout_seconds` | float, > 0 | `600` | Maximum seconds for each summary request; a shorter provider timeout still applies |

An agent or team `compaction` block overrides `defaults.compaction` field by field, and setting a field to `null` clears the inherited value.
Setting either threshold in an override replaces an inherited value of the other.
A `compaction` block that sets any field enables compaction for that scope, even when `defaults.compaction` disables it, unless it sets `enabled` to `false` or `null`.
A block that only clears `model` or `fallback_model` with `null` keeps the inherited enabled state.
Set `defaults.compaction: null` or `enabled: false` to turn compaction off everywhere that does not override it.

`model` and `fallback_model` must name entries under `models:` that define `context_window`; otherwise config validation fails with `Compaction model references unknown models: ...` or `Explicit compaction model references require a model with context_window: ...`.
A `fallback_model` that resolves to the same provider and model ID as the summary model is ignored.

Text compaction needs more than 2,000 tokens of summary input after `reserve_tokens` and prompt overhead are subtracted from the summary model's `context_window`.
With the default `reserve_tokens`, a summary model with a context window of roughly 10,000 tokens or less cannot compact; lower `reserve_tokens` to make it work.

### Compacted history and redaction

Text compaction moves older runs into an archive in the conversation database and replays a merged summary in their place.
Compaction deletes no stored history; only Matrix redaction and conversation deletion remove archived runs.
Redacting a message, including one the agent never answered, removes the first stored run that read, answered, or wrote it, and every later run, from an agent's or team's history before that agent or team next replies in the conversation.
Until then the text stays in local storage; a reply that continues after a tool approval, and a voice-call reply, do not remove it first.
Redacting a Matrix event that a compacted run consumed rolls compaction back to just before that run, removing that run and everything after it, instead of clearing the whole conversation.

If a reply fails with `Saved conversation summary exceeds the available history budget`, the saved summary no longer fits beside the current prompt.
This can happen after switching to a model with a smaller context window or when system instructions and the current message leave too little room.
Choose a model with a larger context window or shorten the prompt; MindRoom never silently drops or truncates the saved summary.

### Native compaction

MindRoom supports automatic [OpenAI Responses compaction](https://developers.openai.com/api/docs/guides/compaction) and [Claude compaction](https://platform.claude.com/docs/en/build-with-claude/compaction).

- **OpenAI**: GPT-5.3 Codex, GPT-5.4, and GPT-6 model families on the official Responses endpoint or the Codex login backend.
  An explicit `store: true`, background mode, an alternate OpenAI endpoint, or a custom context-management request keeps the model on text compaction.
- **Claude**: direct Anthropic and Vertex AI Claude on Sonnet 4.6 and later, Opus 4.6 and later, Fable, and Mythos models.
  This is a provider beta and needs a trigger of at least 50,000 tokens.
  An authored `compact_20260112` context-management policy keeps its own trigger and instructions, and `pause_after_compaction: true` is rejected because MindRoom needs the provider to continue its response.
- Gemini, Chat Completions, and all other providers use text compaction.

Native compaction also requires compaction to be enabled, replay of all history (no `num_history_runs` or `num_history_messages`), no `max_tool_calls_from_history`, and a trigger above the current prompt size and below the hard request limit.
An explicit `compaction.model`, a [scheduled task](https://docs.mindroom.chat/scheduling/) with a `history_limit`, or a request already over the hard budget uses text compaction instead.
Native checkpoints survive restarts and are tied to their provider, model, and endpoint; after a model switch, the conversation is rebuilt from its full stored history.

### Manual compaction

Add the `compact_context` tool to an agent's `tools` to let it request compaction itself.
The request runs before the next reply in the same conversation and always uses text compaction.
It requires a summary model with a `context_window` and the same 2,000-token summary input budget; otherwise the tool returns `Error: Compaction is unavailable for this scope because ...` with the reason.

## Team History and Compaction

Named teams accept `num_history_runs`, `num_history_messages`, `max_tool_calls_from_history`, and `compaction`, with the same meaning, limits, and mutual exclusion as for agents.
The team's shared history uses the team's own settings, never a member agent's, and the team model's `context_window` sets its replay budget.

[Ad hoc teams](https://docs.mindroom.chat/configuration/teams/#ad-hoc-teams) have no `teams:` entry, so their history and compaction settings come from `defaults`, not from any participating agent.
