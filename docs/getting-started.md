---
icon: lucide/rocket
---

# Getting Started

This guide will help you set up MindRoom and create your first AI agent.

## Recommended: Hosted Matrix + Local MindRoom (`uv` only)

If you do not want to self-host Matrix yet, this is the simplest setup.
You only run MindRoom locally.

**Prerequisite:** Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

### 1. Initialize local config

```bash
uvx mindroom config init --profile public
```

This creates:

- `~/.mindroom/config.yaml`
- `~/.mindroom/.env` prefilled with `MATRIX_HOMESERVER=https://mindroom.chat`

The `--profile public` template defaults to the `openai` provider.
Use `--provider` to select a different provider preset:

```bash
# Use Anthropic Claude
uvx mindroom config init --profile public --provider anthropic

# Use Vertex AI Claude (Google Cloud)
uvx mindroom config init --profile public-vertexai-anthropic
```

Profile aliases `public-vertexai-claude`, `vertexai-anthropic`, and `vertexai-claude` are also accepted.

Other profiles:

- `--profile full` — rich starter config with interactive provider selection (default)
- `--profile minimal` or `--minimal` — bare-minimum config

### 2. Add model API key(s)

```bash
$EDITOR ~/.mindroom/.env
```

Set at least one key:

- `ANTHROPIC_API_KEY=...`, or
- `OPENAI_API_KEY=...`, or
- `OPENROUTER_API_KEY=...`, or
- For Vertex AI Claude: set `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` and authenticate with `gcloud auth application-default login`.

### 3. Pair your local install from chat UI

1. Open `https://chat.mindroom.chat` and sign in.
2. Go to `Settings -> Local MindRoom`.
3. Click `Generate Pair Code`.
4. Run locally:

```bash
uvx mindroom connect --pair-code ABCD-EFGH
```

Notes:

- Pair code is short-lived (10 minutes). Generate a new one if it expires.
- `mindroom connect` writes local provisioning values (including `MINDROOM_NAMESPACE`) into `~/.mindroom/.env` by default.
- Use `--no-persist-env` to export variables only for the current shell session instead of writing to `.env`.

### 4. Run MindRoom

```bash
uvx mindroom run
```

### 5. Verify

**In chat:** Send a message mentioning your agent in a room where it is configured.

**Dashboard:** Access the web dashboard at `http://localhost:8765` to configure agents, models, and tools.
Protect the dashboard API in non-localhost environments by setting `MINDROOM_API_KEY` in your `.env`.

**Preflight check:** Run `mindroom doctor` before `mindroom run` to verify config, API keys, Matrix connectivity, and storage in one pass.

For a detailed architecture and credential model, see:
[Hosted Matrix deployment guide](deployment/hosted-matrix.md).

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
$EDITOR .env  # add at least one AI provider key
```

### 3. Start everything

```bash
docker compose up -d
```

Open:

- MindRoom UI: http://localhost:8765
- MindRoom client: http://localhost:8080
- Matrix homeserver: http://localhost:8008

The stack uses published `mindroom`, `mindroom-cinny`, and `mindroom-tuwunel` images by default.

If you access the stack from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

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
    id: gpt-5.4

defaults:
  tools: [scheduler]
  markdown: true

timezone: America/Los_Angeles
```

#### 2. Set up environment variables

Create a `.env` file with your credentials:

```bash
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER=https://matrix.example.com

# Optional: For self-signed certificates (development)
# MATRIX_SSL_VERIFY=false

# Optional: For federation setups where server_name differs from homeserver hostname
# MATRIX_SERVER_NAME=example.com

# AI provider API keys
OPENAI_API_KEY=your_openai_key
# OPENROUTER_API_KEY=your_openrouter_key
# ANTHROPIC_API_KEY=your_anthropic_key

# Optional: protect the dashboard API (recommended for non-localhost)
# MINDROOM_API_KEY=your-secret-key
```

#### Optional: Bootstrap local Synapse + Cinny with Docker (Linux/macOS)

If you want a local Matrix + client setup without running the full `mindroom-stack` app,
use the helper command:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
```

If you're running from source in this repo, use:

```bash
uv run mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
```

This starts Synapse from the `mindroom-stack` compose files, starts a MindRoom Cinny
container, waits for both services to be healthy, and by default writes local Matrix
settings to `.env` next to your active `config.yaml`.

> [!NOTE]
> MindRoom automatically creates Matrix user accounts for each agent. Your Matrix homeserver must allow open registration, or you need to configure it to allow registration from localhost. If registration fails, check your homeserver's registration settings.

#### 3. Run MindRoom

```bash
mindroom run
```

MindRoom will:

1. Connect to your Matrix homeserver
2. Create Matrix users for each agent
3. Create any rooms that don't exist and join them
4. Start listening for messages

## Next Steps

- Learn about [agent configuration](configuration/agents.md)
- Learn about [OpenClaw workspace import](openclaw.md) if you want file-based memory/context patterns
- Explore [available tools](tools/index.md)
- Set up [teams for multi-agent collaboration](configuration/teams.md)
