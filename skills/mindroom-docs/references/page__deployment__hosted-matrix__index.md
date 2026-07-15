# Hosted Matrix + Local Backend

This guide covers the simplest production-like setup:

- Matrix homeserver is hosted at `https://mindroom.chat`
- Web chat runs at `https://chat.mindroom.chat`
- You run only `mindroom run` locally via `uvx`

Watch the 2-minute setup video:

[![MindRoom: installing and talking to my first AI agent in 2 minutes](https://img.youtube.com/vi/jR3xLUxyWhg/maxresdefault.jpg)](https://youtu.be/jR3xLUxyWhg)

## What Runs Where

| Component | Runs on | Purpose |
|----------|---------|---------|
| `chat.mindroom.chat` | Hosted web app | Login UI and pairing UI |
| `mindroom.chat` | Hosted Matrix + provisioning API | Matrix transport + local onboarding API |
| `uvx mindroom run` | Your machine/server | Agent orchestration, tools, model calls |

## Prerequisites

- Python 3.12+
- `uv` installed
- A Matrix account that can sign in to `chat.mindroom.chat`
- At least one AI provider API key, or a local Codex CLI ChatGPT login

## 1. Initialize Local Config

```bash
uvx mindroom config init
```

This creates `~/.mindroom/config.yaml` and `~/.mindroom/.env` with hosted defaults.
Use `uvx mindroom config init --provider codex` if you want the starter config to use `provider: codex`.

## 2. Add AI Provider Key

Edit `~/.mindroom/.env` and set at least one provider key:

```bash
OPENAI_API_KEY=...
# or OPENROUTER_API_KEY=...
```

For Codex CLI ChatGPT authentication, run `codex login` instead of adding an API key.
MindRoom reads `~/.codex/auth.json` by default.

## 3. Pair This Install

1. Open `https://chat.mindroom.chat`.
2. Go to `Settings -> Local MindRoom`.
3. Click `Generate Pair Code`.
4. Run locally:

```bash
uvx mindroom connect --pair-code ABCD-EFGH
```

Pair code behavior:

- Valid for 600 seconds (10 minutes).
- Only used to bootstrap local pairing.

After successful pairing, local provisioning credentials are written to `~/.mindroom/.env` by default unless you use `--no-persist-env`.

## 4. Start MindRoom

```bash
uvx mindroom run
```

MindRoom then:

1. Connects to `MATRIX_HOMESERVER`
2. Creates/updates configured agent Matrix users
3. Joins/creates configured rooms
4. Starts processing messages

## Optional: Docker worker isolation

If you want worker-routed tools to run in dedicated Docker workers instead of the main `uvx mindroom run` process, follow [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/).
That especially includes `coding`, `docker`, `file`, `python`, and `shell`, plus other worker-safe tools that only need worker state or config-referenced filesystem assets.
Dedicated Docker workers do not get a bind mount of `~/.mindroom` or the raw config-adjacent `.env` file.
They still receive a filtered public startup-runtime env payload derived from exported env vars and allowed `.env` values.
Proxied `shell` and `python` requests still receive their execution env from the active runtime contract, so ordinary `.env` values can remain visible to those tools even though the raw file is not mounted.
Use credential leases or `MINDROOM_DOCKER_WORKER_ENV_JSON` for worker-specific secrets.
Use `worker_scope: shared` when you want one persistent container per agent.
Use `worker_scope: user_agent` when each requester should get separate per-agent containers.

## Credential Model (Important)

`mindroom connect` returns local provisioning credentials:

- `MINDROOM_LOCAL_CLIENT_ID`
- `MINDROOM_LOCAL_CLIENT_SECRET`
- `MINDROOM_NAMESPACE`

`MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET` are **not Matrix user access tokens**.
`MINDROOM_NAMESPACE` is appended to managed agent usernames and room aliases to avoid collisions on shared homeservers.

They can only call provisioning-service endpoints that accept local client credentials, including agent registration and retrieval of the Google desktop app client configuration.
The Google app client configuration lets the local process exchange OAuth codes directly with Google; the provisioning service does not receive the resulting Google authorization code or tokens.
Treat the local provisioning credentials as secrets because anyone who obtains them can use the same provisioning capabilities, including retrieving the Google desktop app client configuration.
Revoke them from `Settings -> Local MindRoom` in the chat UI.
The distributed Google desktop client secret is not confidential in the installed-app model because every paired install can retrieve it.
Provisioning keeps that client out of published artifacts, gates casual retrieval, and enables centralized rotation.
Rotate the Google OAuth client in response to observed client abuse or as an operational rotation, not merely because one pairing credential leaked.

## Trust Model (Hosted Server vs Message Privacy)

For message *content*, this setup can be effectively zero-trust toward the homeserver operator when rooms are end-to-end encrypted.

- In E2EE rooms, the homeserver stores ciphertext and cannot read message bodies.
- The local `mindroom run` process holds your agent account keys and performs decryption locally.

Important limits:

- This does **not** hide metadata (room membership, timestamps, event IDs, sender IDs, traffic patterns).
- If a room is not encrypted, the homeserver can read plaintext.
- Any model/tool providers you send content to can still see the prompts/data you send to them.

So the precise claim is: encrypted Matrix message content is protected from the hosted homeserver, not that every part of the system is universally invisible.

## If You Self-Host Later

You can keep the same local flow and switch endpoints:

- `MATRIX_HOMESERVER=https://your-matrix.example.com`
- `MINDROOM_PROVISIONING_URL=https://your-matrix.example.com` (or your dedicated provisioning host)

Then run `mindroom connect` again with a fresh pair code from your own UI.
