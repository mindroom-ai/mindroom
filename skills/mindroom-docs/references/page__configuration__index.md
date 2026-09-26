# Configuration

MindRoom is configured through a `config.yaml` file. This section covers all configuration options.

## Configuration File

MindRoom searches for the configuration file in this order (first match wins):

1. `MINDROOM_CONFIG_PATH` environment variable (if set)
2. `./config.yaml` (current working directory)
3. `~/.mindroom/config.yaml` (home directory)

Data storage (`mindroom_data/`) is placed next to the config file by default.

You can also validate a specific file directly:

```bash
mindroom config validate --path /path/to/config.yaml
```

## Adaptive Agent Participation

By default, threads with multiple human participants require explicit agent mentions.
Set `agents.<name>.participation` to let an agent already involved in a thread decide whether to respond to an untagged message or stay silent.
The check runs only when all of these conditions hold:

- Participation is enabled for that agent.
- The message is in a thread with at least two human participants, including the current sender.
- The message does not explicitly mention an agent or another human.
- The particular individual agent has already replied in that thread and still has permission to reply to the sender.

MindRoom must have the thread history available to establish these conditions.
Agents that are merely present in the room do not judge or join the conversation.
If several opted-in individual agents have replied in the thread, each makes its own participation decision.
For example, with 50 agents in a room and two already involved in a thread, only those two are eligible to judge, and each must opt in.
Explicit agent mentions follow normal reply rules; an explicit human mention bypasses adaptive participation too.
The setting follows the agent into every room where it has permission to reply, including ad hoc rooms joined by invitation.
The room does not need an entry in `rooms` or the agent's auto-join `rooms` list.
Participation is configured only per agent; there are no room overrides.

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      debounce_seconds: 3.0
      instructions: "Reply when you can help; leave human conversation uninterrupted."
```

The pause applies only to eligible untagged text in threads with at least two human participants, including the current sender.
Configured human aliases count as one participant across their transport identities.
Registered agents, the internal service account, and configured `bot_accounts` do not count as humans.
The pause defaults to three seconds and accepts finite values from zero to thirty seconds.
A burst from one sender becomes one turn after the pause; explicit agent or human mentions bypass adaptive selection and end that sender's pending pause immediately.
Single-human conversations keep their usual response behavior.
A decision to stay silent produces no text reply and does not record a completed assistant response.
To acknowledge a deliberate decline, set `decline_reaction` to a reaction such as `"👍"` or `"👀"`:

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      decline_reaction: "👍"
```

Omit `decline_reaction` or set it to `null` to keep declines invisible.
The reaction must be a nonblank string of at most 64 characters; composite emoji are supported.
Each declining agent reacts once to the latest message in the coalesced turn, using the same behavior for all judgment backends.
Failed checks, preparation errors, and cancellation do not trigger a decline reaction.
Reaction delivery is best effort: failed sends are logged without generating an error reply, and replays use a stable Matrix transaction ID.
Choose an emoji appropriate for the agent's conversations: 👍 can imply agreement with the message.

By default, the replying agent's own model makes the decision using its prepared conversation and tool definitions, with tool execution disabled.
If that same-model check fails, the agent stays quiet.
The following provider restrictions apply to this same-model check; a valid decision from a separate judgment backend bypasses the check.
With Claude tools, only the tool and system caches are reusable across the check and reply because disabling tool selection changes tool choice.
Ollama omits tool schemas during the check because its API cannot disable tool selection while retaining them.
Gemini native tools are omitted during the check; its explicit context caches, OpenAI Chat search-only requests, OpenRouter automatic web search, and Groq Compound systems cannot be checked safely and stay quiet.
Cancellation before approval does not create an interruption notice.
If an interrupted turn already owns a visible response, recovery retains its approval and finishes that response.
Commands and scheduled work do not opt into adaptive participation.
Omit `participation` or set it to `null` to keep the default behavior for an agent.
An empty mapping (`participation: {}`) enables adaptive participation with default settings.
The former top-level `room_participation` setting is no longer accepted.

### Participation judgment backends

Participation can use a separate decision model while the configured agent model generates the reply.
Set `judgment.provider` to `llm` for a configured model alias or `typesafe` for System One.
Both backends receive the same participation question, criteria, agent participation guidance, and minimized conversation context, and return a yes/no decision or abstain.
Their predictions can differ; switching providers preserves the decision contract and response lifecycle, not identical judgments.

For a dedicated LLM, reference an existing alias in your `models:` configuration:

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      instructions: "Reply when you can help; leave human conversation uninterrupted."
      judgment:
        provider: llm
        model: fast
        timeout_seconds: 5
```

The decision model uses that alias's normal provider credentials and can be cheaper than the replying agent's model.
It receives no executable tools, agent system prompt, or agent memory.
It must return a structured boolean decision; an explicit abstention or invalid response triggers the existing in-model fallback.
No self-reported confidence score is requested or treated as a probability.
Provider modes whose automatic native tools cannot be disabled are refused for decision calls.

To switch to System One, change the judgment settings and set `TYPESAFE_API_KEY` in the instance environment or config-adjacent `.env`:

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      instructions: "Reply when you can help; leave human conversation uninterrupted."
      judgment:
        provider: typesafe
        threshold: 0.8
        timeout_seconds: 1.5
```

The TypeSafe client pins `jev-1.13.0` and accepts only the requested question's documented [Noul probability response](https://docs.typesafe.ai/api).
A probability at or above `threshold` approves participation; a lower value stays quiet.
The threshold accepts finite values from zero to one and defaults to `0.8`; this default has not been calibrated against a representative participation corpus.
`threshold` is specific to TypeSafe and is rejected for the LLM backend.

Omitting `judgment` (or setting it to `null`) preserves the existing reply-model decision, even when a TypeSafe key is present.
Selecting either backend authorizes sending agent participation guidance and up to eight recent prepared user/assistant messages to that backend.
System prompts, transient memory/hook context, tool definitions/results, and media are excluded; Matrix message metadata is replaced with request-local speaker aliases.
Message bodies can still contain identifying or private text.
Media, attachment references, compressed or non-text content, detected secrets, malformed Unicode, and oversized input trigger fallback without sending that context to the separate judge.
Requests are bounded to 16 KB; TypeSafe responses are bounded while streaming, and LLM decision text is validated against a 64 KiB limit after the provider returns.

Both backends share a process-wide limit of eight concurrent judgments and one per instance/agent owner, with no waiting queue.
`timeout_seconds` accepts finite positive values up to thirty seconds; defaults are five seconds for LLMs and 1.5 seconds for TypeSafe.
This is an additional deadline after participation debounce and includes model loading, inference, and parsing; it does not cover the fallback model call.
There are no application-level judgment retries; configured LLM providers retain their SDK behavior within the deadline.
Missing credentials, exhausted concurrency, timeout, provider errors, malformed output, or TypeSafe model drift fall back to the replying agent's own decision model.
If that fallback also fails, the agent stays quiet.
Cancellation propagates without starting a fallback call.
Valid judgments bypass the reply provider's tool-free decision restrictions, so providers with automatic native tools can answer after approval.
Only one decision settles per turn, including retries; recovery of an already-visible response retains its approval.

Judgment outcome logs record backend, model, decision, latency, token usage, input size, and failure category; TypeSafe also records probability and threshold.
These outcome logs omit request text and credentials; separately enabled LLM request debug logging still follows the normal model configuration.
Use these measurements alongside observed decision quality and provider pricing to compare backends; token counts alone do not establish cost or quality advantages.

## Mid-Turn Coalescing

When another human message arrives during an active response, MindRoom normally adds a notice after a tool batch asking the agent to stop making new tool calls and summarize its progress.
Opt-in `agents.<name>.mid_turn` judgments can let the original task finish when the queued messages are clearly unrelated or simple acknowledgements.
This is separate from participation eligibility and the debounce used to group incoming messages.
Each agent can configure a required `judgment` object, optional `instructions` string (default `""`), and optional `defer_reaction` (default `null`).
The `llm` judgment requires `provider: llm` and a `model` string naming an existing alias; its numeric `timeout_seconds` defaults to `5.0`.
The `typesafe` judgment requires `provider: typesafe`; its numeric `threshold` defaults to `0.8` (range `0`–`1`) and `timeout_seconds` to `1.5`.
Both backends require a positive timeout of at most `30` seconds and reject unknown fields.

```yaml
agents:
  helper:
    display_name: Helper
    mid_turn:
      instructions: Continue for acknowledgements; wrap up for corrections or changed requirements.
      defer_reaction: "👀"
      judgment:
        provider: llm
        model: fast  # An existing alias under models
        timeout_seconds: 5
```

To use TypeSafe instead, configure the same agent with:

```yaml
agents:
  helper:
    display_name: Helper
    mid_turn:
      judgment:
        provider: typesafe
        threshold: 0.8
        timeout_seconds: 1.5
```

TypeSafe also requires `TYPESAFE_API_KEY`.
The setting follows the agent's Matrix user across every authorized room, including ad hoc rooms, just like participation.
Omitting `mid_turn` or setting it to `null` preserves the normal unconditional wrap-up notice.
There are no room overrides; the retired `room_mid_turn` configuration is rejected.
Set `defer_reaction` to acknowledge queued messages when the judge lets the active task finish first.
For example, `"👀"` means the message was seen and deferred; it remains queued for a later turn.
Omit the setting or use `null` to keep deferrals invisible.
Like participation's `decline_reaction`, the key must be a nonblank string of at most 64 characters.
Each message is acknowledged at most once per active response, and stable Matrix transaction IDs prevent duplicate reactions on replay.
Judgments that request wrap-up, fail, time out, are cancelled, or are superseded do not trigger the reaction.
Reaction delivery is best effort; failures do not change the judgment, and a correction arriving during delivery still requests wrap-up.

The judge asks whether any queued message requires an immediate change, pause, or stop.
Acknowledgements, praise, thanks, "continue," and "do not interrupt" allow continuing; corrections and relevant changes take priority, even alongside praise.
Unrelated requests wait for a later turn unless the user requests an immediate switch.
For TypeSafe, continuation requires `1 - P(interrupt) >= threshold`; the default `0.8` permits interruption probabilities up to `0.2`.
For the LLM backend, an explicit `false` answer permits continuation.
Abstentions, timeouts, missing credentials, exhausted capacity, and backend errors retain the normal wrap-up behavior.
Judgments reuse the shared participation concurrency limits and backend deadlines.

The check runs between completed tool batches, including resumed approved tools, without interrupting a tool already running.
It receives the active request text, preceding public conversation text, up to eight pending human messages, and the configured guidance.
Conversation history is refreshed after acquiring the response lock and stops before the active request, so resumed requests such as "continue" retain earlier task instructions and accepted corrections.
Later messages remain separate queued input; private model history is not used.
For interactive selections, the active request includes the original question and selected option.
Each pending message includes a snapshot of the active reply text last acknowledged by Matrix when that message entered the queue.
That snapshot includes published partial text and tool names when those names appear in the visible reply, but excludes buffered text and later edits.
It represents the server-published view at queue admission, not proof of which update the sender had read on their device.
Private tool results and arguments, system prompts, memory, attachment contents, and rich tool-trace metadata are excluded.
Missing or partial conversation history, earlier media, more than 63 earlier messages, and context exceeding the 16 KB request limit retain wrap-up rather than silently dropping earlier instructions.
Room-mode turns currently have no public conversation snapshot and retain normal wrap-up; empty history is sufficient only for a proven new thread.
The agent's published prose can describe its findings; that visible text is included even when it summarizes tool results.
Tool side effects are treated as unknown; visible progress does not establish that continuing is harmless.
An existing reply whose visible text is unavailable retains wrap-up until a new Matrix update is acknowledged.
Missing request text, unavailable progress, media or attachment references, detected credentials, malformed Unicode, and requests exceeding 16 KB retain wrap-up without sending incomplete context to the judge.
Message text can still contain identifying or private information.

A finish decision is reused for the same pending messages within the active response.
Subsequent streaming updates do not replace those messages' frozen progress snapshots.
A later queued message requires a new decision, and a queue change during inference cannot inherit approval for unseen input.
Once a wrap-up notice is sent, later messages cannot reverse that handoff.
The queued messages remain queued and are handled through the existing dispatch path after the active response releases its lock.
The notice requests a handoff from the model; it does not forcibly cancel tools, abort a response, or inject the queued text into the running model.
Explicit stop handling and tool-approval requirements remain unchanged.
Teams retain the normal wrap-up behavior and do not inherit a member agent's mid-turn settings.

## Splitting the Configuration Into Multiple Files

Large configs can be split across multiple files with Home-Assistant-style include tags instead of keeping one monolithic `config.yaml`.

| Tag | Result |
|-----|--------|
| `!include rel/path.yaml` | The parsed content of that YAML file, with nested include tags resolved recursively |
| `!expand rel/path.yaml` | Splices the file's YAML list into the surrounding list at this item; a MindRoom extension for shared tools, instructions, and other lists |
| `!include_text rel/path.md` | The file's raw text as a string (UTF-8, one trailing newline stripped); a MindRoom extension for long prompt or instruction blocks |
| `!include_dir_list rel/dir` | A list with one item per YAML file in the directory |
| `!include_dir_named rel/dir` | A mapping of filename-without-extension to each file's parsed content |
| `!include_dir_merge_list rel/dir` | The concatenation of the lists contained in each file |
| `!include_dir_merge_named rel/dir` | The merge of the mappings contained in each file |

A realistic split keeps each agent in its own file and long prompts in Markdown:

```yaml
# config.yaml
agents: !include_dir_merge_named agents/
models: !include models.yaml

defaults:
  tools: [scheduler]
  markdown: true
```

```yaml
# agents/researcher.yaml
researcher:
  display_name: Researcher
  role: Deep research and analysis
  model: default
  tools: !include _shared/web_tools.yaml
  instructions:
    - !include_text ../prompts/researcher.md
```

```yaml
# agents/_shared/web_tools.yaml
- duckduckgo
- website
- browser
```

Relative paths resolve against the directory of the file containing the tag, so nested include trees compose naturally.
Absolute paths are rejected, and every include must stay inside the top-level config file's directory (symlinks and `..` are resolved before the check), so config edits can never read arbitrary host files.
Directory includes recurse into subdirectories, take only `.yaml`/`.yml` files, and process them in lexicographic order of their relative path.
Files and directories whose name starts with `.` or `_` are skipped by directory includes.
Explicit include paths may not contain hidden components: any path component starting with `.` (other than the `.` and `..` navigation components) is rejected, so `!include_text .env` fails with a clear include error.
The `_` convention gives shared snippet files a home inside included trees, such as `agents/_shared/web_tools.yaml` above, which is pulled in via an explicit `!include`.
Use such snippets instead of YAML anchors when sharing values between files, because YAML anchors are scoped to a single file and cannot cross an include boundary.

### Expanding Shared Lists

Use `!expand` as a list item to reuse a shared list and add items for individual agents:

```yaml
# agents/researcher.yaml
researcher:
  role: Deep research and analysis
  tools:
    - !expand _shared/web_tools.yaml
    - calculator
```

With the `agents/_shared/web_tools.yaml` file above, this resolves to `tools: [duckduckgo, website, browser, calculator]`.
The compact form `tools: [!expand _shared/web_tools.yaml, calculator]` works too.
You can place several expansions anywhere in a list, with ordinary items before, between, or after them.
Shared files can themselves use `!expand` and other include tags recursively, with paths relative to each containing file.
Expansion preserves order and duplicates and only splices one level; nested lists inside the shared list remain nested.
An ordinary `- !include tools.yaml` still inserts the included value as one item, even when that value is a list.
The expanded file must resolve to a YAML list: `[]` adds nothing, while an empty file, `null`, a scalar, or a mapping raises an error.
`!expand` requires a relative file path and is only valid as a list item; use `tools: !include tools.yaml` to replace an entire list.
Expanded files follow the same path restrictions, cycle checks, source tracking, and hot reload behavior as other included files.

### Include Errors and Hot Reload

Error handling is strict so a broken split fails loudly:

- Include cycles are rejected with the full chain (`config.yaml -> agents/a.yaml -> config.yaml`).
- Missing files and directories are reported with the including file and line.
- `!include_dir_merge_named` raises on duplicate keys across files, naming both files, instead of silently letting the later file win as Home Assistant does.
- `!include_dir_named` likewise raises on duplicate filename stems across subdirectories.
- An empty included file resolves to `null` under `!include`, and contributes nothing to directory includes.

Hot reload watches every included file, so editing any file in the include tree triggers the same config reload as editing `config.yaml`.
A reload fires only after the watched files have been quiet for one full scan interval (about one second of added latency), so a multi-file update from `git pull` or a sync tool lands completely before the reload reads the tree.
A watched file vanishing between scans also defers a pending reload by one scan, so a delete-and-recreate save cannot be read mid-rename.
Deleting an included file does not trigger a reload by itself: the watcher deliberately treats a missing file as an in-progress editor save and waits until it reappears or another watched file changes.
To remove an include file, delete the `!include` reference from the including file too — that edit triggers the reload.
If a reload fails, every file the failed attempt read stays watched alongside the last good set, so fixing a newly added include file — even one the last good config never referenced — triggers the retry reload.

When migrating an existing monolith, use `mindroom config resolve` to print the fully merged config with sorted keys, and diff the output before and after the split to prove equivalence:

```bash
mindroom config resolve > before.yaml
# split config.yaml into include files
mindroom config resolve > after.yaml
diff before.yaml after.yaml
```

The dashboard config editor operates on whole-config structured saves, which cannot be mapped back to individual include files.
When the configuration uses include tags, structured saves from the dashboard and self-config tools are rejected with an error asking you to edit the source files instead, including empty directory includes and include sources that fail to load.
Rejected structured saves return HTTP 409 with the machine-readable error code `config_composed_from_includes`, so clients can tell this permanent rejection apart from a retryable stale-write conflict.
If an existing source cannot be read or tokenized well enough to establish whether it uses includes, structured saves return HTTP 422 and ask you to repair the source files; raw YAML editing remains available.
`POST /api/config/load` reports the includes state in the `x-mindroom-config-uses-includes` response header, which the dashboard uses to show an up-front banner explaining that structured saves will be rejected.
The raw config editor (`GET`/`PUT /api/config/raw`) keeps operating on the top-level file's literal text, and its response flags when includes are in use.
`MINDROOM_CONFIG_TEMPLATE` seeding copies only the single template file, so bundled templates that use includes must ship the whole directory.

## MCP Servers

MindRoom can connect to external Model Context Protocol servers through the top-level `mcp_servers` block.
See [MCP](https://docs.mindroom.chat/mcp/) for transport-specific config, tool naming, examples, and agent setup.

## Tool Approval

Use the top-level `tool_approval` block to gate tool calls behind human approval in Matrix conversations.
Rules are evaluated in order and the first matching rule wins.
`match` is a case-sensitive glob over exposed function names, not toolkit identifiers.
Each rule must set exactly one of `action` or `script`.
Use `action: require_approval` to always pause the tool call and send a Matrix approval card.
Use `script: ./approval_scripts/review.py` to run `check(tool_name, arguments, agent_name) -> bool` and require approval only when it returns `True`.
`timeout_days` sets the default approval expiry window and can be overridden per rule.
Late approval decisions fail closed at the persisted deadline, while the periodic Matrix card sweep can take up to about one additional minute to show the expired state.
React to the approval card with `✅` to approve the tool call.
Reply to the approval card with a message to deny the tool call and record that text as the denial reason.
Only the original human requester can approve or deny their pending tool call.
Eligible interactive cards also offer auto-approval for 5, 10, or 30 minutes.
A timed approval accepts the original call, matching pending calls, and subsequent calls for the same room, thread, human requester, invoking agent, and exact tool operation; arguments may differ.
Subsequent matching calls do not publish another approval card.
For generic MCP dispatch, the server and remote tool name are part of the operation, so approving one remote operation does not approve every tool on that server.
Timed approval requires a canonical thread and complete reviewable arguments; native tool-authored confirmations and background-script approvals remain per-call.
The backend fixes the grant deadline when it accepts the originating call and preserves it across restarts; replaying an approval never extends or recreates the grant.
This deadline is separate from the pending card's approval timeout.
Only the original requester with current responder access can stop auto-approval using the originating card.
Expiry or revocation stops future automatic decisions without cancelling decisions already accepted.
Changes to configured bindings or room membership invalidate matching grants.
Clients show approval or revocation as submitted until a backend card edit acknowledges the durable change.
Approval cards show a redacted preview of the tool arguments in the `arguments` content field, and set `arguments_truncated: true` when that preview is shortened for display.
When the preview is truncated, the complete redacted arguments are delivered with the card so clients can render them behind a "show full arguments" expander and the call stays approvable.
They ride inline in a `full_arguments` content field when they fit the Matrix event, and otherwise as an uploaded JSON sidecar referenced by `full_arguments_url` plus `full_arguments_info` in plain rooms or `full_arguments_file` (standard Matrix encrypted-file schema) in encrypted rooms.
When the complete redacted arguments cannot be delivered — over the 2MB completeness cap or because the sidecar upload failed — the card sets `approvable: false` and any approve action is converted into a denial, because a human must be able to review exactly what would run.
Clients should disable or hide the approve action when `approvable` is `false`.
Approval cards are keyed to a durable Agno continuation that stores the exact paused tool calls and arguments.
While approval is pending, MindRoom releases the response coroutine, typing indicator, and per-conversation lock.
Current-format pending cards and recorded decisions recover after restart or configuration reload, and an accepted decision resumes the exact paused run through the normal stoppable response lifecycle.
Legacy, malformed, and orphan approval rows never authorize tool execution.
When Matrix transport is available, startup terminally expires or denies those old rows, and a temporary delivery failure leaves them recoverable for a later startup retry.
If recovery cannot prove whether a claimed tool already executed, it fails closed instead of risking a duplicate side effect.
If an entity account was removed, the router posts a related terminal notice because Matrix forbids it from editing the removed account's original waiting event, which can therefore retain its old pending decoration.
Agent-authored, system-authored, and configured bridge-bot-authored tool calls are denied instead of entering the approval flow.
OpenAI-compatible `/v1/chat/completions` has no approval transport, so any tool function that matches a required-approval rule, including script-based rules, is hidden from the `/v1` tool schema instead of being exposed and blocked later.

This partial example gates Slack message sending and file uploads, plus shell calls selected by the review script.
It does not gate every Slack operation, and the same function names in other toolkits also match.

```yaml
tool_approval:
  default: auto_approve
  timeout_days: 7
  rules:
    - match: send_message*
      action: require_approval
    - match: upload_file
      action: require_approval
    - match: run_shell_command
      script: ./approval_scripts/shell_review.py
      timeout_days: 3
```

## Environment Variables

### Core

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_CONFIG_PATH` | Path to `config.yaml` | `./config.yaml` → `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH` | Data storage directory | `mindroom_data/` next to config |
| `MINDROOM_SESSION_STORAGE_PATH` | Dedicated root for agent and team session SQLite databases. Relative paths resolve from the config directory; learning, workspaces, and other state remain under `MINDROOM_STORAGE_PATH` | `MINDROOM_STORAGE_PATH` |
| `MINDROOM_CONFIG_TEMPLATE` | Path to a config template. When set and `config.yaml` does not exist, MindRoom copies this template to the config path. Used in Docker containers to seed config from bundled templates | Same as config path |
| `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` | Optional base64-encoded 32-byte key for encrypted-at-rest credential files | unset |
| `LOG_LEVEL` | Logging level for `mindroom run` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `MINDROOM_LOGGER_LEVELS` | Optional comma- or semicolon-separated logger level overrides, for example `mindroom:DEBUG,httpx:WARNING,httpcore:WARNING,anthropic:INFO,nio:WARNING` | unset |
| `MINDROOM_LOG_FORMAT` | `text` for readable logs or `json` for structured logs; file output never receives terminal styling | `text` |
| `NO_COLOR` | Any nonempty value disables console colors and styled tracebacks, including in terminals; installed background services set this to `1` | unset |

For native startup, export `LOG_LEVEL`, `MINDROOM_LOGGER_LEVELS`, `MINDROOM_LOG_FORMAT`, and `NO_COLOR` in the process environment; the config-adjacent `.env` does not supply these logging controls.
The `mindroom run --log-level` option takes precedence over `LOG_LEVEL`.
Container `env_file`/`--env-file` injection supplies process variables and can therefore set these controls.

### Matrix

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://localhost:8008` |
| `MATRIX_SERVER_NAME` | Server name for federation | _(derived from homeserver)_ |
| `MATRIX_SSL_VERIFY` | Verify SSL certificates | `true` |
| `MINDROOM_DESKTOP_MATRIX_HOMESERVER` | Public Matrix URL printed by `!desktop setup` when it differs from the runtime's internal homeserver URL | `MATRIX_HOMESERVER` |
| `MINDROOM_DESKTOP_CLOUDFLARE_ACCESS` | Include `--cloudflare-access` in the Desktop login and pairing commands printed by `!desktop setup` | `false` |

### API Keys

Set the API key for each provider you use in `config.yaml`:

| Variable | Provider |
|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint |
| `AZURE_OPENAI_API_VERSION` | Optional Azure OpenAI API version override |
| `OPENAI_API_KEY` | OpenAI |
| `GOOGLE_API_KEY` | Google (Gemini) |
| `OPENROUTER_API_KEY` | OpenRouter |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `ZAI_API_KEY` | Z.ai (GLM models) |
| `CEREBRAS_API_KEY` | Cerebras |
| `GROQ_API_KEY` | Groq |
| `OLLAMA_HOST` | Ollama (host URL, not a key) |
| `EMBEDDER_API_KEY` | Dedicated semantic-search embedder key (optional; falls back to the shared `OPENAI_API_KEY`) |
| `OPENAI_BASE_URL` | Base URL for OpenAI-compatible APIs (e.g., local inference servers) |

For `provider: openai` models, `OPENAI_BASE_URL` can come from the config-adjacent `.env` or the exported process environment; the exported value takes precedence.
A model's `extra_kwargs.base_url` overrides this environment setting, and `extra_kwargs.client_params.base_url` overrides the model endpoint when constructing SDK clients.

All API key variables also support a `_FILE` suffix for file-based secrets (e.g., `ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key`).
See [Model Configuration — File-based Secrets](https://docs.mindroom.chat/configuration/models/#file-based-secrets) for details.

### Codex CLI Subscription Auth

The `codex` provider does not use an API key environment variable.
Run `codex login` so `~/.codex/auth.json` contains ChatGPT OAuth tokens.
Set `CODEX_HOME` only if your Codex CLI state lives outside `~/.codex`.

### Operational

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_NAMESPACE` | Installation namespace for Matrix identity isolation (4–32 lowercase alphanumeric chars) | _(none)_ |
| `MINDROOM_PORT` | Port used by Google OAuth callback URL construction and deployment tooling; it does **not** change the API server bind port, which uses `mindroom run --api-port`. | `8765` |
| `MINDROOM_API_KEY` | API key for authenticating dashboard/API requests (`mindroom config init` auto-generates one; unset = open access) | _(none)_ |
| `MINDROOM_DASHBOARD_ALLOWED_HOSTS` | Comma-separated extra host names that requests without a credential may address and that their browser `Origin` may name, for an unauthenticated dashboard or `/v1` API; loopback names, IP addresses, and the hosts of `MINDROOM_PUBLIC_URL`, `MINDROOM_BASE_URL`, `MINDROOM_URL`, and `MINDROOM_SCRIPT_GATEWAY_URL` are always allowed | _(none)_ |
| `MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed credentialed dashboard CORS responses; cookie and trusted-upstream mutations still require the app's own origin | `http://localhost:3003`, `http://localhost:5173`, `http://127.0.0.1:3003`, `http://127.0.0.1:5173` |
| `MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS` | Set to `true` to allow every dashboard API origin while disabling credentialed CORS responses; without dashboard authentication, origins outside `MINDROOM_DASHBOARD_ALLOWED_HOSTS` are still refused | _(unset)_ |
| `MINDROOM_NO_AUTO_INSTALL_TOOLS` | Set to `1`/`true`/`yes` to disable automatic tool dependency installation | _(unset — auto-install enabled)_ |
| `MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS` | Seconds to wait for the homeserver to return a valid `/_matrix/client/versions` response at startup (`0` = wait indefinitely); MindRoom polls at a fixed interval until success or the deadline | _(wait indefinitely)_ |
| `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` | Positive seconds allowed for the first Matrix sync response | `600` |
| `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` | Finite positive seconds the sync watchdog and `/api/health` may defer while durable ingestion progress keeps advancing | `600` |
| `MINDROOM_SCRIPT_GATEWAY_URL` | Complete worker-reachable background-script gateway base URL, including `/api/script-gateway`; required for Kubernetes and for Docker unless a reachable `MINDROOM_PUBLIC_URL` is configured | _(none)_ |
| `MINDROOM_SCRIPT_GATEWAY_ISOLATED` | Operator attestation that the Kubernetes worker's configured script-gateway listener exposes only `/api/script-gateway`; required to admit Kubernetes background scripts and does not create network isolation itself | `false` |
| `MINDROOM_KUBERNETES_DEFAULT_SCRIPT_RESOURCE_PROFILE` | Default Kubernetes background-script profile (`small`, `standard`, or `large`) when `start_script` omits `resource_profile` | `small` |
| `MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON` | JSON object defining exact CPU and memory requests and limits for the fixed `small`, `standard`, and `large` background-script profiles | Built-in bounded profiles |
| `MINDROOM_SCRIPT_RETENTION_SECONDS` | Finite positive seconds to retain terminal background-script runs, tool-call receipts, approval rows, and durable approval records before lifecycle pruning | `2592000` (30 days) |
| `MINDROOM_WORKER_BACKEND` | Worker backend for tool execution (`static_runner`, `docker`, or `kubernetes`) | `static_runner` |

The ingestion grace bounds continuous catch-up; set it above the observed healthy catch-up duration for the deployment.
Raising it delays both watchdog cancellation and liveness failure only while durable ingestion progress keeps advancing.

### OpenAI-Compatible API

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_COMPAT_API_KEYS` | Comma-separated API keys for authenticating `/v1/*` requests | _(none — locked without this or the flag below)_ |
| `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | Set to `true` to allow unauthenticated `/v1/*` access (local dev only); such requests must address an allowed host (see `MINDROOM_DASHBOARD_ALLOWED_HOSTS`) and must not come from another site's page | _(unset — locked)_ |

See [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/) for the full auth matrix.

### Provisioning / Pairing

These are set automatically by `mindroom connect` and stored in `.env`:

| Variable | Description |
|----------|-------------|
| `MINDROOM_PROVISIONING_URL` | Provisioning service URL (e.g., `https://mindroom.chat`) |
| `MINDROOM_LOCAL_CLIENT_ID` | Client ID from hosted pairing |
| `MINDROOM_LOCAL_CLIENT_SECRET` | Client secret from hosted pairing |

### Frontend / Development

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_FRONTEND_DIST` | Override path to pre-built frontend assets | _(auto-detected)_ |
| `MINDROOM_AUTO_BUILD_FRONTEND` | Set to `0` to skip automatic frontend build | _(enabled)_ |
| `DOCKER_CONTAINER` | Set to `true` when running inside the packaged Docker image | _(unset)_ |
| `BROWSER_EXECUTABLE_PATH` | Path to browser executable for the browser tool | _(system default)_ |

### Vertex AI

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_VERTEX_PROJECT_ID` | Google Cloud project ID for Vertex AI Claude |
| `ANTHROPIC_VERTEX_BASE_URL` | Custom Vertex AI base URL |
| `CLOUD_ML_REGION` | Google Cloud region for Vertex AI |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID |
| `GOOGLE_CLOUD_LOCATION` | Google Cloud region |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google service account JSON |

Authenticate with `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS`.

### Worker / Sandbox

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_SANDBOX_PROXY_URL` | Sandbox proxy endpoint URL (static runner) | _(none)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Auth token for the sandbox proxy | _(none)_ |

See [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/) for the full list of `MINDROOM_SANDBOX_*` variables and Kubernetes backend variables (`MINDROOM_KUBERNETES_WORKER_*`).

### SaaS-Only

| Variable | Description | Default |
|----------|-------------|---------|
| `CUSTOMER_ID` | Tenant identity for worker key derivation (SaaS platform only) | _(none)_ |
| `ACCOUNT_ID` | Account identity for worker key derivation (SaaS platform only) | _(none)_ |

## Basic Structure

```yaml
# Agent definitions (at least one recommended)
agents:
  assistant:
    display_name: Assistant        # Required: Human-readable name
    role: A helpful AI assistant   # Optional: Description of purpose
    model: sonnet                  # Optional: Model name (default: "default")
    tools:                         # Optional: Agent-specific tools (merged with defaults.tools)
      - file
      - shell
      # - shell:                    # Per-agent tool config overrides (single-key dict):
      #     extra_env_passthrough: "DAWARICH_*"
    include_default_tools: true    # Optional: Per-agent opt-out for defaults.tools
    skills: []                     # Optional: List of skill names
    instructions: []               # Optional: Custom instructions
    rooms: [lobby]                 # Optional: Rooms to auto-join
    access:                        # Optional: Conversation access (omit to grant members of this agent's rooms)
      current_room_members: false  # Allow joined members of whatever room the message arrives in
      members_of_rooms: [lobby]    # Allow joined members of these managed rooms
      users: []                    # Matrix user IDs or glob patterns
    credential_managers: []        # Optional: Concrete Matrix IDs allowed to manage this agent's credentials
    accept_invites: true           # Accept all, none, or matching inviter ID patterns
    markdown: true                 # Optional: Override default (inherits from defaults section)
    worker_tools: [shell, file]    # Optional: Override default (inherits from defaults section)
    worker_scope: user_agent       # Optional: Reuse one proxied runtime per requester+agent
    file_access: workspace         # Optional: workspace or unrestricted (inherits from defaults section)
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)
    memory_backend: file           # Optional: Per-agent memory backend override (mem0, file, or none)
    memory_search:                 # Optional: Per-agent file-memory search override
      mode: semantic               # keyword or semantic; omitted fields inherit memory.search
      include:
        - memory/**/*.md
      include_entrypoint: false
    knowledge_bases: [docs]         # Optional: Assign one or more configured knowledge bases
    context_files:                 # Optional: Load files into each freshly built agent instance
      - SOUL.md
      - AGENTS.md
      - USER.md
      - IDENTITY.md
      - TOOLS.md
      - HEARTBEAT.md
  researcher:
    display_name: Researcher
    role: Research and gather information
    model: sonnet
  writer:
    display_name: Writer
    role: Write and edit content
    model: sonnet
  developer:
    display_name: Developer
    role: Write code and implement features
    model: sonnet
  reviewer:
    display_name: Reviewer
    role: Review code and provide feedback
    model: sonnet

# Model configurations (at least a "default" model is recommended)
models:
  default:
    provider: anthropic            # Required: anthropic, azure, bedrock_claude, openai, codex, kimi, llama_cpp, ollama, google, gemini, vertexai_claude, groq, cerebras, openrouter, deepseek, zai, or synthetic
    id: claude-sonnet-5            # Required: Model ID for the provider
  sonnet:
    provider: anthropic            # Required: anthropic, azure, bedrock_claude, openai, codex, kimi, llama_cpp, ollama, google, gemini, vertexai_claude, groq, cerebras, openrouter, deepseek, zai, or synthetic
    id: claude-sonnet-5            # Required: Model ID for the provider
    host: null                     # Optional: Host URL (e.g., for Ollama)
    extra_kwargs: null             # Optional: Provider-specific parameters
    context_window: null           # Optional: Needed on the active runtime model for replay safety; explicit compaction.model also needs its own window for summary generation

# Team configurations (optional)
teams:
  research_team:
    display_name: Research Team    # Required: Human-readable name
    role: Collaborative research   # Required: Description of team purpose
    agents: [researcher, writer]   # Required: List of agent names
    mode: collaborate              # Optional: "coordinate" or "collaborate" (default: coordinate)
    model: sonnet                  # Optional: Model for team coordination (default: "default")
    num_history_runs: 8            # Optional: Team-scoped replay policy
    num_history_messages: null     # Optional: Mutually exclusive with num_history_runs
    max_tool_calls_from_history: 6 # Optional: Limit replayed tool call messages
    max_tool_calls_per_turn: 200   # Optional: Coordinator tool calls per team turn, delegations included; members keep their own budgets (default: defaults.max_tool_calls_per_turn)
    compaction:                    # Optional: Team-scoped required-compaction overrides
      # Soft thresholds do not compact by themselves while history still fits.
      enabled: true
      threshold_percent: 0.8
      reserve_tokens: 16384
      timeout_seconds: 600
    rooms: []                      # Optional: Rooms to auto-join

# Router configuration (optional)
router:
  model: default                   # Optional: Model for routing (default: "default")

# Default settings for all agents (optional)
defaults:
  tools:                           # Default: ["scheduler"] (added to every agent; set [] to disable)
    - scheduler                    # Plain string or single-key dict with inline config overrides
  markdown: true                   # Default: true
  enable_streaming: true           # Default: true (stream responses via message edits)
  large_message_strategy: sidecar  # Default: sidecar (or split: deliver oversized responses as lossless segmented messages)
  streaming:
    update_interval: 5.0           # Default: 5.0 (steady-state seconds between streamed edits)
    min_update_interval: 0.5       # Default: 0.5 (fast-start seconds between early edits)
    interval_ramp_seconds: 15.0    # Default: 15.0 (set 0 to disable interval ramping)
    max_idle: 2.0                  # Default: 2.0 (event-driven idle ceiling before the next edit)
  learning: true                   # Default: true
  learning_mode: always            # Default: always (or agentic)
  max_preload_chars: 50000         # Hard cap for preloaded context from context_files
  max_tool_calls_per_turn: 1000    # Default: 1000 (tool calls one agent or team turn may execute; later calls return a tool error, and the turn ends with its text so far after this many plus two model requests)
  tool_output_auto_save_threshold_bytes: 51200  # Auto-save supported tool outputs larger than 50 KiB
  show_stop_button: true           # Default: true (global only, cannot be overridden per-agent)
  auto_resume_after_restart: true # Default: true (resume eligible interrupted threads after startup or replacement)
  num_history_runs: null           # Number of prior runs to include (null = all)
  num_history_messages: null       # Max messages from history (null = use num_history_runs)
  compress_tool_results: false     # Safer default; enabling can invalidate Anthropic/Vertex Claude prompt caches
  # Required compaction is enabled by default.
  # Soft thresholds do not compact by themselves while history still fits.
  # Set enabled: false to disable automatic pre-reply compaction globally.
  compaction:
    enabled: true
    threshold_percent: 0.8
    # Summary input chunks use the selected compaction model's context window.
    # Text compaction requires a resolved summary input budget greater than 2,000 tokens.
    replay_window_tokens: null     # Optional operational cap; does not change the model's real context window
    reserve_tokens: 16384
    timeout_seconds: 600           # Maximum seconds allowed for each compaction summary request
  max_tool_calls_from_history: null  # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true            # Default: true (show tool details inline; hidden mode still allows generic worker warmup copy)
  worker_tools: null               # Default: null (tool names to route through workers; null = use MindRoom's default routing policy, [] = disable)
  worker_scope: null               # Default: null (no runtime reuse; set shared/user/user_agent to enable)
  file_access: workspace           # Default: workspace (path tools stay in the agent workspace; unrestricted allows any path)
  worker_grantable_credentials: null  # Default: null (deny by default; list credential service names to make available inside isolated workers, e.g. [openai, github_private])
  allow_self_config: false         # Default: false (allow agents to modify their own config via a tool)
  thread_summary_model: null       # Default: null (uses the default model)
  thread_summary_temperature: 0.2  # Default: 0.2 (set null to omit temperature and use provider defaults)
  thread_summary_first_threshold: 1  # Default: 1 (first automatic thread summary after first message)
  thread_summary_subsequent_interval: 10  # Default: 10 (messages between later automatic thread summaries)

# defaults.tools are appended to each agent's tools list with duplicates removed.
# Set agents.<name>.include_default_tools: false to opt out a specific agent.
# defaults.streaming is also global-only and controls streamed message edit cadence.
# Tools can be plain strings or single-key dicts with per-agent config overrides.

# defaults.thread_summary_temperature controls automatic summaries on providers that support runtime temperature overrides.
# Set it to null to use provider defaults.
# GPT-6 Astra, Vertex Claude, Claude Opus 5, Sonnet 5, Fable 5.1, and direct Google Gemini 3.8 Flash and Gemini 3.5 Flash-Lite always use provider defaults.
# room_thread_summary_models can override defaults.thread_summary_model for a room alias or raw Matrix room ID.
#
# A thread's first trusted automatic summary call is summary-only.
# Its next scheduled refresh adds one to three tags when none exist.
# Later refreshes preserve those tags.
#
# worker_grantable_credentials lists shared credential service names that isolated workers may load.
# Google OAuth client and token services stay in the primary runtime and cannot be mirrored.
# Provider environment variables are not injected by this setting.
# google_vertex_adc is unsupported because isolated workers do not receive ADC files.

# Required compaction summarizes older runs of the active session.
# It uses one Matrix lifecycle notice that is edited in place.
# It runs before a reply when raw history exceeds the hard replay budget.
# It also runs before the next reply after a manual compact_context request.
# Otherwise MindRoom leaves the stored session unchanged and relies on replay fitting for that reply.
# It rewrites the stored session summary and moves compacted raw runs into the compaction archive.
# Agno then replays only the summary plus recent runs.
# Use __MINDROOM_INHERIT__ inside a tool override to clear one inherited authored field
# while keeping the rest of defaults.tools for that agent.
# See agents.md for the full per-agent tool configuration syntax.
# These thresholds only affect automatic thread summaries; manual `set_thread_summary`
# tool calls write immediately and pin the thread by default, which stops automatic
# summaries entirely until a `pin=False` call releases it.
# Automatic summaries are also skipped on threads tagged `resolved`.

# Memory system configuration (optional)
memory:
  backend: mem0                    # Global default backend (mem0, file, or none); agents can override with memory_backend
  team_reads_member_memory: false  # Default: false (when true, team reads can access member agent memories)
  embedder:
    provider: openai               # Default: openai (openai, ollama, huggingface, sentence_transformers)
    config:
      model: text-embedding-3-small  # Default embedding model
      credentials_service: null     # Optional: strict named credential binding (e.g. embedder)
      api_key: null                # Optional: explicit embedder key (highest priority; see docs/memory.md)
      host: null                   # Optional: For self-hosted
      dimensions: null             # Optional: Embedding dimension override (e.g., 256)
  llm:                             # Optional: LLM for memory operations
    provider: ollama
    config: {}
  file:                            # File-backed memory settings (when backend: file)
    path: null                     # Optional: fallback root for file memory paths
    max_entrypoint_lines: 200      # Default: 200 (max lines preloaded from MEMORY.md)
  search:                          # File-backed memory search settings
    mode: keyword                  # Default: keyword (keyword or semantic)
    include:                       # Root-relative globs below the effective file-memory root
      - memory/**/*.md             # Default: dated daily memory files
    include_entrypoint: false      # Default: false (MEMORY.md is already preloaded)
  auto_flush:                      # Background memory auto-flush (file backend only)
    enabled: false                 # Default: false (enable background flush worker)
    flush_interval_seconds: 1800   # Default: 1800 (loop interval)
    idle_seconds: 120              # Default: 120 (idle time before flush eligibility)
    max_dirty_age_seconds: 600     # Default: 600 (force flush after this many seconds dirty)
    stale_ttl_seconds: 86400       # Default: 86400 (drop stale flush-state entries older than this)
    max_cross_session_reprioritize: 5  # Default: 5 (same-agent dirty sessions reprioritized per prompt)
    retry_cooldown_seconds: 30     # Default: 30 (cooldown before retrying a failed extraction)
    max_retry_cooldown_seconds: 300  # Default: 300 (upper bound for retry cooldown backoff)
    batch:
      max_sessions_per_cycle: 10   # Default: 10 (max sessions processed per auto-flush loop)
      max_sessions_per_agent_per_cycle: 3  # Default: 3 (max sessions per agent per loop)
    extractor:
      no_reply_token: NO_REPLY     # Default: NO_REPLY (token indicating no durable memory)
      max_messages_per_flush: 20   # Default: 20 (max messages considered per extraction)
      max_chars_per_flush: 12000   # Default: 12000 (max chars considered per extraction)
      max_extraction_seconds: 30   # Default: 30 (timeout for one extraction job)
      include_memory_context:
        memory_snippets: 5         # Default: 5 (max MEMORY.md snippets for dedupe context)
        snippet_max_chars: 400     # Default: 400 (max chars per snippet)
#
# See docs/memory.md for full auto-flush behavior and tuning guidance.
#
# Set memory.embedder.provider: sentence_transformers to run embeddings in-process.
# MindRoom auto-installs that optional extra on first use.

# Knowledge base configuration (optional)
# Keys must be non-empty single path components, so do not use "", ., .., /, \, or line breaks in a knowledge base ID.
knowledge_bases:
  docs:
    description: Product documentation, support notes, and operating procedures
    # Default: semantic.
    # Use files to skip embeddings and expose workspace files.
    mode: semantic
    path: ./knowledge_docs          # Folder containing documents for this base (Pydantic default)
    watch: false                   # Direct external edits require reindex; API mutations still schedule refresh
    require_content_before_publish: false  # Keep a cold semantic index initializing until a managed file exists
    chunk_size: 5000               # Default: 5000 (max characters per indexed chunk)
    chunk_overlap: 0               # Default: 0 (overlapping characters between chunks)
    git:                           # Optional: Sync this folder from a Git repository
      repo_url: https://github.com/pipefunc/pipefunc
      branch: main
      poll_interval_seconds: 300  # Interval for background Git refresh scheduling
      lfs: false                   # Optional: enable Git LFS support (requires git-lfs on the runtime host)
      sync_timeout_seconds: 3600   # Optional: abort a hung git command after this many seconds
      skip_hidden: true
      include_patterns: ["docs/**"]  # Optional: root-anchored glob filters
      exclude_patterns: []
      credentials_service: github_private # Optional: service in CredentialsManager

# Voice message handling (optional)
voice:
  enabled: false                   # Default: false
  visible_router_echo: true        # Optional: show router voice progress or direct fallback
  stt:
    provider: openai               # Default: openai
    model: gpt-transcribe       # Default: gpt-transcribe
    credentials_service: openai    # Named credential service for speech
    api_key: null
    host: null
  intelligence:
    model: default                 # Model for mention normalization and light ASR cleanup

# Voice calls via Element Call / MatrixRTC (optional)
calls:
  enabled: false                   # Default: false
  profiles:                        # Complete reusable backend-specific profiles
    openai-realtime:
      backend: realtime
      model: gpt-realtime-2.1
      credentials_service: openai
      voice: marin
    openai-cascaded:
      backend: cascaded
      model: default                 # Optional: top-level model alias overriding room/agent models for call turns
      stt:
        provider: openai
        model: gpt-transcribe
        credentials_service: openai
      tts:
        provider: openai
        model: gpt-4o-mini-tts
        credentials_service: openai
  agents:                          # Profile name by enabled agent (at most one agent per room)
    assistant: openai-realtime
  livekit_service_url: null        # Optional override for .well-known discovery

# Internal MindRoom user account (optional, omit for hosted/public profiles)
# When present, defaults are: username: mindroom_user, display_name: MindRoomUser
mindroom_user:
  username: mindroom_user          # Set before first startup (account-creation request localpart only)
  display_name: MindRoomUser       # Can be changed later

# Access
administrators: []                 # Concrete Matrix IDs with platform and credential authority
room_defaults:
  join_policy: invite              # invite, knock, or public
  listed: false                    # Publish managed rooms in the room directory
  encrypted: false                 # Enable Matrix E2EE; enabling is irreversible
  invite_users: []                 # Automatic invitation roster only
  admins: []                       # Matrix room power level 100 only

# Non-overlapping authorization features remain under authorization.
authorization:
  config_command_enabled: false    # Enable !config for platform administrators
  aliases: {}                      # Map canonical Matrix user IDs to bridge aliases

# Managed room metadata (optional)
# Keys are managed room keys.
# Rooms listed here are created even before agents or teams are assigned.
rooms:
  lobby:
    display_name: Lobby
    description: Main assistant room
    join_policy: invite             # Optional override
    listed: false                   # Optional override
    encrypted: false                # Optional override
    invite_users: []                # Replaces room_defaults.invite_users
    admins: []                      # Replaces room_defaults.admins

# Room-specific model overrides (optional)
# Keys are room aliases, values are model names from the models section.
# Example: room_models: {dev: sonnet, lobby: gpt4o}
room_models: {}

# Room admins can override this authored choice at runtime with !room_model.
# Runtime overrides are stored under mindroom_data/tracking and do not modify this file.

# Room-specific automatic thread summary model overrides (optional)
# Keys are room aliases or raw Matrix room IDs, values are model names from the models section.
# Example: room_thread_summary_models: {lobby: haiku}
room_thread_summary_models: {}

# Non-MindRoom bot accounts to exclude from multi-human detection (optional)
# These accounts won't trigger the mention requirement in threads
bot_accounts:
  - "@telegram:example.com"

# Plugin paths (optional)
plugins: []

# Matrix Space grouping (optional)
matrix_space:
  enabled: true                    # Default: true (create a root Matrix Space for managed rooms)
  name: MindRoom                   # Default: "MindRoom" (display name for the root Space)

# Matrix sync transport (optional)
# classic uses /v3/sync and backfills limited-timeline gaps from /messages.
# sliding uses MSC4186 Simplified Sliding Sync on compatible homeservers.
# A durable store remains bound to the transport it first used.
matrix_sync:
  mode: classic                    # Default: classic
  sliding_timeline_limit: 100       # Per-room Sliding window, minimum 1
  max_response_bytes: 16777216      # Per-session HTTP response limit: 16 MiB
  max_pending_bytes: 67108864       # Per-session encoded pending output limit: 64 MiB

# Durable Matrix event journal (optional; effective store changes require restart)
event_journal:
  backend: sqlite                 # Default: sqlite; uses <storage>/tracking/event_journal.db

# Timezone for scheduled tasks (optional)
timezone: America/Los_Angeles      # Default: UTC
scheduler_catch_up_grace_seconds: 3600  # Recurring catch-up window; 0 disables it
```

Retired access fields in a monolithic configuration are migrated automatically when the file loads.
MindRoom validates the result, saves the original file once as `config.yaml.pre-membership-access`, and atomically writes the new schema.
For a single-file Docker bind mount, run the directed `mindroom config migrate --path <host-config.yaml>` command on the host before startup.
Access migration fails without changing files or creating a backup when any `!include` is present.
See [Authorization](https://docs.mindroom.chat/authorization/) for the current access model.

The root Space invitation roster is the union of managed-room `invite_users`, and those invitees do not automatically receive Space admin power.
The runtime preserves existing Space admins without adding human admins; managing Space children in a Matrix client requires sufficient existing Matrix power in that Space.
Demote stale Space admins manually in a Matrix client when needed.

## Event Journal

`event_journal.backend` defaults to `sqlite`, which stores the durable Matrix event journal at `<storage>/tracking/event_journal.db` (`mindroom_data/tracking/event_journal.db` with the default storage root).
SQLite has no independent journal-path setting and ignores the PostgreSQL URL fields.
The PostgreSQL backend requires the `postgres` extra, which supplies `psycopg`.
For a Python install, use `uvx --from 'mindroom[postgres]' mindroom run`, or include `--extra postgres` when syncing a source checkout.
Then select the backend explicitly and provide a connection URL:

```yaml
event_journal:
  backend: postgres
  database_url_env: MINDROOM_EVENT_CACHE_DATABASE_URL
```

`MINDROOM_EVENT_CACHE_DATABASE_URL` is the default environment variable name.
Set it in the exported process environment or the config-adjacent `.env`; an exported value takes precedence over `.env`.
Alternatively, `event_journal.database_url` supplies an inline URL, and a nonblank inline value takes precedence over environment lookup.
Custom nonblank `database_url_env` names must be `DATABASE_URL` or end in `_DATABASE_URL` so runtime secret filters recognize them.
PostgreSQL requires a nonblank URL; supplying a URL alone does not switch a SQLite journal to PostgreSQL.

Changes to the effective store apply after restarting MindRoom.
Changing URL fields while the backend remains `sqlite` does not change the opened file or require a journal restart.
See the [journal binding and migration commands](https://docs.mindroom.chat/cli/#journal) before moving, restoring, or adopting a journal.

## Automatic Restart Resumption

`defaults.auto_resume_after_restart` defaults to `true` and permits visible router resume prompts for eligible interrupted threaded conversations after startup or runtime replacement.
Set it to `false` to suppress those automatic prompts and resume the work manually.
Resumption still depends on current recovery ownership, room membership, a resolved original requester, and fresh history checks that reject superseded work.
This setting does not globally disable ordinary durable event replay or stale-response cleanup.
See [runtime recovery](https://docs.mindroom.chat/architecture/bot-runtime/) for the surrounding lifecycle.

## Matrix Sync Limits

`matrix_sync.max_response_bytes` and `matrix_sync.max_pending_bytes` accept positive integers in bytes and apply to both Classic and Sliding Sync.
The defaults are 16 MiB per HTTP response and 64 MiB of encoded pending output per sync session.
If a large initial sync exceeds the durable input bound, increase `max_response_bytes` enough to fit the response.
If preparing events exceeds the durable pending bound, increase `max_pending_bytes` enough to fit the encoded output.
These are separate budgets: encoded events can take more space than the HTTP response, so raising the response limit may also require raising the pending limit.
Each bot owns its own sync session; allow sufficient memory and disk space when increasing these limits.
Changing either setting through config reload restarts running agents to apply it.

Large initial syncs may also need more startup time.
`MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` controls the first-sync watchdog allowance, and runtime Helm chart values under `probes.startup` control the Kubernetes startup probe allowance.
Size those time allowances independently of the byte limits.

## Credential Seeds

MindRoom can bootstrap additional shared credential services at startup from explicit seed declarations.
Use this for deployment-managed credentials that should live in `CredentialsManager` without requiring inline one-off migration scripts.
Seeded credentials are marked `_source=env`: MindRoom updates them on later startups, but it never overwrites dashboard-managed credentials (`_source=ui`) or legacy credentials with no source marker.
OAuth token services whose names end with `_oauth` cannot use credential seeds; connect them through the OAuth lifecycle instead.
OAuth client configuration services whose names end with `_oauth_client` remain seedable.

Set `MINDROOM_CREDENTIAL_SEEDS_FILE` to a JSON file path, or `MINDROOM_CREDENTIAL_SEEDS_JSON` to equivalent inline JSON.
Relative file paths resolve from the config directory.
Credential fields can read from env vars, from files, or from literal values:

```json
[
  {
    "service": "example_oauth_client",
    "credentials": {
      "client_id": {"env": "EXAMPLE_CLIENT_ID"},
      "client_secret": {"env": "EXAMPLE_CLIENT_SECRET"}
    }
  }
]
```

Env refs use the existing secret convention: if `EXAMPLE_CLIENT_SECRET` is unset, MindRoom also checks `EXAMPLE_CLIENT_SECRET_FILE` and reads that file.
If any declared field is missing or empty, MindRoom skips that seed instead of creating a partial credential document.

## Credential Storage Encryption

Set `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` to encrypt stored credential files at rest.
The key must be a base64-encoded 32-byte value and must stay stable across restarts.
Generate one with:

```bash
python - <<'PY'
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
PY
```

When this variable is configured, `CredentialsManager` writes encrypted credential files with mode `0600` and creates credential directories with mode `0700`.
Encrypted mode refuses plaintext credential JSON files for every credential service.
Existing non-OAuth plaintext credentials become unreadable and cannot be overwritten while encryption is enabled, so back them up and recreate them under encryption or remove the key before reading them again.
OAuth credentials are read only from their authoritative SQLite stores and use the active encryption setting when published.
Legacy OAuth JSON files and sidecars are ignored and left unchanged, and changing the encryption setting never imports them.
An OAuth connection that exists only in JSON must be reconnected with the intended encryption setting.
Current encrypted SQLite credentials become readable again when their correct key is restored.

## Debug Logging

Every completed model request emits a privacy-safe structured `LLM usage` log event with provider and model identifiers.
When provider usage data is available, the event also includes provider-normalized context input tokens, uncached input tokens, and a cache-read ratio without prompt content.
When provider usage data is unavailable, the event sets `usage_available` to false and omits token and prompt-cache counters.
`debug.log_llm_requests` additionally enables best-effort provider-request logging for troubleshooting without changing model-call success or failure.
When enabled, MindRoom writes JSONL request records under `debug.llm_request_log_dir` or `mindroom_data/logs/llm_requests` by default.
Those records include prompts, messages, the final provider-prepared tool array after MindRoom wire transformations, model parameters, correlation IDs, requester metadata, and source Matrix event metadata.
The same flag also records successful tool-call rows in `mindroom_data/tracking/tool_calls.jsonl` so tool activity can be correlated with LLM request logs.
Tool failures are always recorded in `tool_calls.jsonl`, even when request logging is disabled.
Tool-call rows include a `timing` object with result-ready, before-hook, and tool-body durations when those phases are measured.
Export `MINDROOM_TIMING=1` before native process startup to emit additional structured debug timing events for stream-visible tool-call start, stream-visible tool-call completion, and full bridge completion.
The config-adjacent `.env` does not enable this flag, and timing decorators read it when modules load.
For example:

```bash
LOG_LEVEL=DEBUG MINDROOM_LOG_FORMAT=json MINDROOM_TIMING=1 mindroom run
```

The same flag emits one `Dispatch pipeline timing` summary per turn at INFO level, including `time_to_model_request_ms` from message handling through local preparation to the first Agno run invocation (not the provider SDK's HTTP-send boundary), plus separate context, queue, payload, agent-build, and model timing spans when available.
Audit logging remains enabled.
Credential-bearing fields such as tokens, cookies, passwords, API keys, and authorization headers are redacted before log records are emitted.
These artifacts can still contain sensitive non-credential prompt, argument, and result data.
Leave the flag disabled unless you are actively debugging.

## Built-In Prompt Overrides

MindRoom keeps built-in prompt defaults as uppercase globals in `src/mindroom/prompts.py`.
Use the optional root `prompts` block to override those defaults without editing Python code.
Keys must match the uppercase global names exactly, and unknown keys fail config validation.

```yaml
prompts:
  HIDDEN_TOOL_CALLS_PROMPT: |
    Tool calls are hidden from users.
    Answer directly without narrating internal tool use.
  ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE: |
    Decide which agent or team should respond to this message.

    Available agents and teams:

    {agents_info}

    User message: {message}

    Return only the agent or team name.
```

Discover allowed names by inspecting `src/mindroom/prompts.py` or by importing `PROMPT_DEFAULT_NAMES` from `mindroom.prompts`.
For prompts with `{...}` placeholders, discover the allowed fields from `PROMPT_TEMPLATE_FIELDS` in `mindroom.prompts`.
MindRoom validates configured prompt placeholders at config load, and unsupported placeholders fail validation before runtime.
Prompt placeholders are not Jinja and not Python `str.format`; MindRoom only replaces exact `{field_name}` placeholders.
Only bare placeholder names are supported; compound access, conversions, and format specs such as `{message.text}`, `{message!r}`, or `{message:.2f}` are rejected.
Escape literal braces as `{{` and `}}` inside prompt overrides that use placeholders.
Changing construction-time root prompt overrides in a running process restarts existing agents, teams, and the router through hot reload.
Prompt overrides that are read per request are picked up without restarting bots.

## Managed Avatars

MindRoom can generate managed avatars for agents, teams, rooms, and the optional root Matrix Space.
Use the root `prompts` block to override the built-in avatar prompt styles without editing Python code.
Every omitted prompt falls back to MindRoom's built-in default.

```yaml
prompts:
  AVATAR_CHARACTER_STYLE: "professional AI avatar portrait, abstract geometric silhouette"
  AVATAR_ROOM_STYLE: "minimalist wayfinding icon, precise geometry, strong silhouette"
  AVATAR_AGENT_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI agent avatar."
  AVATAR_TEAM_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI team avatar."
  AVATAR_ROOM_SYSTEM_PROMPT: "You are creating a refined, minimalist icon design for a room avatar."
```

`mindroom avatars generate` only creates missing local avatar files by default.
Run `mindroom avatars generate --force` to overwrite existing managed workspace avatar files after changing prompts or styles.
Generation uses `gpt-6-astra` for prompt creation and `gpt-image-2.5-sunburst` for 1024x1024 high-quality PNG rendering.
Both stages require only `OPENAI_API_KEY` or the file-based `OPENAI_API_KEY_FILE` credential.
`mindroom avatars sync` only fills missing Matrix avatars by default.
Run `mindroom avatars sync --force` to replace existing Matrix room or root-space avatars.

## Personal Agent Rooms

The optional `personal_rooms` section creates one private, unlisted room for each eligible human who joins an onboarding room.
The router observes those rooms; the selected agent does not need to join them.

```yaml
rooms:
  lobby: {}
agents:
  helper:
    display_name: Helper
    rooms: []
    access:
      members_of_rooms: [lobby]
personal_rooms:
  agent: helper
  onboarding_rooms: [lobby]
  commands: ["!personal"]
  alias_prefix: personal
  name: "Personal room for {user}"
  topic: "Private conversation with {agent}."
  welcome: "Welcome {user}! This is your personal room with {agent}."
  welcome_dispatch: false
  confirmation: ""
  backfill: false
  auto_join_requester: false
  requester_admin: false
  # avatar: avatars/personal.png
  avatar_from_requester: false
```

The top-level `personal_rooms` type is an object or `null`, with default `null`.
When enabled, its fields are:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `agent` | string | Required | Configured agent that owns and answers in personal rooms. |
| `onboarding_rooms` | list of strings | Required | Nonempty list of configured onboarding rooms observed by the router. |
| `commands` | list of strings | `[]` | Exact self-onboarding commands beginning with `!`, without whitespace or arguments. |
| `alias_prefix` | string | `"personal"` | Lowercase alias prefix, matching `[a-z0-9_-]{1,40}`. |
| `name` | string | `"Personal room for {user}"` | New room name template, up to 1,000 characters. |
| `topic` | string | `"Private conversation with {agent}."` | New room topic template, up to 1,000 characters. |
| `welcome` | string | `"Welcome {user}! This is your personal room with {agent}."` | Welcome template, up to 10,000 characters; empty disables new welcome intent. |
| `welcome_dispatch` | boolean | `false` | Dispatch the welcome to the selected agent after the human joins; otherwise send a notice. |
| `confirmation` | string | `""` | Optional once-only notice template in the original onboarding room, up to 10,000 characters. |
| `backfill` | boolean | `false` | Also reconcile current eligible onboarding-room members on startup and config reload. |
| `auto_join_requester` | boolean | `false` | Use the homeserver admin API to join the requester during initial creation of a new personal room. |
| `requester_admin` | boolean | `false` | Grant the human Matrix room administration. |
| `avatar` | string or `null` | `null` | Room avatar file, relative to the configuration; fills only an empty avatar. |
| `avatar_from_requester` | boolean | `false` | Copy the human's profile avatar into an empty room avatar when no avatar file is configured. |

Omit the section to disable onboarding.
Trigger rooms must already be configured.
Commands are optional, exact messages in those rooms, and onboard only their authenticated human sender.
Existing agent access rules still apply; agent and service accounts are excluded.
`backfill: true` also reconciles current eligible members at startup and configuration reload.
`auto_join_requester: true` requires a source Matrix account authorized to use the homeserver's Synapse-compatible admin join API.
It enrolls only the requester of a newly created room.
A failed initial join can retry after restart, but a requester who has already joined, left, or been banned is never force joined again.
Existing and imported rooms do not gain automatic joining when this setting is enabled later.
Restart reconciliation and backfill preserve a requester who left or was banned from a personal room.
A fresh leave-to-join event in the onboarding room may re-invite a departed requester to a newly created personal room; it never overrides a ban or re-invites someone to an imported room.
If room creation succeeds but its local room-ID receipt is lost, alias recovery conservatively skips automatic joining because it cannot prove the room was newly created by that attempt.

Templates support `{user}` (full Matrix user ID), `{room}` (room alias), and `{agent}` (display name).
Aliases combine the prefix, the first 20 lowercase SHA256 hex characters of the full user ID, and the installation namespace.
Room ownership and membership are verified before reusing an alias.
The owner may invite other MindRoom agents into their personal room; an agent sees only messages sent after its invite.
Anyone else invited into a personal room MindRoom created is removed, with one notice explaining why; imported rooms keep their attested roster and fail closed instead.
Reconciliation then logs the warning `Personal-room imported roster has unattested members` naming the unexpected members, and retries that room after doubling delays up to hourly; a configuration reload retries it at once.
Personal rooms are retained across restarts and ordinary room cleanup, including after this feature is disabled.
Disabling onboarding does not delete rooms or revoke their existing access.

The default welcome is a notice.
Set `welcome_dispatch: true` to initiate agent onboarding through the existing trusted hook-dispatch path after the human joins; requester identity and normal authorization remain intact.
Dispatched welcomes explicitly mention the selected agent through Matrix mention metadata, so default and custom templates can mention the human without suppressing agent onboarding.
Welcome delivery is durable and idempotent.
A pending welcome bound to a replaced Matrix device fails closed instead of risking a duplicate.
`requester_admin` explicitly grants the human Matrix room administration; otherwise the agent owns room state.
An optional avatar file uses the existing Matrix upload service; `avatar_from_requester: true` instead copies the human's profile avatar.
Both policies fill only an empty room avatar, with an explicit file taking priority.
An optional `confirmation` template sends one durable notice in the original onboarding room after creation and invitation; its contents, including any room alias, are visible to that room.
Pending confirmation and welcome text remain frozen across configuration changes.
Welcome text and dispatch mode are saved before waiting for the human to join; changing the welcome template, including setting it to empty, does not replace pending intent.
Disabling `personal_rooms` or revoking the human's access prevents pending welcome dispatch.
No tools or workspace reset behavior are added.

### Operator-seeded Existing Rooms

Operators can preserve an existing room ID and history by writing a trusted `PersonalRoomRecord` before enabling onboarding.
Stop the runtime before importing records.
Use `personal_room_record_path(runtime_paths, agent_name, user_id)` and `write_personal_room(path, record)` from `mindroom.matrix.personal_room_store` to atomically persist validated current-format records.
The storage location is `agents/<agent>/personal_rooms/<full-sha256-user-id>.json` under the runtime storage root.
These files are trusted operator state, never user-submitted input.

For a private room with shared history and one already permitted guest, a seed looks like this:

```json
{
  "user_id": "@alice:example.test",
  "alias": "#personal-alice:example.test",
  "source_room_id": "!lobby:example.test",
  "room_id": "!existing:example.test",
  "welcome_completed": true,
  "adoption": {
    "creator_user_id": "@router:example.test",
    "agent_user_id": "@helper:example.test",
    "router_user_id": "@router:example.test",
    "expected_history_visibility": "shared",
    "additional_user_ids": ["@guest:example.test"]
  }
}
```

The filename must bind the exact full requester ID, and adoption requires an exact room ID; an alias alone never authorizes adoption.
`creator_user_id` must match the immutable create-event sender, and `agent_user_id` must match the selected agent's authenticated Matrix identity.
An optional `router_user_id` permits only this installation's persisted router account to remain in the room.
Before import, arrange an agent-authored `org.mindroom.personal_room` state event with empty state key and exactly `{"user_id": "<requester>", "agent_user_id": "<agent>"}` as content.
The target agent must already be joined with room admin power, the directory visibility must be private, and the join rule must be invite-only.
`expected_history_visibility` must exactly match the room's current `invited`, `joined`, or `shared` history policy; `world_readable` is never accepted.
If omitted, it defaults to `invited`.
`additional_user_ids` lists concrete Matrix IDs for existing participants who may be joined, invited, or knocking; it defaults to an empty list.
No other joined, invited, or knocking member is permitted beyond the requester, agent, explicitly seeded router, and these additional users.
The list is a permission attestation only: it does not invite or rejoin anyone, grant room admin power, or retain a router during cleanup.
Only `router_user_id` grants the separate router retention contract.
Normal authorization and current onboarding-room membership still apply when reconciling the seed.
The service verifies these conditions before inviting or sending; changed history or membership fails reconciliation.
It does not rewrite existing room identity, name, topic, or history.
Explicitly seeded router membership is retained during ordinary cleanup, including after onboarding is disabled.

Set `welcome_completed: true` only when onboarding history is already complete; no historical event ID needs to be invented.
Pending welcome content and device receipts should be omitted from completed imports.
No historical-format converter, import CLI, forced self-rejoin, or workspace reset is provided.
Operators must prepare Matrix ownership state and any historical-format conversion before writing this current-format seed.

## Internal User Username

- Configure `mindroom_user.username` with the Matrix localpart to request before first startup.
- After the account is created, `mindroom_user.username` is locked as the account-creation request and cannot be changed in-place.
- Hosted provisioning can return a different actual Matrix ID; MindRoom persists that actual ID and uses it for runtime authorization.
- You can safely change `mindroom_user.display_name` at any time.

## Sections

- [Agents](https://docs.mindroom.chat/configuration/agents/) - Configure individual AI agents
- [Models](https://docs.mindroom.chat/configuration/models/) - Configure AI model providers
- [Teams](https://docs.mindroom.chat/configuration/teams/) - Configure multi-agent collaboration
- [Router](https://docs.mindroom.chat/configuration/router/) - Configure message routing
- [Dynamic Tools](https://docs.mindroom.chat/tools/dynamic-tools/) - Configure optional tools that agents load on demand
- [Memory](https://docs.mindroom.chat/memory/) - Configure memory providers and behavior
- [Knowledge Bases](https://docs.mindroom.chat/knowledge/) - Configure file-backed knowledge bases
- [Voice](https://docs.mindroom.chat/voice/) - Configure speech-to-text voice processing
- [Voice Calls](https://docs.mindroom.chat/voice-calls/) - Configure agents joining Element Call voice calls
- [Authorization](https://docs.mindroom.chat/authorization/) - Configure user and room access control
- [Matrix Space](https://docs.mindroom.chat/matrix-space/) - Configure the root Matrix Space for managed rooms
- [Skills](https://docs.mindroom.chat/skills/) - Skill format, gating, and allowlists
- [Plugins](https://docs.mindroom.chat/plugins/) - Plugin manifest and tool/skill loading

## Notes

- All top-level sections are optional with sensible defaults, but at least one agent is recommended for Matrix interactions
- Keep `models.default` configured for built-in Matrix room topic generation, even when agents, teams, and the router select other models.
- Automatic thread summaries also fall back to `models.default` unless `defaults.thread_summary_model` or an applicable room or entity entry in `room_thread_summary_models` selects another model.
- Agents can set `knowledge_bases`, but each entry must exist in the top-level `knowledge_bases` section
- Router, agent, and team `accept_invites` policies default to `true`; use `false` or `[]` to reject every invite, or a list of exact and wildcard Matrix user IDs matched after human-only alias resolution; non-human accounts retain their exact transport ID
- Invitation acceptance is independent from conversation access, and accepted ad-hoc room IDs are persisted across restarts without adding them to the static `rooms` list
- Approval-gated tools require the router to be joined to the Matrix room before the call executes.
- Every concrete Matrix agent operating in a room has a zero-argument `invite_router` recovery tool that invites the router into its current room when `router.accept_invites` allows the current Matrix transport account's user ID.
- During team execution, the transport identity is the team's Matrix account rather than the member agent's account.
- The recovery tool waits briefly for joined membership and reports a pending state when the router has not joined yet.
- When the router is absent, `invite_router` is the recovery path; after the router auto-accepts, the agent can retry the approval-gated call.
- `agents.<name>.context_files` load files from the agent's workspace into each agent instance, so edits take effect on the next reply without restarting (see [Agents](https://docs.mindroom.chat/configuration/agents/))
- `agents.<name>.room_thread_modes` overrides `thread_mode` for specific rooms, and resolution is room-aware for agents, teams, and router decisions (see [Agents](https://docs.mindroom.chat/configuration/agents/))
- `memory.backend` sets the global memory default, and `agents.<name>.memory_backend` overrides it per agent
- `memory.search` controls file-backed `search_memories`, and `agents.<name>.memory_search` overrides it per agent
- `memory.backend: none`, `memory: none`, or `agents.<name>.memory_backend: none` disables built-in durable memory for the effective agent without disabling Agno Learning
- `defaults.max_preload_chars` caps preloaded file context (`context_files`)
- `administrators`, room invitations, responder access, Matrix power, and `credential_managers` are independent capabilities
- `authorization.config_command_enabled` defaults to `false`; when set to `true`, `!config` requires a platform administrator
- Responder `access` can match static users, current-room members, or members of configured managed rooms
- Responder access and room membership do not grant general dashboard or shared-credential management; eligible requesters may manage [their own OAuth connections](https://docs.mindroom.chat/oauth-framework/)
- `authorization.aliases` maps bridge bot user IDs to canonical users so bridged messages inherit the same permissions (see [Authorization](https://docs.mindroom.chat/authorization/))
- `room_defaults` and `rooms.<key>` own join policy, directory visibility, invitations, encryption, and Matrix admins
- Monolithic configurations with retired access fields migrate automatically; configurations using `!include` must be migrated manually
- Room reconciliation also updates managed room power levels so `com.mindroom.thread.tags` can be written at PL0
- Publishing to the room directory requires the managing service account (typically router) to have moderator/admin power in each room
- Thread-tag power-level reconciliation also requires the managing service account to be joined and able to update `m.room.power_levels`
- The `memory` system works out of the box with OpenAI; use `memory.llm` for memory summarization with a different provider
