# Hosted Matrix + Local Backend

This guide covers the simplest production-like setup:

- Matrix homeserver is hosted at `https://mindroom.chat`
- Web chat runs at `https://chat.mindroom.chat`
- You run only `mindroom run` locally via `uvx`

## What Runs Where

| Component            | Runs on                          | Purpose                                 |
| -------------------- | -------------------------------- | --------------------------------------- |
| `chat.mindroom.chat` | Hosted web app                   | Login UI and pairing UI                 |
| `mindroom.chat`      | Hosted Matrix + provisioning API | Matrix transport + local onboarding API |
| `uvx mindroom run`   | Your machine/server              | Agent orchestration, tools, model calls |

## Prerequisites

- Python 3.12+
- `uv` installed
- A Matrix account that can sign in to `chat.mindroom.chat`
- At least one AI provider API key

## 1. Initialize Local Config

```
mkdir -p ~/mindroom-local
cd ~/mindroom-local
uvx mindroom config init --profile public
```

This creates `config.yaml` and `.env` with hosted defaults.

## 2. Add AI Provider Key

Edit `.env` and set at least one provider key:

```
ANTHROPIC_API_KEY=...
# or OPENAI_API_KEY=...
```

## 3. Pair This Install

1. Open `https://chat.mindroom.chat`.
1. Go to `Settings -> Local MindRoom`.
1. Click `Generate Pair Code`.
1. Run locally:

```
uvx mindroom connect --pair-code ABCD-EFGH
```

Pair code behavior:

- Valid for 600 seconds (10 minutes).
- Only used to bootstrap local pairing.

After successful pairing, local provisioning credentials are written to `.env` unless you use `--no-persist-env`.

## 4. Start MindRoom

```
uvx mindroom run
```

MindRoom then:

1. Connects to `MATRIX_HOMESERVER`
1. Creates/updates configured agent Matrix users
1. Joins/creates configured rooms
1. Starts processing messages

## Credential Model (Important)

`mindroom connect` returns local provisioning credentials:

- `MINDROOM_LOCAL_CLIENT_ID`
- `MINDROOM_LOCAL_CLIENT_SECRET`

These are **not Matrix user access tokens**.

They can only call provisioning-service endpoints that accept local client credentials (for example agent registration flows). Revoke them from `Settings -> Local MindRoom` in the chat UI.

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
