# Getting Started

This page installs MindRoom and gets a first agent answering in chat.
Pick one setup:

| Setup | Use when |
| --- | --- |
| [Your computer + MindRoom Chat](#recommended-your-computer-mindroom-chat) (recommended) | You want the simplest start: MindRoom runs on your computer, and Matrix and the chat app are hosted at `mindroom.chat` |
| [macOS app](https://docs.mindroom.chat/installation/macos-app/) | You want a native app that runs your agents on an Apple silicon Mac, including one-click local models |
| [Full stack Docker Compose](#full-stack-docker-compose) | You want everything on your own machine: dashboard, Matrix homeserver, and chat app |
| [Agent-managed NixOS container](#advanced-agent-managed-nixos-container) | You want an agent to manage its own persistent machine while the host controls what it can see |
| [Manual install](#manual-install-with-your-own-matrix-homeserver) | You already run a Matrix homeserver |
| [Hosted MindRoom](https://mindroom.chat/#hosted) | You want nothing to install, and MindRoom runs your agents for you |




## Recommended: your computer + MindRoom Chat

MindRoom runs on your computer, while the Matrix homeserver is hosted at `mindroom.chat` and the chat app at `chat.mindroom.chat`.

Before you start:

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python when needed.
- Have a model ready: an API key for a provider such as Anthropic, OpenAI, or OpenRouter; a subscription login such as Codex; or a local model through Ollama or llama.cpp (see [Supported Providers](https://docs.mindroom.chat/configuration/models/#supported-providers)).
- Sign in at [chat.mindroom.chat](https://chat.mindroom.chat); the first sign-in creates the MindRoom Chat account that approves the pairing.

### 1. Run MindRoom

```bash
uvx mindroom run
```

When no config exists and MindRoom runs in a terminal, it asks a few setup questions:

1. Choose a model provider preset (default `openai`; the choices are listed under [`config init`](https://docs.mindroom.chat/cli/#config-init)).
2. For `anthropic`, `openai`, or `openrouter`, paste the API key; input is hidden.
   Press Enter to skip, then connect the provider later through the [dashboard](https://docs.mindroom.chat/dashboard/#connect-your-ai-provider), or add the key to `~/.mindroom/.env` and restart `mindroom run`.
   MindRoom does not ask when the key is already set in your environment.
   Other presets print their remaining step instead, such as `codex login`, `ollama pull`, or the Azure, Bedrock, or Vertex AI settings to add to `~/.mindroom/.env` before restarting.

MindRoom then writes `~/.mindroom/config.yaml` and `~/.mindroom/.env` with hosted Matrix defaults (`MATRIX_HOMESERVER=https://mindroom.chat`) and a starter `Mind` agent in a `Personal` room.

### 2. Approve pairing

MindRoom prints a pairing link and QR code.
Open the link or scan the QR code with your MindRoom Chat account to approve, or enter the displayed code in MindRoom Chat → Settings → Local MindRoom.
After approval, MindRoom prints the approving account and asks `Is this your account? [Y/n]` before saving anything.
Pair codes expire after 10 minutes, and `mindroom run` prints a new one when that happens.
See [connect](https://docs.mindroom.chat/deployment/hosted-matrix/#connect) for pairing details, the credentials it saves, and how to revoke a pairing.

### 3. Choose how MindRoom keeps running

After pairing, first-run setup asks whether to run MindRoom in the background and start it at login, as a systemd user service on Linux or a launchd agent on macOS.
Pressing Enter installs and starts the service, the same as `mindroom service install`, and returns to your shell.
Check it with `mindroom service status`, and run `mindroom service restart` after editing `~/.mindroom/.env`.
Answering `n` runs MindRoom and the dashboard in the terminal instead, which also happens when the service cannot be installed.
See [`mindroom service`](https://docs.mindroom.chat/cli/#service) for when the question is skipped.

### Verify

- **Chat:** open `https://chat.mindroom.chat` and send `@mind hello` in the `Personal` room.
- **Dashboard:** open `http://localhost:8765` to configure agents, models, and tools.
  It asks for the `MINDROOM_API_KEY` that setup generated in `~/.mindroom/.env`, because `mindroom run` serves the dashboard on every network interface by default.
- **Preflight check:** `uvx mindroom doctor` checks config, API keys, Matrix connectivity, pairing, and storage in one pass; see [doctor](https://docs.mindroom.chat/cli/#doctor).

See [Hosted Matrix + Local Backend](https://docs.mindroom.chat/deployment/hosted-matrix/) for what runs where and the credential and trust model.
To run worker-routed execution tools such as `coding`, `docker`, `file`, `python`, and `shell` in dedicated Docker workers on the same machine, see [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/).



### Set up from a script or service

Without a terminal (services, Docker, the macOS app), a missing config is an error that points to `mindroom config init`, unless you pass `--provider`.
Flags answer the setup questions up front, which lets a script or coding agent set up MindRoom:

```bash
OPENAI_API_KEY=sk-... uvx mindroom run --provider openai --service
```

`--provider` picks the preset and the API key comes from the environment.
`--service` installs the login service after pairing, and `--no-service` skips the question.
MindRoom prints the pairing link and code for a person to approve, and prints the approving account instead of asking about it.

### Create the config first with `config init`

Use `config init` to review the files before starting, to choose a preset without prompts, or to use a self-hosted homeserver:

```bash
uvx mindroom config init --provider anthropic
$EDITOR ~/.mindroom/.env
uvx mindroom run
```

The generated `.env` lists the credentials the chosen preset needs; set them before running.
See [`config init`](https://docs.mindroom.chat/cli/#config-init) for every option and the extra steps for the `codex`, `kimi`, `ollama`, and `llama.cpp` presets, and [Model Environment Variables](https://docs.mindroom.chat/configuration/models/#environment-variables) for each provider's credentials.



## Full stack Docker Compose

Use this when you want everything local: the MindRoom dashboard, a Matrix homeserver, and the MindRoom Chat client in one stack.
It requires Docker, Docker Compose, and Python 3 for the quickstart script.

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # set ANTHROPIC_API_KEY for the default stack config
./scripts/quickstart.py
```

The quickstart validates provider configuration, starts the stack, waits until it is ready, and explains common port and startup failures.
The default stack config uses Anthropic and requires `ANTHROPIC_API_KEY`.
To use another provider, edit `config/config.yaml` and supply its credentials before starting; see the [stack model configuration guide](https://github.com/mindroom-ai/mindroom-stack#configure-models).

The stack serves the MindRoom dashboard at `http://localhost:8765`, the MindRoom Chat client at `http://localhost:8080`, and Matrix at `http://localhost:8008`, using the published `mindroom`, `mindroom-chat`, and `mindroom-tuwunel` images.
To reach it from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting.
See the [stack README](https://github.com/mindroom-ai/mindroom-stack) for running `docker compose` directly, changing host ports, and pinning images.



## Advanced: agent-managed NixOS container

Use this when you want to give a MindRoom agent full freedom over its own virtual machine while you, from the host, control precisely what it can see.
The [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) flake provisions an Incus LXC system container running NixOS with MindRoom, the Tuwunel Matrix homeserver, MindRoom Chat, and Caddy, plus Docker and `ragenix`-based secrets.
Because the whole machine is declared in the flake, the agent can rebuild and manage the persistent system it runs on without touching the host, unlike the mostly stateless Docker Compose stack.
It is harder to set up by hand, but a coding agent such as Codex or Claude Code can follow the repo's `AGENTS.md` runbook directly.

It requires a Linux host running [Incus](https://linuxcontainers.org/incus/docs/main/installing/):

```bash
git clone https://github.com/mindroom-ai/lxc-nixos.git
cd lxc-nixos
incus launch images:nixos/unstable mindroom -c security.nesting=true
incus config device add mindroom repo disk source="$PWD" path=/mnt/repo shift=true
```

Then follow the repo's `AGENTS.md` runbook for operator SSH keys, secrets bootstrap, and the `nixos-rebuild switch` deployment.

## Manual install with your own Matrix homeserver

Use this if you already have a Matrix homeserver and want to run MindRoom directly.

Prerequisites:

- Python 3.12 or higher.
- A Matrix homeserver where MindRoom can create agent accounts (see [Matrix account provisioning](#matrix-account-provisioning)).
- Credentials for at least one AI provider.

### Installation

=== "uv (recommended)"

    ```bash
    uv tool install mindroom
    ```

=== "pip"

    ```bash
    pip install mindroom
    ```

=== "From source"

    ```bash
    git clone https://github.com/mindroom-ai/mindroom
    cd mindroom
    uv sync
    source .venv/bin/activate
    ```

    Install Node.js 24 and [Bun](https://bun.sh/) to build missing dashboard assets on first run, or set `MINDROOM_FRONTEND_DIST` to a prebuilt dashboard directory.
    Without assets and Bun, the API is available but the dashboard is not.

### Configuration

Generate a starter with `mindroom config init --matrix-server self-hosted`, or write `config.yaml` yourself.
A generated config contains `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__` placeholders; replace each with your quoted Matrix user ID, such as `"@alice:matrix.example.com"`, because only hosted pairing fills them in.
See [Configuration](https://docs.mindroom.chat/configuration/#configuration-file) for where MindRoom looks for `config.yaml`.

A minimal hand-written `config.yaml`:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant that can answer questions
    model: default
    rooms: [lobby]

models:
  default:
    provider: openai
    id: gpt-6-astra

defaults:
  tools: [scheduler]
  markdown: true

administrators:
  - "@alice:matrix.example.com"
room_defaults:
  invite_users:
    - "@alice:matrix.example.com"

timezone: America/Los_Angeles
```

Put credentials in a `.env` next to `config.yaml`:

```bash
MATRIX_HOMESERVER=https://matrix.example.com

# Recommended when the homeserver supports token-gated registration.
# MATRIX_REGISTRATION_TOKEN=your-registration-token

# Optional: for self-signed certificates (development)
# MATRIX_SSL_VERIFY=false

# Optional: when server_name differs from the homeserver hostname
# MATRIX_SERVER_NAME=example.com

# AI provider API keys
OPENAI_API_KEY=your_openai_key
# ANTHROPIC_API_KEY=your_anthropic_key
# OPENROUTER_API_KEY=your_openrouter_key

# Dashboard API key; generate one with `openssl rand -hex 32`.
# `mindroom run` listens on every network interface by default, so keep this key set;
# leave it empty (MINDROOM_API_KEY=) only together with `--api-host 127.0.0.1`.
MINDROOM_API_KEY=replace-with-a-long-random-secret
```

See [Environment Variables](https://docs.mindroom.chat/configuration/#environment-variables) for every setting.

### Matrix account provisioning

MindRoom creates a Matrix account for each agent, team, and the router.
Configure one way for it to do so: hosted provisioning, `MATRIX_REGISTRATION_TOKEN`, `MATRIX_REGISTRATION_SHARED_SECRET`, an [application service](https://docs.mindroom.chat/matrix/#agent-users), or intentionally open registration.
If registration fails, check the selected credentials and the homeserver's registration policy.

### Optional: local Synapse and MindRoom Chat with Docker

On Linux or macOS, `local-stack-setup` starts Synapse and a MindRoom Chat container from the core MindRoom repository's `local/matrix` Compose files and writes the matching Matrix settings to the `.env` next to your active config:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom/local/matrix
```

From a source checkout, run `uv run mindroom local-stack-setup --synapse-dir local/matrix`.
With a custom config path, export `MINDROOM_CONFIG_PATH` first so it updates the matching `.env`.
See [local-stack-setup](https://docs.mindroom.chat/cli/#local-stack-setup) for its options.

### Run MindRoom

```bash
mindroom run
```

MindRoom connects to the homeserver, creates Matrix users for each agent, creates and joins the configured rooms, and starts answering messages.
See [`mindroom run`](https://docs.mindroom.chat/cli/#run) for options such as `--config` and `--storage-path`.

## Next Steps

- Learn about [agent configuration](https://docs.mindroom.chat/configuration/agents/)
- Learn about [OpenClaw workspace import](https://docs.mindroom.chat/openclaw/) if you want file-based memory/context patterns
- Explore [available tools](https://docs.mindroom.chat/tools/)
- Set up [teams for multi-agent collaboration](https://docs.mindroom.chat/configuration/teams/)
