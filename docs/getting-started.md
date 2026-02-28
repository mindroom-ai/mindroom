---
icon: lucide/rocket
---

# Getting Started

This guide will help you set up MindRoom and create your first AI agent.

## Recommended: Hosted Matrix + Local Backend (`uvx` only)

If you do not want to self-host Matrix yet, this is the simplest setup.
You only run the MindRoom backend locally.

### 1. Create a local project

```bash
mkdir -p ~/mindroom-local
cd ~/mindroom-local
uvx mindroom config init --profile public
```

This creates:

- `config.yaml`
- `.env` prefilled with `MATRIX_HOMESERVER=https://mindroom.chat`

### 2. Add model API key(s)

```bash
$EDITOR .env
```

Set at least one key:

- `ANTHROPIC_API_KEY=...`, or
- `OPENAI_API_KEY=...`, or
- another supported provider key.

### 3. Pair your local install from chat UI

1. Open `https://chat.mindroom.chat` and sign in.
2. Go to `Settings -> Local MindRoom`.
3. Click `Generate Pair Code`.
4. Run locally:

```bash
uvx mindroom connect --pair-code ABCD-EFGH
```

Notes:

- Pair code is short-lived (10 minutes).
- `mindroom connect` writes local provisioning credentials into `.env`.
- Those credentials are not Matrix access tokens.
- They only authorize provisioning endpoints for local onboarding.

### 4. Run MindRoom

```bash
uvx mindroom run
```

### 5. Verify in chat

Send a message mentioning your agent in a room where it is configured.

For a detailed architecture and credential model, see:
[Hosted Matrix deployment guide](deployment/hosted-matrix.md).

## Alternative: Full Stack Docker Compose (backend + frontend + Matrix + Element)

Use this when you want everything local: backend, frontend, Matrix homeserver, and a Matrix client in one stack.

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

- MindRoom UI: http://localhost:3003
- Element: http://localhost:8080
- Matrix homeserver: http://matrix.localhost:8008

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
    # context_files: [./workspace/SOUL.md, ./workspace/USER.md]

models:
  default:
    provider: anthropic
    id: claude-sonnet-4-5-latest

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
ANTHROPIC_API_KEY=your_anthropic_key
# OPENAI_API_KEY=your_openai_key
# GOOGLE_API_KEY=your_google_key

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
