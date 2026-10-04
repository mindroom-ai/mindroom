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

Shortcut: in a terminal, `uvx mindroom run` with no config asks for a provider and API key, creates the files below, pairs, offers to install a login service, and otherwise starts in one command; the steps below are the explicit path.
Without a terminal, `OPENAI_API_KEY=... uvx mindroom run --provider openai --service` answers those questions with flags.

## 1. Initialize Local Config

```bash
uvx mindroom config init
```

This creates `~/.mindroom/config.yaml` and `~/.mindroom/.env` with hosted defaults.
Use `uvx mindroom config init --provider codex` if you want the starter config to use `provider: codex`.

## 2. Add AI Provider Key

Edit `~/.mindroom/.env` and set credentials matching the provider selected during `config init`.
The default provider is OpenAI:

```bash
OPENAI_API_KEY=...
```

To use OpenRouter instead, regenerate with `uvx mindroom config init --provider openrouter` and then set `OPENROUTER_API_KEY`.

For Codex CLI ChatGPT authentication, run `codex login` instead of adding an API key.
MindRoom reads `~/.codex/auth.json` by default.

## 3. Start MindRoom (pairing happens automatically)

```bash
uvx mindroom run
```

On first run, MindRoom prints a pairing link and QR code.
Open the link or scan the QR code with your MindRoom Chat account to approve the pairing.
Alternatively, enter the displayed code in MindRoom Chat → Settings → Local MindRoom.
Approve only when the code on the page matches your terminal, because a link someone else sends you belongs to their machine; the page also shows the address the request came from.

After approval, MindRoom prints the approving account, such as `Approved by @alice:mindroom.chat.`, before it saves anything.
In a terminal, it asks `Is this your account? [Y/n]`; answering `n` or pressing Ctrl+C discards the credentials and stops, and you can revoke that connection in MindRoom Chat → Settings → Local MindRoom.
Under a service or the macOS app, it prints the approving account without asking.
While it waits for approval, `/api/health` on the API port already reports healthy and `/api/ready` reports `Waiting for local pairing approval`, so container health checks do not restart it with a new code.

Pair code behavior:

- Valid for 600 seconds (10 minutes).
- MindRoom automatically generates a new code if the previous expires.
- Only used to bootstrap local pairing.

After successful pairing, local provisioning credentials are written to `~/.mindroom/.env`.

MindRoom then:

1. Connects to `MATRIX_HOMESERVER`
2. Creates/updates configured agent Matrix users
3. Joins/creates configured rooms
4. Starts processing messages

## connect

Pair this local MindRoom install with your MindRoom Chat account through a provisioning service.

Default provisioning URL is `https://mindroom.chat` unless you override it with `--provisioning-url` or `MINDROOM_PROVISIONING_URL`.

```bash
mindroom connect
```

The command prints an approval link, a pair code, and (in a terminal) a QR code of the link.
Open the link or scan the QR code while signed in to MindRoom Chat and approve the machine, or enter the code in MindRoom Chat → Settings → Local MindRoom.
Add `--open-browser` to open the approval link in your default browser.
The link must be a plain `http` or `https` URL without credentials, backslashes, or whitespace; any other link from the provisioning service fails the pairing.
Approve only when the code on the page matches your terminal (`Approve only if the page shows code ABCD-EFGH`); the page also shows the address the request came from.

`connect` makes one attempt: if nobody approves within 10 minutes, it exits with `Approval timed out. Run the command again to get a new link.`
The 10-minute limit also applies while the provisioning service is unreachable, with one extra minute of grace for an approval made just before expiry.
When the service rate-limits polling, for example because several machines share one public address, `connect` and `run` wait longer between polls, up to 30 seconds.
You usually do not need `connect` at all, because `mindroom run` pairs automatically when hosted pairing is required and prints a new link whenever the previous one expires.

After approval, and before anything is saved, MindRoom prints the approving account, for example `Approved by @alice:mindroom.chat.`
Anyone who sees the link or code can approve it, and the approving account is the one your agents will trust.
In a terminal, `connect` and `run` then ask `Is this your account? [Y/n]`.
Pressing Enter or answering `y` saves the credentials.
Answering `n`, pressing Ctrl+C, or closing input discards the credentials without writing `.env` or changing `config.yaml`, and the command exits with an error (`run` does not start).
The discarded connection is unusable, and you can revoke it in MindRoom Chat → Settings → Local MindRoom.
Without a terminal, such as under a service or the macOS app, nothing is asked, and the approving account is printed with the same revoke hint.
If the provisioning service does not name the approving account, nothing is asked either, because there is no account to recognize; the same revoke hint is printed.
Owner placeholders in `config.yaml` are replaced only once, so when a later pairing is approved by an account missing from `administrators`, MindRoom prints a note and leaves the config unchanged; add that account to `administrators`, `room_defaults.invite_users`, and `room_defaults.admins` yourself if it should manage MindRoom.

If the approval's response is lost in transit, the provisioning service has already handed out the credentials once and will not send them again.
`connect` then exits with an explanation and asks you to run it again, while `run` warns and starts a new pairing.
You can revoke the unused entry in MindRoom Chat → Settings → Local MindRoom.

Pairing is only for hosted mindroom.chat or your own provisioning service.
When no provisioning URL is configured and the effective homeserver is not mindroom.chat, `connect` refuses without contacting anything or writing files.
This includes an unset `MATRIX_HOMESERVER`, which defaults to `http://localhost:8008`, so run `mindroom config init --matrix-server mindroom.chat` first for hosted defaults.
Self-hosted servers register agents with `MATRIX_REGISTRATION_TOKEN` or `MATRIX_REGISTRATION_SHARED_SECRET` instead.
Pass `--provisioning-url` to pair with your own provisioning service anyway.

If this machine is already connected, pairing again creates a new connection and a new agent namespace: existing agents keep working, and new agents get the new namespace.
In a terminal, `connect` asks before pairing again.
Without a terminal, it does not pair and exits with code `3`, so scripts and the macOS app can tell this apart from a failure (exit code `1`).
Add `--force` to pair again without asking.

The macOS app uses `--graceful-cancel` so SIGTERM stops a waiting connection with exit code `130` without saving credentials.
An approval request already in flight, or a credential save already underway, finishes and reports its normal success or error result instead.
This preserves credentials that the provisioning service issues only once.

On success (default `--persist-env`), this writes to `.env` next to `config.yaml`:

- `MINDROOM_PROVISIONING_URL`
- `MINDROOM_LOCAL_CLIENT_ID`
- `MINDROOM_LOCAL_CLIENT_SECRET`
- `MINDROOM_NAMESPACE`

The client ID and secret must be plain tokens of letters, digits, and `._~+/-` of at most 512 characters, optionally followed by `=` padding; any other value fails the pairing before anything is saved.

If your config still contains the owner placeholder token `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__`, `connect` will auto-replace it in membership access and managed-room policy settings when pairing returns a valid `owner_user_id`.
A valid `owner_user_id` is a Matrix user ID with a current-grammar localpart (lowercase letters, digits, and `._=/+-`) and a valid server name; any other value is reported as malformed and never written to `.env` or `config.yaml`.
Such an account still counts as named: it is printed as `Approved by '<value>', which is not a valid Matrix user ID.` and, in a terminal, confirmed like any other approver.
`mindroom config init` likewise warns when `MINDROOM_OWNER_USER_ID`, from the environment or `.env`, is outside that grammar, names where it came from, and leaves the owner placeholders for you to replace.

Use `--no-persist-env` if you want to export variables only for the current shell session.

```bash
mindroom connect --no-persist-env
```

Use `--provisioning-url` for non-default deployments:

```bash
mindroom connect --provisioning-url https://matrix.example.com
```

See [Optional: Docker worker isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/#optional-docker-worker-isolation).

## Credential Model (Important)

`mindroom connect` returns local provisioning credentials:

- `MINDROOM_LOCAL_CLIENT_ID`
- `MINDROOM_LOCAL_CLIENT_SECRET`
- `MINDROOM_NAMESPACE`

`MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET` are **not Matrix user access tokens**.
`MINDROOM_NAMESPACE` is appended to managed agent usernames and room aliases to avoid collisions on shared homeservers.

They can only call provisioning-service endpoints that accept local client credentials, including agent registration and retrieval of the Google desktop app client configuration.
Agent registration never sends your agents' passwords to the provisioning service.
The service creates each new agent account with a one-time password and returns it once, and the local process immediately changes it to a locally generated password that is never sent to the provisioning service.
The Google app client configuration lets the local process exchange OAuth codes directly with Google; the provisioning service does not receive the resulting Google authorization code or tokens.
Treat the local provisioning credentials as secrets because anyone who obtains them can use the same provisioning capabilities, including retrieving the Google desktop app client configuration.
Revoke them from `Settings -> Local MindRoom` in the chat UI.
That page shows when each paired install was last seen.
Each account keeps at most 20 paired installs, revoked ones included; pairing another at the limit removes one, preferring a revoked install and otherwise the one seen least recently.
A running `mindroom run` process reports itself to the provisioning service at startup and then every six hours.
Each report is an empty request authenticated only by `MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET`, so it carries no messages, configuration, or other content.
The service records these reports with ten-minute resolution.
If the service rejects the credentials as invalid or revoked, the install logs a warning asking you to run `mindroom connect` again and stops reporting.
The distributed Google desktop client secret is not confidential in the installed-app model because every paired install can retrieve it.
Provisioning keeps that client out of published artifacts, gates casual retrieval, and enables centralized rotation.
Rotate the Google OAuth client in response to observed client abuse or as an operational rotation, not merely because one pairing credential leaked.

## Trust Model (Hosted Server vs Message Privacy)

In end-to-end encrypted rooms, the homeserver stores message bodies as ciphertext.
The local `mindroom run` process holds your agent account keys and performs decryption locally.
Content privacy depends on authentic recipient devices and trusted clients.

MindRoom currently shares outbound encryption keys with unverified devices belonging to room members; it does not require recipient-device verification before sending.
An actively malicious homeserver that advertises an attacker-controlled device for a participating account can therefore receive keys when MindRoom next shares an outbound session with that device.
This limits protection against an active homeserver operator; it does not imply access to arbitrary stored history.
See the [Matrix E2EE implementation guide](https://matrix.org/docs/matrix-concepts/end-to-end-encryption/) for device discovery, session sharing, and verification.

Important limits:

- This does **not** hide metadata (room membership, timestamps, event IDs, sender IDs, traffic patterns).
- If a room is not encrypted, the homeserver can read plaintext.
- Any model/tool providers you send content to can still see the prompts/data you send to them.

## If You Self-Host Later

You can keep the same local flow and switch endpoints:

- `MATRIX_HOMESERVER=https://your-matrix.example.com`
- `MATRIX_SERVER_NAME=your-matrix.example.com`
- `MINDROOM_PROVISIONING_URL=https://your-matrix.example.com` (or your dedicated provisioning host)

If the homeserver requires a registration token for managed agent accounts, also set `MATRIX_REGISTRATION_TOKEN`.

On the provisioning service itself, set `MINDROOM_PROVISIONING_APPROVE_URL=https://chat.your-matrix.example.com/connect` so pairing links open your own chat UI.
It defaults to `https://chat.mindroom.chat/connect` and is not read by the local `mindroom` process.

Then run `mindroom connect` or `mindroom run` to pair with your own deployment.
`mindroom connect` refuses to pair a non-mindroom.chat homeserver, including an unset `MATRIX_HOMESERVER` (which defaults to `http://localhost:8008`), unless `MINDROOM_PROVISIONING_URL` is set or `--provisioning-url` is given.
Without a provisioning service, register agents with `MATRIX_REGISTRATION_TOKEN` or `MATRIX_REGISTRATION_SHARED_SECRET` instead of pairing.
