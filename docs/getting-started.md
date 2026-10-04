---
icon: lucide/rocket
---

# Getting Started

This guide will help you set up MindRoom and create your first AI agent.

## Recommended: Hosted Matrix + Local MindRoom (`uv` only)

If you do not want to self-host Matrix yet, this is the simplest setup.
You only run MindRoom locally.
Watch the 2-minute setup video:

[![MindRoom: installing and talking to my first AI agent in 2 minutes](https://img.youtube.com/vi/jR3xLUxyWhg/maxresdefault.jpg)](https://youtu.be/jR3xLUxyWhg)

**Prerequisite:** Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

### 1. Run MindRoom

```bash
uvx mindroom run
```

When no config exists yet and MindRoom runs in a terminal, it asks a few setup questions first:

1. Choose a model provider preset (default `openai`).
2. For `anthropic`, `openai`, or `openrouter`, paste the API key; input is hidden and never printed.
   Press Enter to skip, then connect the provider later through the dashboard's provider setup, or add the key to `~/.mindroom/.env` and restart `mindroom run`.
   MindRoom does not ask when the key is already set in your environment.
   Other presets print their remaining setup step instead, such as `codex login`, `ollama pull`, or the Azure, Bedrock, or Vertex AI settings to add to `~/.mindroom/.env` before restarting `mindroom run`.

MindRoom then writes `~/.mindroom/config.yaml` and `~/.mindroom/.env` with hosted Matrix defaults (`MATRIX_HOMESERVER=https://mindroom.chat`) and continues straight into pairing.
Without a terminal (services, Docker, the macOS app), a missing config is an error that points to `mindroom config init`, unless the flags below answer the questions.

Flags answer the same questions up front, which lets a coding agent or script set up MindRoom without a terminal:

```bash
OPENAI_API_KEY=sk-... uvx mindroom run --provider openai --service
```

`--provider` picks the preset, the API key comes from the environment, and `--service` installs the login service after pairing (`--no-service` skips the question).
Without a terminal, MindRoom prints the pairing link and code for a person to approve, and the approving account is printed instead of asked about.

On first run, MindRoom automatically initiates pairing.
It prints a pairing link and QR code.
Open the link or scan the QR code with your MindRoom Chat account to approve the pairing.
Alternatively, enter the displayed code in MindRoom Chat → Settings → Local MindRoom.
After approval, MindRoom prints the approving account and, in a terminal, asks `Is this your account? [Y/n]` before saving anything.

After pairing completes, first-run setup asks whether to run MindRoom in the background and start it at login, as a systemd user service on Linux or a launchd agent on macOS.
Pressing Enter installs and starts the service, the same as `mindroom service install`, and returns to your shell; check it with `mindroom service status`, and run `mindroom service restart` after editing `~/.mindroom/.env`.
Answering `n` starts the runtime and dashboard in the terminal instead, which also happens on platforms without launchd or systemd or when the service cannot be installed.
See [`mindroom service`](cli.md#service) for when the question is skipped.

Notes:

- Pair codes are short-lived (10 minutes).
- `mindroom run` automatically prints a new link and code when the previous one expires, while `mindroom connect` exits after 10 minutes and asks you to run it again.
- `mindroom run` (or explicit `mindroom connect`) writes local provisioning values (including `MINDROOM_NAMESPACE`) into `~/.mindroom/.env`.
- Pairing writes `MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET` to `.env` and replaces owner placeholders in `config.yaml`.

### 2. Optional: set up explicitly with `config init`

Use `config init` to create or review the files before starting, to pick a preset non-interactively, or to use a self-hosted homeserver:

```bash
uvx mindroom config init
```

This creates:

- `~/.mindroom/config.yaml`
- `~/.mindroom/.env` prefilled with `MATRIX_HOMESERVER=https://mindroom.chat`

The default `--matrix-server mindroom.chat` preset uses hosted Matrix and defaults to the `openai` provider.
Use `--provider` to select a different provider preset:

```bash
# Use Anthropic Claude
uvx mindroom config init --provider anthropic

# Use Azure OpenAI
uvx mindroom config init --provider azure

# Use a Codex CLI ChatGPT login
uvx mindroom config init --provider codex

# Use local Ollama
uvx mindroom config init --provider ollama

# Use local llama.cpp through its OpenAI-compatible server
uvx mindroom config init --provider llama.cpp

# Use Vertex AI Claude (Google Cloud)
uvx mindroom config init --provider vertexai_claude
```

Use `--matrix-server mindroom.chat` for hosted Matrix and `--matrix-server self-hosted` when you run your own homeserver.
Use `--provider` for the model provider.
Run `codex login` before starting MindRoom when using `--provider codex`.

`--provider azure` uses Azure OpenAI through your deployment name.
Set `models.default.id` to the Azure deployment name you created.

`--provider ollama` uses local Ollama with `gemma4` by default and also configures `qwen3.8:27b`.
Run `ollama pull gemma4` and `ollama pull qwen3.8:27b` before starting MindRoom.

`--provider llama.cpp` uses a local OpenAI-compatible llama.cpp server on `http://localhost:8080/v1`.
Start `llama-server` with one of the configured Unsloth GGUF refs before starting MindRoom.
These local provider configs run entirely locally and do not require real cloud API keys such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` unless you switch the config to a remote provider.

Use `--provider vertexai_claude` for Vertex AI Claude on hosted Matrix.

Then add remote-provider credentials when needed:

```bash
$EDITOR ~/.mindroom/.env
```

For hosted providers, set the credentials for the provider you selected:

- `ANTHROPIC_API_KEY=...`, or
- `AZURE_OPENAI_API_KEY=...` and `AZURE_OPENAI_ENDPOINT=...`, or
- `OPENAI_API_KEY=...`, or
- `OPENROUTER_API_KEY=...`, or
- For Codex CLI ChatGPT authentication: run `codex login`.
- For Kimi Code CLI authentication: run `kimi` and `/login`.
- For Vertex AI Claude: set `ANTHROPIC_VERTEX_PROJECT_ID`, keep `CLOUD_ML_REGION=global` for the starter's Sonnet 5.5 model (or choose `us` / `eu`), and authenticate with `gcloud auth application-default login`.
Skip this step for `--provider ollama` or `--provider llama.cpp` unless you also add a remote provider.

Then run `uvx mindroom run`.

### 3. Verify

**In chat:** Open `https://chat.mindroom.chat` and send a message mentioning your agent in a room where it is configured (e.g., "@general hello" in "Lobby").

**Dashboard:** Access the web dashboard at `http://localhost:8765` to configure agents, models, and tools.
Setup writes a generated `MINDROOM_API_KEY` to your `.env`, and the dashboard asks for it before loading, because `mindroom run` serves the dashboard on every network interface by default.

**Preflight check:** Run `uvx mindroom doctor` before `uvx mindroom run` to verify config, API keys, Matrix connectivity, pairing, and storage in one pass.
Before the first run, doctor reports `Not paired yet` as a passing check, because `mindroom run` pairs automatically.

For a detailed architecture and credential model, see:
[Hosted Matrix deployment guide](deployment/hosted-matrix.md).

### Recommended: Hosted Matrix + Local MindRoom (`uvx` only)

You only run MindRoom locally; the Matrix homeserver is hosted at `mindroom.chat` and the chat UI at `chat.mindroom.chat`.
Watch the 2-minute setup video:

[![MindRoom: installing and talking to my first AI agent in 2 minutes](https://img.youtube.com/vi/jR3xLUxyWhg/maxresdefault.jpg)](https://youtu.be/jR3xLUxyWhg)

**Prerequisite:** Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uvx mindroom run
# First run: choose a model provider; anthropic, openai, and openrouter also ask for an API key (Enter skips).
# MindRoom writes ~/.mindroom/config.yaml and ~/.mindroom/.env with hosted defaults,
# then prints a link and QR code: approve it with your MindRoom Chat account,
# or enter the code in MindRoom Chat → Settings → Local MindRoom.
# Finally, Enter keeps MindRoom running in the background as a login service (systemd or launchd); n runs it here
```

To create the files without starting, use `uvx mindroom config init` and edit `~/.mindroom/.env` before `uvx mindroom run`.

See [the full walkthrough](#recommended-hosted-matrix-local-mindroom-uv-only) and [Hosted Matrix Deployment](deployment/hosted-matrix.md) for architecture details.

### Hosted Matrix + local MindRoom (recommended)

```bash
# Creates ~/.mindroom/config.yaml and ~/.mindroom/.env by default
uvx mindroom config init
$EDITOR ~/.mindroom/.env
uvx mindroom run
# Open the printed link or scan the QR code to approve pairing,
# or enter the code in MindRoom Chat → Settings → Local MindRoom
```

Pairing happens automatically on first run.

See [Hosted Matrix deployment](deployment/hosted-matrix.md) for the full walkthrough.
If you want worker-routed execution tools like `coding`, `docker`, `file`, `python`, and `shell` to run in dedicated Docker workers on the same machine, see [Sandbox Proxy Isolation](deployment/sandbox-proxy.md).

## Preferred alternative: NixOS LXC container (agent-controlled machine)

Use this when you want to give a MindRoom agent full freedom over its own virtual machine while you, from the host, control precisely what it can see.
The [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) flake provisions the virtual machine — an Incus LXC system container running NixOS — with the full MindRoom stack (MindRoom, Tuwunel Matrix homeserver, MindRoom Chat, and Caddy) plus Docker and `ragenix`-based secrets wiring.
Because the whole virtual machine is declared in the flake, the agent can rebuild and manage the persistent system it runs on — unlike the mostly stateless Docker Compose stack below — without ever touching the host.
It is slightly harder to set up by hand, but asking a coding agent such as Codex or Claude Code to do it is trivial: the repo ships machine-oriented instructions in `AGENTS.md`.
It requires a Linux host running [Incus](https://linuxcontainers.org/incus/docs/main/installing/).

```bash
git clone https://github.com/mindroom-ai/lxc-nixos.git
cd lxc-nixos
incus launch images:nixos/unstable mindroom -c security.nesting=true
incus config device add mindroom repo disk source="$PWD" path=/mnt/repo shift=true
```

Then follow the repo README for operator SSH keys, secrets bootstrap, and the `nixos-rebuild switch` deployment flow.

### Preferred alternative: NixOS LXC container (agent-controlled machine)

Use this when you want to give a MindRoom agent full freedom over its own virtual machine while you, from the host, control precisely what it can see.
A standalone NixOS flake provisions the virtual machine — an Incus LXC system container running NixOS — with the full MindRoom stack (MindRoom, Tuwunel Matrix homeserver, MindRoom Chat, and Caddy) plus Docker and secrets wiring, so the agent can rebuild and manage the persistent virtual machine it runs on — unlike the mostly stateless Docker Compose stack below — without ever touching the host.
It is slightly harder to set up by hand, but asking a coding agent such as Codex or Claude Code to do it is trivial: the repo ships machine-oriented instructions in `AGENTS.md`.
See [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) for the full setup.

### NixOS LXC container (preferred alternative, agent-controlled machine)

Use this when you want to give a MindRoom agent full freedom over its own virtual machine while you, from the host, control precisely what it can see.
The [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) flake provisions the virtual machine — an Incus LXC system container running NixOS — with MindRoom, Tuwunel, MindRoom Chat, and Caddy plus Docker and `ragenix`-based secrets wiring, so the agent can rebuild and manage the persistent system it runs on — unlike the mostly stateless Docker Compose stack below — without ever touching the host.
It is slightly harder to set up by hand, but asking a coding agent such as Codex or Claude Code to do it is trivial: the repo ships machine-oriented instructions in `AGENTS.md`.
It requires a Linux host running [Incus](https://linuxcontainers.org/incus/docs/main/installing/); see the repo README for the full setup.

```bash
git clone https://github.com/mindroom-ai/lxc-nixos.git
cd lxc-nixos
incus launch images:nixos/unstable mindroom -c security.nesting=true
incus config device add mindroom repo disk source="$PWD" path=/mnt/repo shift=true
```

## Alternative: Full Stack Docker Compose (bundled dashboard + Matrix + MindRoom client)

Use this when you want everything local: the bundled MindRoom dashboard, Matrix homeserver, and a Matrix client in one stack.

**Prereqs:** Docker + Docker Compose.

### 1. Clone the full stack repo

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
```

### 2. Add your API keys

```bash
cp .env.example .env
$EDITOR .env  # set ANTHROPIC_API_KEY for the default stack config
```

The default stack config uses Anthropic and requires `ANTHROPIC_API_KEY`.
To use another provider, edit `config/config.yaml` and supply its matching credentials before starting the stack; see the [stack model configuration guide](https://github.com/mindroom-ai/mindroom-stack#configure-models).

### 3. Start everything

```bash
docker compose up -d
```

Open:

- MindRoom UI: http://localhost:8765
- MindRoom client: http://localhost:8080
- Matrix homeserver: http://localhost:8008

The stack uses published `mindroom`, `mindroom-chat`, and `mindroom-tuwunel` images by default.

If you access the stack from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

### Alternative: Full Stack Docker Compose (bundled dashboard + Matrix + MindRoom client)

Use this when you want everything local: the bundled MindRoom dashboard, Matrix homeserver, and a Matrix client in one stack.

**Prereqs:** Docker + Docker Compose.

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # set ANTHROPIC_API_KEY for the default stack config

docker compose up -d
```

The default stack config uses Anthropic and requires `ANTHROPIC_API_KEY`.
To use another provider, edit `config/config.yaml` and supply its matching credentials before starting the stack; see the [stack model configuration guide](https://github.com/mindroom-ai/mindroom-stack#configure-models).

Open:

- MindRoom UI: http://localhost:8765
- MindRoom client: http://localhost:8080
- Matrix homeserver: http://localhost:8008

The stack uses published `mindroom`, `mindroom-chat`, and `mindroom-tuwunel` images by default.

If you access the stack from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

### Full Stack Docker Compose (all-local alternative)

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # set ANTHROPIC_API_KEY for the default stack config

./scripts/quickstart.py
```

The default stack config uses Anthropic and requires `ANTHROPIC_API_KEY`.
To use another provider, edit `config/config.yaml` and supply its matching credentials before starting the stack; see the [stack model configuration guide](https://github.com/mindroom-ai/mindroom-stack#configure-models).

Raw `docker compose up -d` remains a manual fallback; the quickstart validates provider configuration, waits for readiness, and diagnoses common port and startup failures.

The stack exposes MindRoom at `http://localhost:8765`, the MindRoom client at `http://localhost:8080`, and Matrix at `http://localhost:8008`.
The stack uses published `mindroom`, `mindroom-chat`, and `mindroom-tuwunel` images by default.
If you access it from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

## Manual Install (advanced)

Use this if you already have a Matrix homeserver and want to run MindRoom directly.

### Prerequisites

- Python 3.12 or higher
- A Matrix homeserver (or use a public one like matrix.org)
- API keys for your preferred AI provider (Anthropic, OpenAI, etc.)

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
    Without assets and Bun, the API is available but the dashboard is unavailable.

### Configuration

#### 1. Create your config file

Create a `config.yaml` in your working directory:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant that can answer questions
    model: default
    include_default_tools: true
    rooms: [lobby]
    # Optional: file-based context (OpenClaw-style)
    # context_files: [SOUL.md, USER.md]

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

#### 2. Set up environment variables

Create a `.env` file with your credentials:

```bash
# Matrix homeserver must allow managed account provisioning.
MATRIX_HOMESERVER=https://matrix.example.com

# Recommended when the homeserver supports token-gated registration.
# MATRIX_REGISTRATION_TOKEN=your-registration-token

# Optional: For self-signed certificates (development)
# MATRIX_SSL_VERIFY=false

# Optional: For federation setups where server_name differs from homeserver hostname
# MATRIX_SERVER_NAME=example.com

# AI provider API keys
OPENAI_API_KEY=your_openai_key
# OPENROUTER_API_KEY=your_openrouter_key
# ANTHROPIC_API_KEY=your_anthropic_key

# Dashboard API key; generate one with `openssl rand -hex 32`.
# Set it empty (MINDROOM_API_KEY=) only with `--api-host 127.0.0.1`; `mindroom run` listens on every interface by default.
MINDROOM_API_KEY=replace-with-a-long-random-secret
```

#### Optional: Bootstrap local Synapse + MindRoom Chat with Docker (Linux/macOS)

Use the core [MindRoom repository](https://github.com/mindroom-ai/mindroom)'s `local/matrix` development Compose files with a host-installed backend:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom/local/matrix
```

If you're running from source in this repo, use:

```bash
uv run mindroom local-stack-setup --synapse-dir local/matrix
```

This starts Synapse from the core repository development Compose files, starts a MindRoom Chat container, waits for both services to be healthy, and by default writes local Matrix settings to `.env` next to your active config file.
If you use a custom config path, export `MINDROOM_CONFIG_PATH` before running the helper so it updates the matching `.env`.
The full `mindroom-stack` workflow above uses Tuwunel instead.

> [!NOTE]
> MindRoom automatically creates Matrix user accounts for each agent.
> Configure one supported provisioning path: hosted provisioning, `MATRIX_REGISTRATION_TOKEN`, `MATRIX_REGISTRATION_SHARED_SECRET`, or intentionally open registration.
> If registration fails, check the selected provisioning credentials and homeserver policy.

#### 3. Run MindRoom

```bash
mindroom run
```

MindRoom will:

1. Connect to your Matrix homeserver
2. Create Matrix users for each agent
3. Create any rooms that don't exist and join them
4. Start listening for messages

### Manual Install (advanced)

Use this if you already have a Matrix homeserver and want to run MindRoom directly.

```bash
# Using uv
uv tool install mindroom

# Or using pip
pip install mindroom
```

### Basic Usage (manual)

1. Create a `config.yaml`:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
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
```

2. Set up your environment in `.env`:

```bash
# Matrix homeserver must allow agent registration, either through a
# registration token/provisioning service or intentionally open registration.
MATRIX_HOMESERVER=https://matrix.example.com
# MATRIX_REGISTRATION_TOKEN=your-registration-token

# AI provider API keys
OPENAI_API_KEY=your_api_key
```

3. Run MindRoom:

```bash
mindroom run
```

For local development with a host-installed backend plus Dockerized Synapse + MindRoom Chat (Linux/macOS), use the core MindRoom checkout's `local/matrix` directory:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom/local/matrix
mindroom run
```

### Direct (Development)

```bash
mindroom run --storage-path ./mindroom_data
```

The config file path is set via `MINDROOM_CONFIG_PATH` and otherwise defaults to `./config.yaml`, then `~/.mindroom/config.yaml`.

For local Matrix + MindRoom Chat with a host-installed MindRoom runtime (Linux/macOS), use the core MindRoom checkout's `local/matrix` directory:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom/local/matrix
mindroom run --storage-path ./mindroom_data
```

## Next Steps

- Learn about [agent configuration](configuration/agents.md)
- Learn about [OpenClaw workspace import](openclaw.md) if you want file-based memory/context patterns
- Explore [available tools](tools/index.md)
- Set up [teams for multi-agent collaboration](configuration/teams.md)
