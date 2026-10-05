---
icon: fontawesome/brands/openai
---

# OpenAI-Compatible API

MindRoom serves an OpenAI-compatible chat completions API at `/v1`, so any chat frontend that speaks the OpenAI protocol (LibreChat, Open WebUI, LobeChat, ChatBox, BoltAI, and others) can use MindRoom agents and teams as selectable models.
Each request runs the selected agent with its configured tools, instructions, memory, and knowledge bases, without Matrix.

## Setup

### 1. Choose authentication

Add one of these to `.env`:

```bash
# Require API keys (recommended)
OPENAI_COMPAT_API_KEYS=sk-my-secret-key-1,sk-my-secret-key-2

# Or allow unauthenticated access (local development only)
OPENAI_COMPAT_ALLOW_UNAUTHENTICATED=true
```

With neither set, every `/v1` request returns 401.
Unauthenticated access answers only requests addressed to allowed host names and refuses cross-site browser requests; see [Unauthenticated dashboard host names](authorization.md#unauthenticated-dashboard-host-names).
A client that reaches an unauthenticated `/v1` by another host name, such as LibreChat in Docker using `http://host.docker.internal:8765/v1`, needs that name in `MINDROOM_DASHBOARD_ALLOWED_HOSTS`; with `OPENAI_COMPAT_API_KEYS` set, any host name works.

### 2. Start MindRoom

```bash
uv run mindroom run
```

The API is served by the bundled dashboard/API server at `http://localhost:8765/v1/`; change the port with `--api-port`, and note that `--no-api` turns the API off.

> [!IMPORTANT]
> If the dashboard and `/v1/*` share a domain behind a reverse proxy, route `/v1/*` to the MindRoom runtime as well as `/api/*`.
> Otherwise OpenAI-compatible requests reach the dashboard and fail.

### 3. Verify

```bash
# List available models
curl -H "Authorization: Bearer sk-my-secret-key-1" \
  http://localhost:8765/v1/models

# Chat (non-streaming)
curl -H "Authorization: Bearer sk-my-secret-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"general","messages":[{"role":"user","content":"Hello"}]}' \
  http://localhost:8765/v1/chat/completions

# Chat (streaming)
curl -N -H "Authorization: Bearer sk-my-secret-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"general","messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  http://localhost:8765/v1/chat/completions
```

## Client Configuration

Any OpenAI-compatible client works: set the base URL to `http://localhost:8765/v1` and the API key to one of `OPENAI_COMPAT_API_KEYS`.
MindRoom implements `GET /v1/models` and `POST /v1/chat/completions`.

### LibreChat

Add to `librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "MindRoom"
      apiKey: "${MINDROOM_API_KEY}"
      baseURL: "http://localhost:8765/v1"
      models:
        default: ["general"]
        fetch: true
      modelDisplayLabel: "MindRoom"
      titleConvo: true
      titleModel: "general"
      dropParams: ["stop", "frequency_penalty", "presence_penalty", "top_p"]
      headers:
        X-Session-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
```

The `X-Session-Id` header keeps each LibreChat conversation in one MindRoom session; see [Session continuity](#session-continuity).

### Open WebUI

1. Go to **Admin Settings > Connections > OpenAI > Manage**.
2. Set API URL to `http://localhost:8765/v1`.
3. Set API Key to one of your `OPENAI_COMPAT_API_KEYS`.
4. Agents appear in the model picker.

## Model selection

Each available agent appears in `/v1/models` with its config name as the model ID (for example `code`), its `display_name` as the name, and its `role` as the description.
Only shared agents that are unscoped or use `worker_scope: shared` are available.
Agents with `private`, `worker_scope: user`, or `worker_scope: user_agent` are not listed, and neither is any agent whose `delegate_to` targets, directly or transitively, include such an agent.
Requesting one of them returns HTTP 400 with code `unsupported_worker_scope`, and the message names the agents that block the request.

## Auto-routing

Select the `auto` model to let the router pick the best available agent for each message.
`auto` considers agents only; select `team/<team_name>` to run a team.
The response's `model` field names the chosen agent, and the session belongs to that agent.
When a [router judgment](configuration/router.md#responder-selection-judgments) finds no suitable agent, the request fails with HTTP 400 and code `no_suitable_responder`; choose a model explicitly or rephrase.
When routing itself fails, the request goes to the first available agent.
`auto` is listed only when at least one agent is available.

## Teams

Teams appear as `team/<team_name>` models, and selecting one runs the team in its configured `coordinate` or `collaborate` mode.
A team is not listed when any member agent, or any agent a member delegates to, uses a requester-private scope (see [Model selection](#model-selection)).
For mapped callers, the team's own `access` decides whether they can use it, and that grant covers its member agents for the request.

## Session continuity

MindRoom picks the session for each request from its headers:

1. `X-Session-Id`, when present.
2. `X-LibreChat-Conversation-Id`, combined with the selected model.
3. Otherwise a new session for every request.

Requests with the same session keep the agent's conversation history and long-lived tool sessions, such as a [`claude_agent`](tools/agent-orchestration.md#claude_agent) coding session, so set `X-Session-Id` for continuity.
Sessions are separate per API key and per mapped requester, so two keys sending the same `X-Session-Id` never share a session.
With unauthenticated access, all callers share one set of sessions.

## Messages and parameters

The last `user` message is the prompt, and earlier `user` and `assistant` messages are passed as conversation history; `tool` messages are ignored.
Client `system` and `developer` messages are prepended to the prompt and add to the agent's own instructions rather than replacing them.
When `content` is an array of parts, only the `text` parts are used; images and other parts are ignored.

The API accepts but ignores these OpenAI parameters, because the agent's configuration controls them:

- `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`
- `tools`, `tool_choice` (agents use their configured tools)
- `n`, `stop`, `frequency_penalty`, `presence_penalty`, `seed`
- `response_format`, `logprobs`, `logit_bias`
- `stream_options` (streaming responses never include a usage chunk)
- `user` (it never selects another user's identity, memory, or usage attribution; see [Requester identities and delegation](#requester-identities-and-delegation))

## Streaming and tool output

`stream: true` returns standard OpenAI Server-Sent Events: a role chunk, content chunks, a finish chunk, and `[DONE]`.
Tool calls appear inline in the content as `<tool id="N" state="start|done">...</tool>` text, not as native OpenAI `tool_calls`.
Tool-call text is always included on `/v1`, even when the agent sets `show_tool_calls: false`.

## Authentication

| `OPENAI_COMPAT_API_KEYS` | `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | Behavior |
|---|---|---|
| Set | (any) | `Authorization: Bearer <key>` required, matching one of the comma-separated keys |
| Unset | `true` | No authentication required |
| Unset | Unset/`false` | All requests return 401 |

`/v1` authentication is independent of the dashboard: `MINDROOM_API_KEY` protects `/api/*` and the dashboard, while `OPENAI_COMPAT_API_KEYS` protects `/v1/*`.

### Requester identities and delegation

Bind API keys to Matrix user IDs with a JSON object in `.env`:

```dotenv
OPENAI_COMPAT_API_KEYS=sk-astrbot,sk-legacy
OPENAI_COMPAT_API_KEY_REQUESTERS='{"sk-astrbot":"@alice:example.org"}'
```

Restart MindRoom after changing these settings.
The key must also appear in `OPENAI_COMPAT_API_KEYS`; a mapping alone never authenticates a caller.
Mapped identities must be concrete human Matrix user IDs, and [bridge aliases](authorization.md#bridge-aliases) resolve to their canonical requester.
The request's `user` field and headers cannot override the mapped identity.

A mapped caller sees and can use only the agents and teams whose [responder access](authorization.md#responder-access) admits that user, and `auto` routes only among those agents.
Grant access with `access.users`, administrator membership, or `members_of_rooms`; `current_room_members` never applies because `/v1` requests have no room.
Mapped callers can use `run_subagent`, which requires both the calling agent's `delegate_to` and the requester's access to the target.

Unmapped keys and unauthenticated callers can use every available model but cannot delegate to subagents.

## Limitations

- **Fewer tools than in Matrix**: `/v1` hides tool functions matched by a required [tool approval](tool-approval.md#tools-that-cannot-pause-for-approval) rule, including script rules, because approval cards need a Matrix room.
  It also hides tools that need a live Matrix room, such as `scheduler`, `matrix_message`, `attachments`, and `todo`.
- **No requester-private agents**: see [Model selection](#model-selection).
- **Partial token usage**: only non-streaming responses report `usage`.
  It counts the turn's final model run, summed over the leader and members for teams, with cached input included in `prompt_tokens`, so tokens spent on earlier internal attempts in the same turn are not counted.
- **Request size**: request bodies over 16 MiB are rejected with HTTP 413.

## Errors

Errors use the OpenAI error format with an HTTP status, message, and `code`.

| Status | Message or code | Fix |
|---|---|---|
| 401 | `OpenAI-compatible API keys are not configured` | Set `OPENAI_COMPAT_API_KEYS` or `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED=true` |
| 401 | `Missing or invalid Authorization header`, `Invalid API key` | Send `Authorization: Bearer <key>` with a configured key |
| 400 | `Host '<host>' is not allowed without a credential; ...` | Add the host to `MINDROOM_DASHBOARD_ALLOWED_HOSTS` or configure API keys |
| 403 | `This requester is not authorized for the model`, `No agents are authorized for this requester` | Grant the mapped user [responder access](authorization.md#responder-access) |
| 403 | `API requester must be a human identity` | Map the key to a human user, not an agent, bot account, or MindRoom's internal user |
| 404 | `model_not_found` | Use an ID from `/v1/models` |
| 400 | `unsupported_worker_scope` | See [Model selection](#model-selection) |
| 400 | `no_suitable_responder` | See [Auto-routing](#auto-routing) |
| 503 | `Invalid API requester configuration` | Make `OPENAI_COMPAT_API_KEY_REQUESTERS` a JSON object mapping keys to full Matrix user IDs |
